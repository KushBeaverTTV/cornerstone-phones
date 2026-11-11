"""The Google Fast Pair reader: per-earbud battery for every followed device.

    cornerstone-fastpair [bluetooth-address ...]
    cornerstone-fastpair --help

Not a Headset, and not started through cornerstone-bridge. BlueZ allows one
org.bluez.Profile1 registration per UUID for the whole system, so a reader per
device would see every one after the first told the UUID is taken. This one
process registers the Message Stream profile once and serves every device the
shell follows; the object path that comes with each socket BlueZ hands over
says which device it belongs to.

The process contract (Service.qml and DeviceFollower.qml read it)
------------------------------------------------------------------

Arguments: zero or more Bluetooth addresses, followed from the start. Any
argument that is not an address ends the run at once (exit 1, error line).
`-h` or `--help` first prints the usage as plain text and exits 0.

stdin, one command per line. Anything else, or a malformed address, is
ignored:

    follow <address>     keep that device's channel open from now on;
                         following a followed device does nothing
    unfollow <address>   let the channel go and say nothing more about it
    refresh <address>    drop the channel and open it again: the device then
                         re-announces, the only way to ask for a reading it
                         thinks we already have; the next battery message is
                         printed even if it repeats the last one

Closing stdin, SIGTERM and SIGINT end the run cleanly. A run with nothing
followed is idle, not finished.

stdout, one JSON object per line, three shapes:

    a reading, the whole state of one device, printed whenever any of it
    changes (keys in this order, the last three only once the device has
    sent them, and then on every later line):

        address          str   uppercase, colon separated
        stream           bool  true
        single           bool  true for a set with one battery for everything
        battery          int   that one battery, 0-100, or -1; -1 unless single
        batteryCharging  bool
        left, right, case                          int   0-100, or -1 for no
                                                         reading; -1 when single
        leftCharging, rightCharging, caseCharging  bool
        modelId          str   the Fast Pair model id, lowercase hex
        bleAddress       str   the current (rotating) BLE address, uppercase
        firmware         str   the firmware version string

    a device's channel going down, the only line where stream is false:

        {"address": str, "stream": false, "error": str}

    the reader itself, with no address: {"stream": false, "error": str} when
    it cannot serve anyone (a missing dependency, no system bus, the profile
    refused at start, a registration that keeps failing), and {"stream": true}
    when a reported registration failure is over

Exit codes: 0 for a clean stop (asked, stdin closed, --help); 1 for anything
else, always after a no-address error line on stdout.

The Message Stream
------------------

RFCOMM UUID df21fe2c-2515-4fdb-8886-f12c4d67927c, a public Google spec
(developers.google.com/nearby/fast-pair/specifications/extensions/messagestream).
BlueZ does the SDP lookup and hands over the socket; the device then announces
itself and pushes a battery message whenever a level changes. Nothing is
written to it.

    frame   group u8  code u8  length u16 BE  payload

    group 3 (device information):
        code 1  model id, raw bytes
        code 2  BLE address, 6 bytes
        code 3  battery: 3 bytes left, right, case, or 1 byte for a set with
                one battery. High bit charging, low seven bits the level;
                0x7F in the low seven bits means no reading, and so does a
                level above 100 (a Redmi Buds 5 sent 0xFE). Other lengths
                (a Redmi Buds 5 also sent two bytes) are not read.
        code 9  firmware version, UTF-8

Keeping the channels up
-----------------------

BlueZ hands a socket over only when it opens a channel. One it thinks is open
already gets an "already connected" answer and no socket, which is the usual
case for a device followed after the registration landed (BlueZ opened its
channel then, and it was refused because nobody followed it yet). So each
device has a supervisor: the first attempt is a ConnectProfile, every later
one a DisconnectProfile and then, once that is answered, a ConnectProfile.
One call per device is out at a time. Attempts come 300 ms after the device
is found, 2 s after each answer for the first four attempts of an outage,
4 s after a channel closes, and every 20 s for every device regardless. Only
the fourth and later failed attempts of one outage are reported, and never an
"already connected" answer. A device BlueZ does not know is looked up again
every 5 s, reported at once when the shell asked for it and from the third
lookup otherwise. A bluetoothd that leaves and comes back is registered with
again and every device looked up again.

Everything that touches D-Bus or GLib is in LiveBus and LiveLoop at the end;
the rest takes them as arguments, so tests/test_fastpair.py runs it on fakes.
"""
import os
import struct
import sys
import time
import traceback

