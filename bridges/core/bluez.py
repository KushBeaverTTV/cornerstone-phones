"""Turning a link spec (core/links.py) into a live connection.

Four kinds, one shape. Each class takes the host and its spec, and reports
back through three host calls: link_opened, link_data and link_closed, plus
link_unavailable for a link that never came up. Everything runs on the GLib
loop; the one blocking step (dialling an RFCOMM channel by number) runs on a
worker thread and hands its socket back with idle_add.

Only the runtime imports this file. It needs python-dbus and PyGObject, which
Omarchy ships; brand modules and tests never touch it.
"""
import ctypes
import os
import re
import signal
import socket
import subprocess
import threading
import time

import dbus
import dbus.service
from gi.repository import GLib

from . import links
from .headset import SETUP, SILENT, TRANSIENT

PROFILE_ROOT = "/io/cornerstone/phones"
ANSI = re.compile(r"\x1b\[[0-9;]*[A-Za-z]")
READ_SIZE = 4096
WATCH = GLib.IO_IN | GLib.IO_HUP | GLib.IO_ERR


def dbus_message(error):
    return error.get_dbus_message() or str(error)


def device_path(bus, address):
    """The BlueZ object for an address, preferring a connected one.

    A machine with two adapters lists a device once per adapter; the
    connected entry is the one a channel can be opened on.
    """
    wanted = address.upper()
    manager = dbus.Interface(bus.get_object("org.bluez", "/"),
                             "org.freedesktop.DBus.ObjectManager")
    found = None
    for path, interfaces in manager.GetManagedObjects(timeout=5).items():
        device = interfaces.get("org.bluez.Device1")
        if not device or str(device.get("Address", "")).upper() != wanted:
            continue
        if bool(device.get("Connected", False)):
            return str(path)
        found = found or str(path)
    return found


class Link:
    """What the four kinds share: a name, a stream once open, one ending."""

    def __init__(self, host, spec):
        self.host = host
        self.spec = spec
        self.name = spec.name
        self.label = host.headset.label
        self.fd = None
        self.watch = None
        self.done = False
        self.channel = None

    # -- the stream, once there is one

    def attach(self, fd):
        # Blocking on purpose: reads happen only after GLib says there is
        # something to read, and a write then goes out whole.
        self.fd = fd
        os.set_blocking(fd, True)
        self.watch = GLib.io_add_watch(fd, GLib.PRIORITY_DEFAULT, WATCH, self._readable)

    def _readable(self, _fd, condition):
        if self.done:
            return False
        if condition & GLib.IO_IN:
            try:
                data = os.read(self.fd, READ_SIZE)
            except BlockingIOError:
                return True
            except OSError as error:
                self.lost(error.strerror or str(error))
                return False
            if data:
                self.host.link_data(self, data)
                return not self.done
        self.lost(None)
        return False

    def write(self, data):
        if self.fd is None or self.done:
            return
        view = memoryview(bytes(data))
        try:
            while view:
                view = view[os.write(self.fd, view):]
        except OSError as error:
            self.lost(error.strerror or str(error))

    def lost(self, reason):
        if not self.done:
            self.done = True
            self.host.link_closed(self, reason)

    def unavailable(self, message, code=TRANSIENT):
        if not self.done:
            self.done = True
            self.host.link_unavailable(self, message, code)

    def shutdown(self):
        self.done = True
        if self.watch is not None:
            GLib.source_remove(self.watch)
            self.watch = None
        if self.fd is not None:
            try:
                os.close(self.fd)
            except OSError:
                pass
            self.fd = None


