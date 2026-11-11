"""The Fast Pair reader: owners' Message Stream frames, the supervisor, the
process contract.

    python3 -m unittest tests.test_fastpair

Real nonblocking pipes stand in for the RFCOMM sockets, so partial reads,
glued frames and end-of-stream are the kernel's. BlueZ is FakeBus and time is
the harness's FakeHost clock: no Bluetooth, D-Bus or GLib, and nothing sleeps.
The frames marked as an owner's come from tests/fixtures/canonical.json.
"""
import io
import json
import os
import subprocess
import sys
import unittest
from contextlib import redirect_stdout
from unittest import mock

from tests import harness
import fastpair
from fastpair import BusError

with open(os.path.join(harness.HERE, "fixtures", "canonical.json")) as handle:
    OWNERS = json.load(handle)

SONY = OWNERS["sony"]["address"]        # a WH-CH720N: one battery
JBL = OWNERS["jbl"]["address"]          # earbuds: left, right, case


def owner_frame(brand, label):
    return harness.hexbytes(OWNERS[brand]["gfps"][label]["hex"])


def path_of(address):
    return "/org/bluez/hci0/dev_" + address.replace(":", "_")


class FakeLoop(harness.FakeHost):
    """The harness host, plus fd watches."""

    def __init__(self):
        super().__init__()
        self.watches = {}
        self.next_watch = 0

    def watch(self, fd, callback):
        self.next_watch += 1
        self.watches[self.next_watch] = (fd, callback)
        return self.next_watch

    def unwatch(self, handle):
        del self.watches[handle]


class FakeBus:
    """BlueZ as Reader sees it. Calls out are recorded with their handlers,
    and answered only when a test answers them."""

    def __init__(self, known=(SONY, JBL)):
        self.paths = {address: path_of(address) for address in known}
        self.calls = []             # (kind, path, handlers...)
        self.register_error = None
        self.registered = 0
        self.unregistered = 0
        self.bluez = ":1.10"
        self.owner_callback = None
        self.lookup_error = None

    def owner(self):
        return self.bluez

    def watch_owner(self, callback):
        self.owner_callback = callback

    def device_path(self, address):
        if self.lookup_error is not None:
            raise self.lookup_error
        return self.paths.get(address)

    def register(self):
        self.registered += 1
        if self.register_error is not None:
            raise self.register_error

    def unregister(self):
        self.unregistered += 1

    def connect_profile(self, path, reply, error):
        self.calls.append(("connect", path, reply, error))

    def disconnect_profile(self, path, done):
        self.calls.append(("disconnect", path, done))

    def kinds(self):
        return [call[0] for call in self.calls]

    def answer(self, *args):
        """Answer the last call: a connect's reply (or, with a BusError, its
        error), a disconnect's done."""
        call = self.calls[-1]
        if call[0] == "connect":
            (call[3] if args else call[2])(*args)
        else:
            call[2](*args)


class Rig:
    """A Reader on fakes, started with `addresses`."""

    def __init__(self, addresses=(), bus=None, start=True):
        self.loop = FakeLoop()
        self.bus = bus or FakeBus()
        self.reader = fastpair.Reader(self.loop, self.bus)
        self.pipes = []
        if start:
            self.reader.start(addresses)

    @property
    def lines(self):
        return self.loop.lines

    def timers(self):
        return [t[0] for t in self.loop.timers]

    def device(self, address):
        return self.reader.devices[address]

    def socket(self, address):
        """BlueZ hands over a channel for this device; returns the far end."""
        incoming, outgoing = os.pipe()
        self.pipes.append(outgoing)
        self.reader.new_connection(path_of(address), incoming)
        return outgoing

    def close(self):
        for device in self.reader.devices.values():
            device.reading.quiet = True
            device.close_channel("test over")
        for fd in self.pipes:
            try:
                os.close(fd)
            except OSError:
                pass