from core.headset import ADDRESS, CLEAN, TRANSIENT

UUID = "df21fe2c-2515-4fdb-8886-f12c4d67927c"
PROFILE_PATH = "/io/cornerstone/phones/gfps"
PROFILE_OPTIONS = {
    "Name": "Cornerstone Phones",
    "Role": "client",
    "RequireAuthentication": False,
    "RequireAuthorization": False,
    # On: BlueZ opens the channel itself when a device connects.
    "AutoConnect": True,
}
USAGE = "usage: cornerstone-fastpair [bluetooth-address ...]"

FAILED = TRANSIENT      # the one failure code this process has

DEVICE_INFO = 0x03
MODEL_ID, BLE_ADDRESS, BATTERY, FIRMWARE = 0x01, 0x02, 0x03, 0x09
NO_READING = 0x7F
CHARGING = 0x80
HEADER = 4
READ_SIZE = 4096

FIRST_LOOK_MS = 300         # device found -> first attempt
ANSWER_MS = 2000            # an attempt answered -> the next look
CLOSED_MS = 4000            # a channel closed -> the next look
STEADY_MS = 20000           # every device, always
LOOKUP_MS = 5000            # device or registration not there -> look again
QUICK_ATTEMPTS = 4          # attempts that get ANSWER_MS, and that stay quiet
QUIET_LOOKUPS = 3           # lookups that stay quiet unless the shell asked

SYNC_TIMEOUT_S = 5          # lookups and registration, which block the loop
CALL_TIMEOUT_S = 20         # ConnectProfile / DisconnectProfile, asynchronous

# ConnectProfile's ways of saying the channel is up already. BlueZ says it
# four ways depending on where it noticed; matched exactly so that real
# errors still get through.
ALREADY_CONNECTED_NAMES = ("org.bluez.Error.AlreadyConnected", "org.bluez.Error.InProgress")
ALREADY_CONNECTED_TEXTS = ("br-connection-already-connected", "Already Connected")
# RegisterProfile's ways of saying the UUID is held, by BlueZ version.
TAKEN_NAMES = ("org.bluez.Error.NotPermitted", "org.bluez.Error.AlreadyExists")


class BusError(Exception):
    """A D-Bus error, reduced to the two strings the reader looks at."""

    def __init__(self, name="", message=""):
        super().__init__(message)
        self.name = name or ""
        self.message = message or ""


def already_connected(error):
    return (error is not None
            and (error.name in ALREADY_CONNECTED_NAMES
                 or error.message.strip() in ALREADY_CONNECTED_TEXTS))


def registration_error(error):
    if error.name in TAKEN_NAMES:
        return ("cannot register the Fast Pair profile: another program (or a second "
                "copy of this widget) already holds it: %s" % error.message)
    return "cannot register the Fast Pair profile: %s" % error.message


def address_from_path(path):
    """'/org/bluez/hci0/dev_78_5E_A2_76_7F_20' -> '78:5E:A2:76:7F:20', or None.

    BlueZ names a device object after its address, which is what lets one
    process tell the sockets of several devices apart.
    """
    for part in str(path).split("/"):
        if part.startswith("dev_"):
            text = part[4:].replace("_", ":")
            if ADDRESS.match(text):
                return text.upper()
    return None


# ---- the bytes

def component(byte):
    """(level, charging) for one battery byte; (-1, False) for no reading."""
    level = byte & NO_READING
    if level == NO_READING or level > 100:
        return -1, False
    return level, bool(byte & CHARGING)


def unpack(buffer):
    """Whole frames off the front of a bytearray, as [(group, code, payload)].

    A frame not yet whole stays in the buffer for the next read.
    """
    out = []
    while len(buffer) >= HEADER:
        total = HEADER + struct.unpack_from(">H", buffer, 2)[0]
        if len(buffer) < total:
            break
        out.append((buffer[0], buffer[1], bytes(buffer[HEADER:total])))
        del buffer[:total]
    return out