class ProfileLink(Link):
    """links.Profile: register a Profile1, ask BlueZ to connect it."""

    def __init__(self, host, spec):
        super().__init__(host, spec)
        self.path = PROFILE_ROOT + "/" + host.headset.brand
        if spec.name != links.MAIN:
            self.path += "_" + spec.name
        self.manager = None
        self.device = None
        self.device_path = None
        self.endpoint = None
        self.registered = False

    def start(self):
        bus = self.host.bus()
        try:
            self.device_path = device_path(bus, self.spec.address)
        except dbus.DBusException as error:
            return self.unavailable("cannot reach BlueZ: %s" % dbus_message(error))
        if not self.device_path:
            return self.unavailable("no paired device with address %s" % self.spec.address)
        self.device = dbus.Interface(bus.get_object("org.bluez", self.device_path),
                                     "org.bluez.Device1")
        self.manager = dbus.Interface(bus.get_object("org.bluez", "/org/bluez"),
                                      "org.bluez.ProfileManager1")
        self.endpoint = _ProfileEndpoint(bus, self.path, self)
        try:
            self.manager.RegisterProfile(self.path, self.spec.uuid, {
                "Name": "Cornerstone Phones %s" % self.label,
                "Role": "client",
                "RequireAuthentication": dbus.Boolean(False),
                "RequireAuthorization": dbus.Boolean(False),
                "AutoConnect": dbus.Boolean(False),
            }, timeout=5)
        except dbus.DBusException as error:
            # Taken: another holder of this UUID, most often a bridge for a
            # second headset of the same brand. Nothing to do but come back.
            return self.unavailable("cannot register the %s profile: %s"
                                    % (self.label, dbus_message(error)))
        self.registered = True
        GLib.timeout_add(self.spec.delay_ms, self._connect, 0)

    def _connect(self, attempt):
        if self.done or self.fd is not None:
            return False

        def failed(error):
            if self.done or self.fd is not None:
                return
            if attempt + 1 < self.spec.attempts:
                GLib.timeout_add(self.spec.retry_ms, self._connect, attempt + 1)
            else:
                self.unavailable("cannot open the %s channel: %s"
                                 % (self.label, dbus_message(error)))

        # Asynchronous on purpose: NewConnection arrives on this same loop,
        # and a blocking call would sit waiting for the connection it blocks.
        self.device.ConnectProfile(self.spec.uuid, reply_handler=lambda: None,
                                   error_handler=failed, timeout=self.spec.timeout_s)
        return False

    def connection(self, path, fd):
        if self.done or self.fd is not None or (self.device_path and path != self.device_path):
            # A second channel, or one for some other device on the same UUID:
            # not ours to hold, and holding it would double every reading.
            os.close(fd)
            return
        self.attach(fd)
        self.host.link_opened(self)

    def released(self):
        self.lost("BlueZ released the profile")

    def shutdown(self):
        super().shutdown()
        if self.registered:
            try:
                self.manager.UnregisterProfile(self.path, timeout=5)
            except dbus.DBusException:
                pass
            self.registered = False
        if self.endpoint is not None:
            try:
                self.endpoint.remove_from_connection()
            except Exception:
                pass
            self.endpoint = None


class _ProfileEndpoint(dbus.service.Object):
    """The org.bluez.Profile1 object BlueZ calls back on."""

    def __init__(self, bus, path, link):
        super().__init__(bus, path)
        self.link = link

    @dbus.service.method("org.bluez.Profile1", in_signature="", out_signature="")
    def Release(self):
        self.link.released()

    @dbus.service.method("org.bluez.Profile1", in_signature="oha{sv}", out_signature="")
    def NewConnection(self, path, fd, _properties):
        self.link.connection(str(path), fd.take())

    @dbus.service.method("org.bluez.Profile1", in_signature="o", out_signature="")
    def RequestDisconnection(self, _path):
        self.link.lost(None)


class RfcommLink(Link):
    """links.Rfcomm: dial channels by number, optionally confirming each."""

    def __init__(self, host, spec):
        super().__init__(host, spec)
        self.sock = None
        self.stopped = threading.Event()

    def start(self):
        if not hasattr(socket, "AF_BLUETOOTH"):
            return self.unavailable("this Python has no Bluetooth sockets", SETUP)
        threading.Thread(target=self._dial, daemon=True).start()

    def _dial(self):
        spec = self.spec
        last_error = None
        silent = False
        for attempt in range(spec.attempts):
            if attempt and self.stopped.wait(spec.retry_ms / 1000):
                return
            for channel in spec.channels:
                if self.stopped.is_set():
                    return
                sock = socket.socket(socket.AF_BLUETOOTH, socket.SOCK_STREAM,
                                     socket.BTPROTO_RFCOMM)
                try:
                    sock.settimeout(spec.timeout_s)
                    sock.connect((spec.address, channel))
                    leftover = b""
                    if spec.probe is not None:
                        verdict, leftover = self._confirm(sock)
                        if verdict != "yes":
                            silent = silent or verdict == "silent"
                            sock.close()
                            continue
                except OSError as error:
                    last_error = error
                    sock.close()
                    continue
                GLib.idle_add(self._connected, sock, channel, leftover)
                return
        if silent:
            GLib.idle_add(self.unavailable, None, SILENT)
        else:
            reason = (getattr(last_error, "strerror", None) or str(last_error)
                      if last_error else "no channel answered")
            GLib.idle_add(self.unavailable,
                          "cannot open the %s channel: %s" % (self.label, reason))

    def _confirm(self, sock):
        """Write the probe, wait for a reply the spec accepts.

        "yes" with the bytes after the accepted reply, "silent" for a channel
        that stayed quiet or said something else, "gone" for one that closed.
        """
        spec = self.spec
        sock.sendall(spec.probe)
        sock.settimeout(0.2)
        raw = b""
        deadline = time.monotonic() + spec.probe_timeout_s
        while time.monotonic() < deadline and not self.stopped.is_set():
            try:
                data = sock.recv(READ_SIZE)
            except socket.timeout:
                continue
            if not data:
                return "gone", b""
            raw += data
            used = spec.accepts(raw)
            if used:
                return "yes", raw[used:]
        return "silent", b""

    def _connected(self, sock, channel, leftover):
        if self.done:
            sock.close()
            return False
        sock.setblocking(True)
        self.sock = sock
        self.channel = channel
        self.attach(sock.fileno())
        self.host.link_opened(self)
        if leftover and not self.done:
            self.host.link_data(self, leftover)
        return False

    def shutdown(self):
        self.stopped.set()
        if self.watch is not None:
            GLib.source_remove(self.watch)
            self.watch = None
        self.done = True
        if self.sock is not None:
            try:
                self.sock.close()
            except OSError:
                pass
            self.sock = None
        self.fd = None