class Base(unittest.TestCase):
    def rig(self, addresses=(), **kwargs):
        rig = Rig(addresses, **kwargs)
        self.addCleanup(rig.close)
        return rig

    def stream(self, rig, address):
        far = rig.socket(address)
        return rig.device(address).channel, far

    def send(self, channel, far, data):
        os.write(far, data)
        self.assertTrue(channel.ready(False))


# ---- the frames

class Frames(Base):
    def test_owner_battery_frames_name_only_the_parts_that_exist(self):
        for brand, address in (("sony", SONY), ("jbl", JBL)):
            with self.subTest(brand=brand):
                rig = self.rig([address])
                channel, far = self.stream(rig, address)
                self.send(channel, far, owner_frame(brand, "battery"))
                line = rig.lines[-1]
                self.assertEqual(line["address"], address)
                self.assertIs(line["stream"], True)
                parts = (line["single"], line["battery"], line["left"], line["right"], line["case"])
                if brand == "sony":
                    self.assertEqual(parts, (True, 39, -1, -1, -1))
                else:
                    self.assertEqual(parts, (False, -1, 70, 80, 67))
                self.assertFalse(any(line[k] for k in ("batteryCharging", "leftCharging",
                                                       "rightCharging", "caseCharging")))

    def test_a_reading_carries_every_key_in_order(self):
        rig = self.rig([JBL])
        channel, far = self.stream(rig, JBL)
        self.send(channel, far, b"".join(owner_frame("jbl", k) for k in ("model", "ble", "battery"))
                  + harness.hexbytes("03 09 00 05") + b"1.0.5")
        self.assertEqual(list(rig.lines[-1]), [
            "address", "stream", "single", "battery", "batteryCharging",
            "left", "leftCharging", "right", "rightCharging", "case", "caseCharging",
            "modelId", "bleAddress", "firmware"])
        self.assertEqual(rig.lines[-1]["firmware"], "1.0.5")

    def test_every_split_point_waits_for_the_whole_owner_frame(self):
        for brand, address in (("sony", SONY), ("jbl", JBL)):
            frame = owner_frame(brand, "battery")
            for split in range(1, len(frame)):
                with self.subTest(brand=brand, split=split):
                    rig = self.rig([address])
                    channel, far = self.stream(rig, address)
                    self.send(channel, far, frame[:split])
                    self.assertEqual(rig.lines, [])
                    self.send(channel, far, frame[split:])
                    self.assertEqual(len(rig.lines), 1)
                    self.assertEqual(channel.buffer, bytearray())

    def test_glued_model_ble_and_battery_frames_keep_every_field(self):
        for brand, address, model, ble in (("sony", SONY, "f42ffc", "CA:D0:19:9C:8C:D3"),
                                           ("jbl", JBL, "71f20a", "64:3B:E9:EB:E3:0B")):
            with self.subTest(brand=brand):
                rig = self.rig([address])
                channel, far = self.stream(rig, address)
                self.send(channel, far, b"".join(owner_frame(brand, k)
                                                 for k in ("model", "ble", "battery")))
                self.assertEqual(rig.lines[-1]["modelId"], model)
                self.assertEqual(rig.lines[-1]["bleAddress"], ble)
                self.assertEqual(channel.buffer, bytearray())

    def test_unknown_groups_and_lengths_not_read_are_ignored(self):
        rig = self.rig([JBL])
        channel, far = self.stream(rig, JBL)
        for data in ("04 03 00 01 64", "03 03 00 00", "03 03 00 02 64 64",
                     "03 03 00 04 64 64 64 64", "03 02 00 05 01 02 03 04 05"):
            self.send(channel, far, harness.hexbytes(data))
        self.assertEqual(rig.lines, [])
        self.send(channel, far, owner_frame("jbl", "battery"))
        self.assertEqual(rig.lines[-1]["left"], 70)

    def test_a_level_above_100_is_no_level(self):
        # 0xFE is the only battery byte a Redmi Buds 5 sent in one session
        # (docs/captures/redmi-buds-5-fastpair.txt); the neighbours are synthetic.
        for byte in (0xFE, 0x7E, 101, 0xE5, 0x80 | 101):
            self.assertEqual(fastpair.component(byte), (-1, False), hex(byte))
        self.assertEqual(fastpair.component(100), (100, False))
        self.assertEqual(fastpair.component(0x80 | 100), (100, True))

    def test_charging_and_no_reading(self):
        # Protocol boundaries, not claims about what an owner captured.
        for byte, expected in [(0, (0, False)), (100, (100, False)), (0x80, (0, True)),
                               (0xE4, (100, True)), (0x7F, (-1, False)), (0xFF, (-1, False))]:
            self.assertEqual(fastpair.component(byte), expected, hex(byte))
        rig = self.rig([JBL])
        channel, far = self.stream(rig, JBL)
        self.send(channel, far, harness.hexbytes("03 03 00 03 ff e4 7f"))
        line = rig.lines[-1]
        self.assertEqual((line["left"], line["right"], line["case"]), (-1, 100, -1))
        self.assertIs(line["rightCharging"], True)

    def test_a_single_byte_above_100_is_unreported_not_126_percent(self):
        rig = self.rig([JBL])
        channel, far = self.stream(rig, JBL)
        self.send(channel, far, harness.hexbytes("03 03 00 01 fe"))
        line = rig.lines[-1]
        self.assertIs(line["single"], True)
        self.assertEqual((line["battery"], line["batteryCharging"]), (-1, False))

    def test_the_two_byte_battery_a_redmi_buds_5_sent_is_not_read(self):
        # docs/captures/redmi-buds-5-fastpair.txt: `94 94`, which would read as
        # 20% charging on both buds while the app showed 65 / 65.
        rig = self.rig([JBL])
        channel, far = self.stream(rig, JBL)
        self.send(channel, far, harness.hexbytes("03 03 00 02 94 94"))
        self.assertEqual(rig.lines, [])

    def test_a_single_battery_leaves_the_three_parts_unreported(self):
        self.assertEqual(fastpair.battery_values(b"\xa7"), {
            "single": True, "battery": 39, "batteryCharging": True,
            "left": -1, "leftCharging": False, "right": -1, "rightCharging": False,
            "case": -1, "caseCharging": False})