def battery_values(payload):
    """The reading keys a battery payload sets, or None for one not read."""
    if len(payload) == 1:
        level, charging = component(payload[0])
        values = {"single": True, "battery": level, "batteryCharging": charging}
        parts = {}
    elif len(payload) == 3:
        values = {"single": False, "battery": -1, "batteryCharging": False}
        parts = dict(zip(("left", "right", "case"), (component(b) for b in payload)))
    else:
        return None
    for part in ("left", "right", "case"):
        level, charging = parts.get(part, (-1, False))
        values[part] = level
        values[part + "Charging"] = charging
    return values


def decode(group, code, payload):
    """(changes, is_battery) for one frame, or None for a frame not read."""
    if group != DEVICE_INFO:
        return None
    if code == MODEL_ID:
        return {"modelId": payload.hex()}, False
    if code == BLE_ADDRESS and len(payload) == 6:
        return {"bleAddress": ":".join("%02X" % b for b in payload)}, False
    if code == FIRMWARE:
        return {"firmware": payload.decode("utf-8", "replace")}, False
    if code == BATTERY:
        values = battery_values(payload)
        if values is not None:
            values["stream"] = True
            return values, True
    return None


# ---- one device's state, and the lines it prints

def blank_values():
    return {
        "stream": True,
        "single": False,
        "battery": -1, "batteryCharging": False,
        "left": -1, "leftCharging": False,
        "right": -1, "rightCharging": False,
        "case": -1, "caseCharging": False,
    }


class Reading:
    """What one device last said, printed whole whenever any of it changes.

    The levels and the identifying keys outlive an outage: a channel that
    comes back starts from them rather than from nothing, so a refresh never
    blanks the panel on its way to the same figures.
    """

    def __init__(self, address, emit):
        self.address = address
        self.emit = emit
        self.values = blank_values()
        self.last_error = None      # the outage already printed, said once
        self.repeat = False         # print the next battery message, even unchanged
        self.owed = False           # the shell's last line for us said stream false
        self.quiet = False          # the channel is being cycled on purpose

    def write(self, payload):
        if not self.quiet:
            line = {"address": self.address}
            line.update(payload)
            self.emit(line)

    def announce_again(self):
        """Armed by a refresh. Only a battery message spends it: the model id
        and BLE address come first on a new channel, and a line for them must
        not use up the one the levels are owed."""
        self.repeat = True

    def stream_up(self):
        self.values["stream"] = True
        self.values.pop("error", None)
        self.last_error = None

    def stream_down(self, reason):
        self.values["stream"] = False
        if self.quiet or reason == self.last_error:
            return
        self.last_error = reason
        self.owed = True
        self.write({"stream": False, "error": reason})

    def update(self, changes, battery=False):
        forced = self.owed or (battery and self.repeat)
        if battery:
            self.repeat = False
        if not forced and all(self.values.get(k) == v for k, v in changes.items()):
            return
        self.values.update(changes)
        self.last_error = None
        self.owed = False
        self.write(dict(self.values))


# ---- one socket

class Channel:
    """One open Message Stream socket, read until it runs dry each time."""

    def __init__(self, fd, reading, loop, on_close=None):
        self.fd = fd
        self.reading = reading
        self.loop = loop
        self.on_close = on_close
        self.alive = True
        self.buffer = bytearray()
        os.set_blocking(fd, False)
        reading.stream_up()
        self.watch = loop.watch(fd, self.ready)

    def ready(self, hangup):
        """The socket woke us. Bytes still in it are read before a hang-up is
        believed: the last thing a device sends before it drops the channel is
        often a battery message. False once the channel is closed."""
        if not self.alive:
            return False
        try:
            still_open, reason = self.pump()
        except Exception as error:      # a frame this code cannot take
            still_open, reason = False, "%s: %s" % (type(error).__name__, error)
            hangup = False
        if hangup:
            self.close("message stream closed by the device")
        elif not still_open:
            self.close(reason)
        return self.alive

    def pump(self):
        while True:
            try:
                data = os.read(self.fd, READ_SIZE)
            except BlockingIOError:
                return True, None
            except OSError as error:
                return False, "read failed: %s" % error
            if not data:
                return False, "message stream ended"
            self.buffer += data
            for group, code, payload in unpack(self.buffer):
                decoded = decode(group, code, payload)
                if decoded is not None:
                    self.reading.update(*decoded)

    def close(self, reason):
        """Safe to call twice. The watch goes before the fd: a watch left on a
        closed fd spins the loop, and reads the next socket given that number."""
        if not self.alive:
            return
        self.alive = False
        if self.watch is not None:
            self.loop.unwatch(self.watch)
            self.watch = None
        try:
            os.close(self.fd)
        except OSError:
            pass
        self.reading.stream_down(reason)
        if self.on_close is not None:
            self.on_close()