class ChildLink(Link):
    """links.Child: a helper program spoken to by lines."""

    def __init__(self, host, spec):
        super().__init__(host, spec)
        self.process = None
        self.pending = b""

    def start(self):
        try:
            self.process = subprocess.Popen(
                list(self.spec.argv), stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT, preexec_fn=_die_with_parent)
        except FileNotFoundError:
            return self.unavailable("%s is not installed" % self.spec.argv[0], SETUP)
        except OSError as error:
            return self.unavailable("cannot start %s: %s" % (self.spec.argv[0], error), SETUP)
        self.attach(self.process.stdout.fileno())
        self.host.link_opened(self)

    def _readable(self, _fd, condition):
        if self.done:
            return False
        if condition & GLib.IO_IN:
            try:
                data = os.read(self.fd, READ_SIZE)
            except BlockingIOError:
                return True
            except OSError:
                data = b""
            if data:
                self.pending += data
                while b"\n" in self.pending and not self.done:
                    raw, self.pending = self.pending.split(b"\n", 1)
                    text = ANSI.sub("", raw.decode("utf-8", "replace")).strip()
                    if text:
                        self.host.link_data(self, text)
                return not self.done
        self.lost(None)
        return False

    def write(self, text):
        if self.done or self.process is None or self.process.poll() is not None:
            return
        try:
            self.process.stdin.write((str(text) + "\n").encode())
            self.process.stdin.flush()
        except (BrokenPipeError, ValueError, OSError):
            pass

    def shutdown(self):
        if self.watch is not None:
            GLib.source_remove(self.watch)
            self.watch = None
        self.done = True
        self.fd = None
        if self.process is not None and self.process.poll() is None:
            try:
                self.process.stdin.write(b"quit\n")
                self.process.stdin.flush()
            except (BrokenPipeError, ValueError, OSError):
                pass
            try:
                self.process.wait(timeout=2)
            except subprocess.TimeoutExpired:
                self.process.kill()
                self.process.wait()


def _die_with_parent():
    libc = ctypes.CDLL("libc.so.6", use_errno=True)
    libc.prctl(1, signal.SIGTERM)