# ---- one device's lines

class Lines(Base):
    def test_one_devices_outage_cannot_touch_the_other(self):
        rig = self.rig([SONY, JBL])
        sony, sf = self.stream(rig, SONY)
        jbl, jf = self.stream(rig, JBL)
        self.send(sony, sf, owner_frame("sony", "battery"))
        self.send(jbl, jf, owner_frame("jbl", "battery"))
        before = dict(jbl.reading.values)
        sony.close("link lost")
        self.assertEqual(jbl.reading.values, before)
        self.assertTrue(jbl.alive)
        self.assertEqual(rig.lines[-1], {"address": SONY, "stream": False, "error": "link lost"})

    def test_a_repeat_is_quiet_but_a_refresh_reannounces_the_same_battery(self):
        rig = self.rig([JBL])
        channel, far = self.stream(rig, JBL)
        battery = owner_frame("jbl", "battery")
        self.send(channel, far, battery)
        self.send(channel, far, battery)
        self.assertEqual(len(rig.lines), 1)
        channel.reading.announce_again()
        self.send(channel, far, owner_frame("jbl", "model"))
        self.assertTrue(channel.reading.repeat, "the model id must not spend it")
        self.send(channel, far, battery)
        self.assertEqual(len(rig.lines), 3)
        self.assertFalse(channel.reading.repeat)
        self.send(channel, far, battery)
        self.assertEqual(len(rig.lines), 3)

    def test_a_channel_back_clears_the_error_and_keeps_the_reading(self):
        rig = self.rig([SONY])
        channel, far = self.stream(rig, SONY)
        self.send(channel, far, owner_frame("sony", "battery"))
        channel.reading.stream_down("lost")
        channel.reading.stream_down("lost")
        self.assertEqual(len(rig.lines), 2, "the same outage is said once")
        channel.reading.stream_up()
        channel.reading.announce_again()
        self.send(channel, far, owner_frame("sony", "battery"))
        self.assertEqual(len(rig.lines), 3)
        self.assertIs(rig.lines[-1]["stream"], True)
        self.assertNotIn("error", rig.lines[-1])
        self.assertEqual(rig.lines[-1]["battery"], 39)

    def test_after_a_printed_outage_the_next_frame_is_printed_even_unchanged(self):
        # The shell's last line for the device said stream false. A new channel
        # announcing the figures it had before must still say stream true.
        rig = self.rig([SONY])
        channel, far = self.stream(rig, SONY)
        self.send(channel, far, owner_frame("sony", "model") + owner_frame("sony", "battery"))
        channel.close("message stream ended")
        self.assertIs(rig.lines[-1]["stream"], False)
        channel, far = self.stream(rig, SONY)
        self.send(channel, far, owner_frame("sony", "model"))
        self.assertIs(rig.lines[-1]["stream"], True)
        self.assertEqual(rig.lines[-1]["battery"], 39)
        count = len(rig.lines)
        self.send(channel, far, owner_frame("sony", "battery"))
        self.assertEqual(len(rig.lines), count)

    def test_a_hangup_reads_the_last_battery_then_closes_once(self):
        rig = self.rig([JBL])
        channel, far = self.stream(rig, JBL)
        closed = []
        channel.on_close = lambda: closed.append(True)
        os.write(far, owner_frame("jbl", "battery"))
        self.assertFalse(channel.ready(True))
        self.assertEqual(rig.lines[0]["case"], 67)
        self.assertEqual(rig.lines[1], {"address": JBL, "stream": False,
                                        "error": "message stream closed by the device"})
        self.assertFalse(channel.alive)
        self.assertIsNone(channel.watch)
        self.assertEqual(rig.loop.watches, {})
        channel.close("again")
        self.assertEqual(closed, [True])
        self.assertEqual(len(rig.lines), 2)

    def test_a_frame_cut_short_by_the_hangup_is_never_a_reading(self):
        # Synthetic damage: the owner's frame less its last byte.
        rig = self.rig([SONY])
        channel, far = self.stream(rig, SONY)
        os.write(far, owner_frame("sony", "battery")[:-1])
        channel.ready(True)
        self.assertEqual(rig.lines, [{"address": SONY, "stream": False,
                                      "error": "message stream closed by the device"}])

    def test_the_far_end_closing_is_the_stream_ending(self):
        rig = self.rig([SONY])
        channel, far = self.stream(rig, SONY)
        os.write(far, owner_frame("sony", "battery"))
        os.close(far)
        rig.pipes.remove(far)
        self.assertFalse(channel.ready(False))
        self.assertEqual(rig.lines[-1]["error"], "message stream ended")
        self.assertEqual(rig.lines[0]["battery"], 39)

    def test_a_frame_that_breaks_the_decoder_closes_only_that_channel(self):
        rig = self.rig([SONY])
        channel, far = self.stream(rig, SONY)
        with mock.patch.object(fastpair, "decode", side_effect=ValueError("bad")):
            os.write(far, owner_frame("sony", "battery"))
            self.assertFalse(channel.ready(False))
        self.assertEqual(rig.lines[-1]["error"], "ValueError: bad")
        self.assertEqual(rig.timers()[-1], fastpair.CLOSED_MS)