# ---- one followed device

class Device:
    """A followed device: its reading, its channel, and the supervisor that
    keeps the channel open (see "Keeping the channels up" above)."""

    def __init__(self, reader, address):
        self.reader = reader
        self.bus = reader.bus
        self.address = address
        self.reading = Reading(address, reader.loop.emit)
        self.channel = None
        self.path = None            # the BlueZ object, once found
        self.dropped = False        # unfollowed: timers already set find nothing to do
        self.attempts = 0           # in the current outage
        self.lookups = 0            # failed lookups since the last that worked
        self.looking = False        # a lookup is already set for later
        self.calling = False        # a Connect/DisconnectProfile is out

    def later(self, ms, callback, *args):
        self.reader.loop.later(ms, callback, args, False)

    # -- finding the device

    def attach(self, asked):
        """Look the device up and start keeping its channel open. `asked` is a
        start-up or a `follow`, where a device BlueZ does not know is said at
        once; after a bluetoothd restart it may simply not be listed yet."""
        if self.dropped:
            return
        try:
            path = self.bus.device_path(self.address)
        except BusError as error:
            return self.look_again("cannot reach BlueZ: %s" % error.message, asked)
        if not path:
            return self.look_again("no paired device with address %s" % self.address, asked)
        self.path = path
        self.lookups = 0
        self.attempts = 0
        self.later(FIRST_LOOK_MS, self.supervise)

    def look_again(self, reason, asked):
        self.lookups += 1
        if asked or self.lookups >= QUIET_LOOKUPS:
            self.reading.stream_down(reason)
        if self.looking:
            return
        self.looking = True
        self.later(LOOKUP_MS, self.looked)

    def looked(self):
        self.looking = False
        self.attach(False)

    def forget(self):
        """bluetoothd went away, and the device object and channel with it."""
        self.close_channel("bluetoothd went away")
        self.path = None
        self.calling = False

    def drop(self):
        """Unfollowed: let the channel go, quietly. The shell stopped listening
        for this address before it said so; a parting line would reach nobody,
        or whoever follows the address next."""
        self.dropped = True
        self.reading.quiet = True
        self.disconnect()
        self.close_channel("unfollowed")

    # -- the channel

    def disconnect(self, then=None):
        """DisconnectProfile, and `then` once it is answered either way: a
        refused disconnect (nothing to drop) leaves things as wanted too."""
        if self.path is None:
            return
        self.calling = True

        def answered(*_ignored):
            self.calling = False
            if then is not None:
                then()

        self.bus.disconnect_profile(self.path, answered)

    def connect(self):
        """ConnectProfile. The socket arrives through take_socket, not here:
        this only learns whether the asking was refused."""
        if self.path is None:
            return
        self.calling = True
        self.bus.connect_profile(self.path, self.connected, self.connect_failed)

    def connected(self):
        self.calling = False
        self.look_soon()

    def connect_failed(self, error):
        self.calling = False
        if not already_connected(error) and self.attempts >= QUICK_ATTEMPTS:
            self.reading.stream_down("cannot connect the message stream: %s"
                                     % (error.message if error is not None else "no answer"))
        self.look_soon()

    def look_soon(self):
        # An answer is not a socket. The next attempt starts with the drop that
        # makes BlueZ open the channel anew, so have it in two seconds rather
        # than at the next steady check.
        if self.attempts < QUICK_ATTEMPTS:
            self.later(ANSWER_MS, self.supervise)

    def take_socket(self, fd):
        if self.channel is not None and self.channel.alive:
            # A second socket for a live channel: the old one goes, quietly,
            # since the stream is not going down.
            quiet, self.reading.quiet = self.reading.quiet, True
            self.close_channel("replaced by a new channel")
            self.reading.quiet = quiet
        self.attempts = 0
        self.channel = Channel(fd, self.reading, self.reader.loop, self.channel_closed)

    def channel_closed(self):
        self.later(CLOSED_MS, self.supervise)

    def close_channel(self, reason):
        channel, self.channel = self.channel, None
        if channel is not None:
            channel.close(reason)

    def refresh(self):
        """Drop the channel so the device announces itself again. Not an
        outage: nothing is printed for the drop, the levels stay, and it does
        not count towards the attempts an outage reports after."""
        if self.dropped or self.path is None or self.calling:
            return
        self.reading.quiet = True
        self.close_channel("refreshing")
        self.reading.announce_again()
        self.reading.quiet = False
        self.attempts = 0
        self.disconnect(then=self.connect)

    def supervise(self):
        if self.dropped:
            return
        if self.channel is not None and self.channel.alive:
            self.attempts = 0
            return
        if self.path is None or self.calling:
            return
        self.attempts += 1
        if self.attempts > 1:
            self.disconnect(then=self.connect)
        else:
            self.connect()