class GattLink(Link):
    """links.Gatt: subscribe to one characteristic, write to another.

    Steps, each retried as BlueZ's object tree fills in: find the device
    (scanning for it if the spec allows), connect it if it is not, find the
    service's two characteristics, StartNotify. Notifications that arrive
    before the subscription is confirmed are held and delivered after it.
    """

    TICK_MS = 250

    def __init__(self, host, spec):
        super().__init__(host, spec)
        self.bus = None
        self.path = None
        self.notify_path = None
        self.write_path = None
        self.ready = False
        self.connecting = False
        self.subscribing = False
        self.owned = False
        self.scanning = None
        self.early = []
        self.deadline = 0
        self.matches = []
        self.tick = None

    def interface(self, path, name):
        return dbus.Interface(self.bus.get_object("org.bluez", path), name)

    def start(self):
        self.bus = self.host.bus()
        self.deadline = time.monotonic() + self.spec.timeout_s
        self.matches = [
            self.bus.add_signal_receiver(
                self._changed, signal_name="PropertiesChanged",
                dbus_interface="org.freedesktop.DBus.Properties", path_keyword="path"),
            self.bus.add_signal_receiver(
                self._bearer_lost, signal_name="Disconnected",
                dbus_interface="org.bluez.Bearer.LE1", path_keyword="path"),
        ]
        self.tick = GLib.timeout_add(self.TICK_MS, self._advance)
        self._advance()

    def _objects(self):
        manager = dbus.Interface(self.bus.get_object("org.bluez", "/"),
                                 "org.freedesktop.DBus.ObjectManager")
        return manager.GetManagedObjects(timeout=2)

    def _advance(self):
        if self.done or self.ready:
            self.tick = None
            return False
        if time.monotonic() >= self.deadline:
            self.tick = None
            self._failed(None)
            return False
        try:
            objects = self._objects()
        except dbus.DBusException:
            self.tick = None
            self.unavailable("the Bluetooth service is unavailable")
            return False
        wanted = self.spec.address.upper()
        path = None
        for candidate, interfaces in objects.items():
            device = interfaces.get("org.bluez.Device1")
            if device and str(device.get("Address", "")).upper() == wanted:
                path = str(candidate)
                break
        if path is None:
            if not self.spec.scan:
                self.tick = None
                self.unavailable("%s device is unavailable in BlueZ" % self.label)
                return False
            self._scan(objects)
            return True
        self.path = path
        self._stop_scan()
        services = {str(p) for p, v in objects.items()
                    if str(p).startswith(path + "/")
                    and str(v.get("org.bluez.GattService1", {}).get("UUID", "")) == self.spec.service}
        chars = {}
        for p, v in objects.items():
            char = v.get("org.bluez.GattCharacteristic1")
            if char and str(char.get("Service", "")) in services:
                chars[str(char.get("UUID", ""))] = str(p)
        connected = bool(objects[path]["org.bluez.Device1"].get("Connected", False))
        if connected and self.spec.notify in chars and self.spec.write in chars:
            if not self.subscribing:
                self.subscribing = True
                self.notify_path = chars[self.spec.notify]
                self.write_path = chars[self.spec.write]
                self.interface(self.notify_path, "org.bluez.GattCharacteristic1").StartNotify(
                    reply_handler=self._subscribed, error_handler=self._failed, timeout=5)
        elif not self.connecting and not connected:
            self.connecting = True
            self.owned = True
            self.interface(path, "org.bluez.Device1").Connect(
                reply_handler=lambda: None, error_handler=self._failed, timeout=15)
        return True

    def _scan(self, objects):
        if self.scanning is not None:
            return
        adapter = None
        for p, v in objects.items():
            if "org.bluez.Adapter1" in v:
                adapter = str(p)
                break
        if adapter is None:
            return
        self.scanning = adapter
        try:
            self.interface(adapter, "org.bluez.Adapter1").StartDiscovery(timeout=3)
        except dbus.DBusException as error:
            self.scanning = None
            self._failed(error)

    def _stop_scan(self):
        adapter, self.scanning = self.scanning, None
        if adapter:
            try:
                self.interface(adapter, "org.bluez.Adapter1").StopDiscovery(timeout=2)
            except dbus.DBusException:
                pass

    def _subscribed(self):
        if self.done:
            return
        self.ready = True
        self.host.link_opened(self)
        held, self.early = self.early, []
        for data in held:
            if self.done:
                break
            self.host.link_data(self, data)

    def _failed(self, error):
        self.lost(dbus_message(error) if error is not None else None)

    def _changed(self, interface, values, _invalidated, path=None):
        if self.done:
            return
        if (path == self.notify_path and interface == "org.bluez.GattCharacteristic1"
                and "Value" in values):
            data = bytes(values["Value"])
            if self.ready:
                self.host.link_data(self, data)
            elif len(self.early) < 16:
                self.early.append(data)
        elif (path == self.path and interface == "org.bluez.Device1"
              and "Connected" in values and not bool(values["Connected"])):
            self._failed(None)

    def _bearer_lost(self, *_args, path=None):
        if path == self.path:
            self._failed(None)

    def write(self, data):
        if self.done or not self.ready:
            return
        self.interface(self.write_path, "org.bluez.GattCharacteristic1").WriteValue(
            dbus.Array(bytes(data), signature="y"),
            dbus.Dictionary({"type": self.spec.write_type}, signature="sv"),
            reply_handler=lambda: None, error_handler=self._failed, timeout=3)

    def shutdown(self):
        was_ready = self.ready
        self.done = True
        self.ready = False
        if self.tick is not None:
            GLib.source_remove(self.tick)
            self.tick = None
        self._stop_scan()
        for match in self.matches:
            match.remove()
        self.matches = []
        if self.bus is None:
            return
        if was_ready and self.notify_path:
            try:
                self.interface(self.notify_path, "org.bluez.GattCharacteristic1").StopNotify(timeout=1)
            except dbus.DBusException:
                pass
        if self.owned and self.path:
            try:
                self.interface(self.path, "org.bluez.Device1").Disconnect(timeout=2)
            except dbus.DBusException:
                pass


KINDS = {
    links.Profile: ProfileLink,
    links.Rfcomm: RfcommLink,
    links.Child: ChildLink,
    links.Gatt: GattLink,
}


def make(host, spec):
    return KINDS[type(spec)](host, spec)