# ---- the supervisor

class Supervisor(Base):
    def attached(self):
        rig = self.rig([SONY])
        device = rig.device(SONY)
        self.assertEqual(device.path, path_of(SONY))
        return rig, device

    def test_a_found_device_is_looked_at_300_ms_later(self):
        rig, _ = self.attached()
        self.assertEqual(sorted(rig.timers()), [fastpair.FIRST_LOOK_MS, fastpair.STEADY_MS])
        rig.loop.advance(0.3)
        self.assertEqual(rig.bus.kinds(), ["connect"])
        self.assertEqual(rig.bus.calls[0][1], path_of(SONY))

    def test_a_retry_waits_for_the_disconnect_answer_before_connecting(self):
        rig, device = self.attached()
        device.supervise()
        self.assertEqual(rig.bus.kinds(), ["connect"])
        device.supervise()
        self.assertEqual(rig.bus.kinds(), ["connect"], "one call out at a time")
        device.calling = False
        device.supervise()
        self.assertEqual(rig.bus.kinds(), ["connect", "disconnect"])
        rig.bus.answer()
        self.assertEqual(rig.bus.kinds(), ["connect", "disconnect", "connect"])

    def test_already_connected_is_never_reported_and_the_fourth_failure_is(self):
        rig, device = self.attached()
        for name, text in (("org.bluez.Error.AlreadyConnected", ""),
                           ("org.bluez.Error.InProgress", ""),
                           ("org.bluez.Error.Failed", "br-connection-already-connected"),
                           ("org.bluez.Error.Failed", " Already Connected ")):
            self.assertTrue(fastpair.already_connected(BusError(name, text)))
        self.assertFalse(fastpair.already_connected(BusError("org.bluez.Error.Failed",
                                                             "already connected-ish")))
        busy = BusError("org.bluez.Error.Failed", "br-connection-busy")

        def attempt(error):
            device.supervise()
            if rig.bus.calls[-1][0] == "disconnect":
                rig.bus.answer()            # the drop, then the connect
            self.assertEqual(rig.bus.calls[-1][0], "connect")
            rig.bus.answer(error)

        for number in range(1, 4):
            attempt(busy)
            self.assertEqual(device.attempts, number)
        self.assertEqual(rig.bus.kinds(), ["connect"] + ["disconnect", "connect"] * 2)
        self.assertEqual(rig.lines, [], "three failed attempts stay quiet")
        attempt(BusError("org.bluez.Error.AlreadyConnected", "x"))
        self.assertEqual(rig.lines, [])
        attempt(busy)
        self.assertEqual(rig.lines, [{"address": SONY, "stream": False,
                                      "error": "cannot connect the message stream: br-connection-busy"}])

    def test_an_answer_brings_the_next_look_forward_for_four_attempts(self):
        rig, device = self.attached()
        rig.loop.timers = []
        device.supervise()
        rig.bus.answer()
        self.assertEqual(rig.timers(), [fastpair.ANSWER_MS])
        rig.loop.timers = []
        device.attempts = fastpair.QUICK_ATTEMPTS
        device.calling = False
        device.connect_failed(BusError("org.bluez.Error.Failed", "x"))
        self.assertEqual(rig.timers(), [])

    def test_a_socket_is_taken_and_a_live_channel_stops_the_attempts(self):
        rig, device = self.attached()
        device.attempts = 3
        rig.socket(SONY)
        self.assertEqual(device.attempts, 0)
        rig.bus.calls = []
        device.supervise()
        self.assertEqual(rig.bus.calls, [])

    def test_a_closed_channel_is_looked_at_4_s_later(self):
        rig, device = self.attached()
        rig.socket(SONY)
        rig.loop.timers = []
        device.channel.close("gone")
        self.assertEqual(rig.timers(), [fastpair.CLOSED_MS])
        rig.loop.advance(4)
        self.assertEqual(rig.bus.kinds(), ["connect"])

    def test_a_second_socket_replaces_the_first_quietly(self):
        rig, device = self.attached()
        rig.socket(SONY)
        first = device.channel
        rig.socket(SONY)
        self.assertFalse(first.alive)
        self.assertTrue(device.channel.alive)
        self.assertEqual(rig.lines, [])
        self.assertFalse(device.reading.quiet)
        self.assertEqual(len(rig.loop.watches), 1)

    def test_refresh_is_quiet_keeps_the_reading_and_cycles_only_its_profile(self):
        rig = self.rig([SONY, JBL])
        device = rig.device(SONY)
        device.reading.values["battery"] = 39
        rig.socket(SONY)
        rig.reader.handle_line("refresh " + SONY.lower())
        self.assertTrue(device.reading.repeat)
        self.assertEqual(device.reading.values["battery"], 39)
        self.assertFalse(device.reading.quiet)
        self.assertIsNone(device.channel)
        self.assertEqual(rig.lines, [])
        self.assertEqual(rig.bus.kinds(), ["disconnect"])
        self.assertEqual(rig.bus.calls[0][1], path_of(SONY))
        rig.bus.answer(None)        # refused or not, the connect follows
        self.assertEqual(rig.bus.kinds(), ["disconnect", "connect"])
        self.assertEqual(device.attempts, 0)

    def test_refresh_waits_while_a_call_is_out(self):
        rig, device = self.attached()
        device.supervise()
        rig.reader.handle_line("refresh " + SONY)
        self.assertEqual(rig.bus.kinds(), ["connect"])
        self.assertFalse(device.reading.repeat)

    def test_an_unfollowed_device_cannot_be_reconnected_by_old_timers(self):
        rig, device = self.attached()
        rig.socket(SONY)
        rig.reader.handle_line("unfollow " + SONY)
        self.assertNotIn(SONY, rig.reader.devices)
        self.assertEqual(rig.bus.kinds(), ["disconnect"])
        self.assertEqual(rig.lines, [], "no parting line")
        before = list(rig.bus.calls)
        device.supervise()
        device.refresh()
        rig.loop.advance(60)
        self.assertEqual(rig.bus.calls, before)
        self.assertTrue(device.reading.quiet)

    def test_the_steady_check_covers_every_device_every_20_s(self):
        rig = self.rig([SONY, JBL])
        for address in (SONY, JBL):
            rig.device(address).attempts = 1        # past the first, quick look
        rig.loop.timers = [t for t in rig.loop.timers if t[0] == fastpair.STEADY_MS]
        rig.loop.advance(20)
        self.assertEqual(sorted(call[1] for call in rig.bus.calls),
                         sorted([path_of(SONY), path_of(JBL)]))
        self.assertEqual(rig.timers(), [fastpair.STEADY_MS])