# ---- the process

class Reader:
    """Every followed device, behind the one registration.

    `loop` is the host: emit(line), later(ms, callback, args, repeat),
    watch(fd, callback(hangup)) / unwatch(handle), finish(code). `bus` is
    BlueZ: owner(), watch_owner(callback), device_path(address), register(),
    unregister(), connect_profile(path, reply, error) and
    disconnect_profile(path, done); a failure is a BusError.
    """

    def __init__(self, loop, bus):
        self.loop = loop
        self.bus = bus
        self.devices = {}
        self.finished = False
        self.owner = None           # whose bluetoothd the registration was made with
        self.live = False           # the profile is registered
        self.failures = 0           # registration attempts failed in a row
        self.retrying = False
        self.reported = None        # the process-level error last printed

    def start(self, addresses=()):
        for address in addresses:
            address = address.upper()
            if address not in self.devices:
                self.devices[address] = Device(self, address)
        self.register(True)
        if self.finished:
            return
        self.bus.watch_owner(self.owner_changed)
        self.loop.later(STEADY_MS, self.steady, (), True)

    def fatal(self, message):
        if not self.finished:
            self.finished = True
            self.loop.emit({"stream": False, "error": message})
            self.loop.finish(FAILED)

    def stop(self):
        if not self.finished:
            self.finished = True
            self.loop.finish(CLEAN)

    def stopping(self):
        self.bus.unregister()

    # -- the registration

    def register(self, initial):
        """Our Profile1, once for every device. Again whenever bluetoothd comes
        back, which forgets it; a registration BlueZ says is taken on a retry
        is most likely ours surviving, and is taken as done."""
        # Read first: a bluetoothd restarting in the middle leaves a stale
        # owner here, and the owner watch registers again.
        self.owner = self.bus.owner()
        try:
            self.bus.register()
        except BusError as error:
            if initial:
                return self.fatal(registration_error(error))
            if error.name not in TAKEN_NAMES:
                return self.register_later(registration_error(error))
        self.live = True
        self.failures = 0
        if self.reported is not None:
            # Nothing else would ever say the failure printed earlier is over.
            self.reported = None
            self.loop.emit({"stream": True})
        for device in list(self.devices.values()):
            device.attach(initial)

    def register_later(self, reason):
        self.failures += 1
        if self.failures >= QUIET_LOOKUPS and self.reported != reason:
            self.reported = reason
            self.loop.emit({"stream": False, "error": reason})
        if self.retrying:
            return
        self.retrying = True
        self.loop.later(LOOKUP_MS, self.registered_later, (), False)

    def registered_later(self):
        self.retrying = False
        self.register(False)

    def owner_changed(self, owner):
        """bluetoothd's bus name changed hands: once at the start with the
        current owner, then on every stop and start."""
        if not owner:
            for device in list(self.devices.values()):
                device.forget()
            self.owner = None
            self.live = False
            return
        if owner != self.owner:
            self.register(False)

    def steady(self):
        for device in list(self.devices.values()):
            device.supervise()

    # -- what BlueZ calls

    def new_connection(self, path, fd):
        device = self.devices.get(address_from_path(path))
        if device is None:
            # The registration is system-wide, so BlueZ opens channels for
            # devices nobody follows. Closing the fd is how one is refused.
            try:
                os.close(fd)
            except OSError:
                pass
            return
        device.take_socket(fd)

    def request_disconnection(self, path):
        device = self.devices.get(address_from_path(path))
        if device is not None:
            device.close_channel("BlueZ asked for the channel back")

    # -- what the shell says

    def handle_line(self, line):
        parts = line.strip().split()
        if len(parts) != 2 or not ADDRESS.match(parts[1]):
            return
        verb, address = parts[0], parts[1].upper()
        if verb == "follow":
            self.follow(address)
        elif verb == "unfollow":
            device = self.devices.pop(address, None)
            if device is not None:
                device.drop()
        elif verb == "refresh":
            device = self.devices.get(address)
            if device is not None:
                device.refresh()

    def follow(self, address):
        if address in self.devices:
            return
        device = self.devices[address] = Device(self, address)
        # Without a registration there is nothing to attach to yet; register()
        # attaches every device once there is.
        if self.live:
            device.attach(True)


# ---- the live side: D-Bus and GLib

class LiveBus:
    """BlueZ over the system bus, in the shape Reader wants."""

    def __init__(self, bus):
        import dbus
        from core import bluez
        self.dbus = dbus
        self.bluez = bluez
        self.bus = bus
        self.owner_watch = None

    def error(self, error):
        return BusError(error.get_dbus_name(), self.bluez.dbus_message(error))

    def interface(self, path, name):
        # Fresh each time: a proxy is bound to the bluetoothd that was running
        # when it was made, and a restarted one is somebody else.
        return self.dbus.Interface(self.bus.get_object("org.bluez", path), name)

    def owner(self):
        try:
            return str(self.bus.get_name_owner("org.bluez"))
        except self.dbus.DBusException:
            return None

    def watch_owner(self, callback):
        self.owner_watch = self.bus.watch_name_owner("org.bluez", callback)

    def device_path(self, address):
        try:
            return self.bluez.device_path(self.bus, address)
        except self.dbus.DBusException as error:
            raise self.error(error)

    def register(self):
        options = {key: self.dbus.Boolean(value) if isinstance(value, bool) else value
                   for key, value in PROFILE_OPTIONS.items()}
        try:
            self.interface("/org/bluez", "org.bluez.ProfileManager1").RegisterProfile(
                PROFILE_PATH, UUID, options, timeout=SYNC_TIMEOUT_S)
        except self.dbus.DBusException as error:
            raise self.error(error)

    def unregister(self):
        try:
            self.interface("/org/bluez", "org.bluez.ProfileManager1").UnregisterProfile(
                PROFILE_PATH, timeout=SYNC_TIMEOUT_S)
        except self.dbus.DBusException:
            pass

    # Asynchronous, both: bluetoothd calls us back (NewConnection,
    # RequestDisconnection) while it works on them, and a loop sitting in a
    # blocking call could not answer.
    def connect_profile(self, path, reply, error):
        try:
            self.interface(path, "org.bluez.Device1").ConnectProfile(
                UUID, timeout=CALL_TIMEOUT_S, reply_handler=reply,
                error_handler=lambda e: error(self.error(e)))
        except self.dbus.DBusException as failure:
            error(self.error(failure))

    def disconnect_profile(self, path, done):
        try:
            self.interface(path, "org.bluez.Device1").DisconnectProfile(
                UUID, timeout=CALL_TIMEOUT_S, reply_handler=done, error_handler=done)
        except self.dbus.DBusException:
            done()