class Lookup(Base):
    def test_an_unknown_device_asked_for_is_reported_at_once(self):
        rig = self.rig()
        rig.reader.handle_line("follow 11:22:33:44:55:66")
        self.assertEqual(rig.lines, [{"address": "11:22:33:44:55:66", "stream": False,
                                      "error": "no paired device with address 11:22:33:44:55:66"}])
        self.assertEqual(rig.timers(), [fastpair.STEADY_MS, fastpair.LOOKUP_MS])

    def test_after_a_restart_the_third_failed_lookup_is_the_first_said(self):
        bus = FakeBus()
        rig = self.rig([SONY], bus=bus)
        bus.lookup_error = BusError("org.freedesktop.DBus.Error.ServiceUnknown", "gone")
        device = rig.device(SONY)
        device.attach(False)
        device.attach(False)
        self.assertEqual(rig.lines, [])
        self.assertEqual(rig.timers().count(fastpair.LOOKUP_MS), 1, "once only")
        device.attach(False)
        self.assertEqual(rig.lines[-1]["error"], "cannot reach BlueZ: gone")
        bus.lookup_error = None
        rig.loop.advance(5)
        self.assertEqual(device.path, path_of(SONY))
        self.assertEqual(device.lookups, 0)


# ---- the process

class Process(Base):
    def test_sockets_go_to_the_device_their_path_names(self):
        rig = self.rig([SONY, JBL])
        far = rig.socket(JBL)
        os.write(far, owner_frame("jbl", "battery"))
        rig.device(JBL).channel.ready(False)
        self.assertEqual(rig.lines[-1]["address"], JBL)
        self.assertIsNone(rig.device(SONY).channel)

    def test_a_socket_for_a_device_nobody_follows_is_closed(self):
        rig = self.rig([SONY])
        incoming, outgoing = os.pipe()
        self.addCleanup(os.close, outgoing)
        rig.reader.new_connection(path_of(JBL), incoming)
        with self.assertRaises(OSError):
            os.fstat(incoming)
        rig.reader.new_connection("/org/bluez/hci0", os.dup(outgoing))
        self.assertEqual(rig.lines, [])

    def test_bluez_asking_for_the_channel_back_is_an_outage(self):
        rig = self.rig([SONY])
        rig.socket(SONY)
        rig.reader.request_disconnection(path_of(SONY))
        self.assertEqual(rig.lines, [{"address": SONY, "stream": False,
                                      "error": "BlueZ asked for the channel back"}])
        rig.reader.request_disconnection(path_of(JBL))
        self.assertEqual(len(rig.lines), 1)

    def test_commands(self):
        rig = self.rig()
        for junk in ("", "follow", "follow nope", "follow %s extra" % SONY, "follo " + SONY,
                     "unfollow " + SONY, "refresh " + SONY):
            rig.reader.handle_line(junk)
        self.assertEqual(rig.reader.devices, {})
        rig.reader.handle_line("  follow %s \n" % SONY.lower())
        first = rig.device(SONY)
        rig.reader.handle_line("follow " + SONY)
        self.assertIs(rig.device(SONY), first, "following twice does nothing")
        self.assertEqual(first.path, path_of(SONY))

    def test_command_line_addresses_are_followed_once_each(self):
        rig = self.rig([SONY.lower(), SONY])
        self.assertEqual(list(rig.reader.devices), [SONY])
        self.assertEqual(rig.bus.registered, 1)

    def test_a_start_with_nothing_followed_is_idle(self):
        rig = self.rig()
        self.assertIsNone(rig.loop.finished)
        self.assertEqual(rig.lines, [])

    def test_a_refused_registration_at_start_ends_the_run(self):
        for name, words in (("org.bluez.Error.AlreadyExists", "another program"),
                            ("org.bluez.Error.Failed", "cannot register")):
            with self.subTest(name=name):
                bus = FakeBus()
                bus.register_error = BusError(name, "UUID already registered")
                rig = self.rig([SONY], bus=bus)
                self.assertEqual(rig.loop.finished, 1)
                (line,) = rig.lines
                self.assertEqual(set(line), {"stream", "error"})
                self.assertIs(line["stream"], False)
                self.assertTrue(line["error"].startswith("cannot register the Fast Pair profile"))
                self.assertIn(words, line["error"])
                self.assertTrue(line["error"].endswith("UUID already registered"))
                self.assertIsNone(rig.device(SONY).path)

    def test_bluetoothd_leaving_and_coming_back(self):
        bus = FakeBus()
        rig = self.rig([SONY], bus=bus)
        bus.owner_callback(":1.10")             # the watch's first call: no change
        self.assertEqual(bus.registered, 1)
        rig.socket(SONY)
        bus.bluez = None
        bus.owner_callback("")
        device = rig.device(SONY)
        self.assertEqual(rig.lines, [{"address": SONY, "stream": False,
                                      "error": "bluetoothd went away"}])
        self.assertIsNone(device.path)
        self.assertFalse(rig.reader.live)
        rig.reader.handle_line("follow " + JBL)
        self.assertIsNone(rig.device(JBL).path, "nothing to attach to yet")
        bus.bluez = ":1.20"
        bus.owner_callback(":1.20")
        self.assertEqual(bus.registered, 2)
        self.assertEqual(device.path, path_of(SONY))
        self.assertEqual(rig.device(JBL).path, path_of(JBL))

    def test_a_failing_registration_is_said_on_the_third_try_once_then_cleared(self):
        bus = FakeBus()
        rig = self.rig([SONY], bus=bus)
        bus.register_error = BusError("org.bluez.Error.Failed", "no adapter")
        bus.owner_callback(":1.30")
        self.assertEqual(rig.lines, [])
        rig.loop.advance(5)
        self.assertEqual(rig.lines, [])
        rig.loop.advance(5)
        reason = "cannot register the Fast Pair profile: no adapter"
        self.assertEqual(rig.lines, [{"stream": False, "error": reason}])
        rig.loop.advance(5)
        self.assertEqual(len(rig.lines), 1, "said once")
        self.assertEqual(rig.timers().count(fastpair.LOOKUP_MS), 1)
        bus.register_error = None
        rig.loop.advance(5)
        self.assertEqual(rig.lines[-1], {"stream": True})
        self.assertIsNone(rig.loop.finished)

    def test_a_taken_registration_on_a_retry_counts_as_ours(self):
        bus = FakeBus()
        rig = self.rig([SONY], bus=bus)
        bus.owner_callback("")
        bus.register_error = BusError("org.bluez.Error.NotPermitted", "taken")
        bus.owner_callback(":1.40")
        self.assertTrue(rig.reader.live)
        self.assertEqual(rig.device(SONY).path, path_of(SONY))

    def test_stop_and_unregister(self):
        rig = self.rig([SONY])
        rig.reader.stop()
        self.assertEqual(rig.loop.finished, 0)
        rig.reader.stopping()
        self.assertEqual(rig.bus.unregistered, 1)

    def test_device_paths(self):
        self.assertEqual(fastpair.address_from_path("/org/bluez/hci0/dev_78_5e_A2_76_7F_20"), JBL)
        self.assertEqual(fastpair.address_from_path("/org/bluez/hci1/dev_78_5E_A2_76_7F_20/sep1"), JBL)
        for path in ("/org/bluez/hci0", "/org/bluez/hci0/dev_78_5E", "/dev_zz_5E_A2_76_7F_20"):
            self.assertIsNone(fastpair.address_from_path(path), path)