class LiveLoop:
    """The GLib main loop, stdin and signals, in the shape Reader wants.

    An exception in a callback is written to stderr and the loop goes on, as
    the old reader's did: one device's trouble must not drop every other
    device's channel.
    """

    def __init__(self):
        from gi.repository import GLib
        from core import runtime
        self.GLib = GLib
        self.write_line = runtime.write_line
        self.main = GLib.MainLoop()
        self.exit_code = None
        self.reader = None
        self.stdin = b""

    def guard(self, callback, *args, fallback=None):
        try:
            return callback(*args)
        except Exception:
            traceback.print_exc(file=sys.stderr)
            return fallback

    def emit(self, line):
        self.write_line(line)

    def later(self, ms, callback, args, repeat):
        def fire():
            self.guard(callback, *args)
            return bool(repeat)
        return self.GLib.timeout_add(int(ms), fire)

    def cancel(self, handle):
        self.GLib.source_remove(handle)

    def watch(self, fd, callback):
        GLib = self.GLib

        def woke(_fd, condition):
            return bool(self.guard(callback, bool(condition & (GLib.IO_HUP | GLib.IO_ERR)),
                                   fallback=False))
        return GLib.io_add_watch(fd, GLib.PRIORITY_DEFAULT,
                                 GLib.IO_IN | GLib.IO_HUP | GLib.IO_ERR, woke)

    def unwatch(self, handle):
        self.GLib.source_remove(handle)

    def finish(self, code):
        if self.exit_code is None:
            self.exit_code = code
        self.main.quit()

    def now(self):
        return time.monotonic()

    def read_stdin(self, fd, _condition):
        try:
            data = os.read(fd, READ_SIZE)
        except OSError:
            data = b""
        if not data:
            self.reader.stop()
            return False
        self.stdin += data
        while b"\n" in self.stdin and not self.reader.finished:
            raw, self.stdin = self.stdin.split(b"\n", 1)
            self.guard(self.reader.handle_line, raw.decode("utf-8", "replace"))
        if len(self.stdin) > 8192:
            self.stdin = b""
        return not self.reader.finished

    def run(self, reader, addresses):
        import signal
        GLib = self.GLib
        self.reader = reader
        try:
            from gi.repository import GLibUnix
            signal_add = GLibUnix.signal_add
        except ImportError:
            signal_add = GLib.unix_signal_add
        for number in (signal.SIGTERM, signal.SIGINT):
            signal_add(GLib.PRIORITY_DEFAULT, number, lambda *_: reader.stop() or False)
        GLib.io_add_watch(0, GLib.PRIORITY_DEFAULT,
                          GLib.IO_IN | GLib.IO_HUP | GLib.IO_ERR, self.read_stdin)
        try:
            reader.start(addresses)
            if not reader.finished:
                self.main.run()
        finally:
            self.guard(reader.stopping)
        return self.exit_code or CLEAN


def endpoint(bus, reader):
    """The org.bluez.Profile1 object BlueZ calls back on."""
    import dbus.service

    class Profile(dbus.service.Object):
        @dbus.service.method("org.bluez.Profile1", in_signature="", out_signature="")
        def Release(self):
            pass

        @dbus.service.method("org.bluez.Profile1", in_signature="oha{sv}", out_signature="")
        def NewConnection(self, path, fd, _properties):
            reader.new_connection(str(path), fd.take())

        @dbus.service.method("org.bluez.Profile1", in_signature="o", out_signature="")
        def RequestDisconnection(self, path):
            reader.request_disconnection(str(path))

    return Profile(bus, PROFILE_PATH)


def main(argv):
    from core import runtime
    if argv and argv[0] in ("-h", "--help"):
        sys.stdout.write(USAGE + "\n")
        return CLEAN
    for given in argv:
        if not ADDRESS.match(given):
            runtime.write_line({"stream": False,
                                "error": "not a Bluetooth address: %s; %s" % (given, USAGE)})
            return FAILED
    runtime.arm_parent_death()
    try:
        import dbus
        import dbus.mainloop.glib
        import dbus.service  # noqa: F401
        from gi.repository import GLib  # noqa: F401
    except ImportError as error:
        runtime.write_line({"stream": False, "error": "missing python dependency %s"
                            % (getattr(error, "name", None) or error)})
        return FAILED
    try:
        dbus.mainloop.glib.DBusGMainLoop(set_as_default=True)
        try:
            bus = dbus.SystemBus()
        except dbus.DBusException as error:
            runtime.write_line({"stream": False, "error": "no system bus: %s"
                                % (error.get_dbus_message() or error)})
            return FAILED
        loop = LiveLoop()
        reader = Reader(loop, LiveBus(bus))
        endpoint(bus, reader)
        return loop.run(reader, argv)
    except KeyboardInterrupt:
        return CLEAN
    except Exception as error:
        runtime.write_line({"stream": False, "error": "%s: %s" % (type(error).__name__, error)})
        return FAILED