class CommandLine(unittest.TestCase):
    ENTRY = os.path.join(harness.BRIDGES, "cornerstone-fastpair")

    def run_entry(self, *args):
        env = dict(os.environ, PYTHONDONTWRITEBYTECODE="1")
        return subprocess.run([sys.executable, self.ENTRY] + list(args), capture_output=True,
                              text=True, env=env, stdin=subprocess.DEVNULL, timeout=10)

    def test_help_is_plain_text_and_exit_0(self):
        done = self.run_entry("--help")
        self.assertEqual((done.returncode, done.stdout), (0, fastpair.USAGE + "\n"))

    def test_a_bad_address_is_a_line_without_an_address_and_exit_1(self):
        done = self.run_entry(SONY, "nope")
        self.assertEqual(done.returncode, 1)
        self.assertEqual(json.loads(done.stdout), {
            "stream": False,
            "error": "not a Bluetooth address: nope; " + fastpair.USAGE})

    def test_a_missing_dependency_is_said_on_stdout(self):
        out = io.StringIO()
        real_import = __import__

        def no_dbus(name, *args, **kwargs):
            if name == "dbus" or name.startswith("dbus."):
                raise ImportError("No module named 'dbus'", name="dbus")
            return real_import(name, *args, **kwargs)

        with mock.patch("core.runtime.arm_parent_death"), \
                mock.patch("builtins.__import__", no_dbus), redirect_stdout(out):
            code = fastpair.main([SONY])
        self.assertEqual(code, 1)
        self.assertEqual(json.loads(out.getvalue()),
                         {"stream": False, "error": "missing python dependency dbus"})


if __name__ == "__main__":
    unittest.main()
