"""Soundcore: the frozen sessions, the model lookup, what is read and what is
written back, and the behaviour of UNKNOWN, the one row that may change.

    python3 -m unittest tests.test_soundcore
"""
import unittest

from tests import harness
from brands import soundcore
from core.headset import SETUP, SILENT, TRANSIENT, SetupError

ADDRESS = "84:9D:4B:B0:2D:00"
IN_HEADER = bytes([0x09, 0xFF, 0x00, 0x00, 0x01])

SPACE_2 = "0cf12d31-fac3-4553-bd80-d6832e7d1402"
SPACE_ONE_PRO = "0cf12d31-fac3-4553-bd80-d6832e7b3062"
LIFE_Q30 = "0cf12d31-fac3-4553-bd80-d6832e7b302a"

QUERY = soundcore.packet(soundcore.SOUND_MODES)


def inbound(command, body=b""):
    """A device frame: the mirror of soundcore.packet."""
    size = 10 + len(body)
    raw = IN_HEADER + bytes(command) + bytes([size & 0xFF, size >> 8]) + bytes(body)
    return raw + bytes([sum(raw) & 0xFF])


def state_body(length, offset, block):
    """A 01 01 state body with the block at offset and 0x31 everywhere else:
    a plausible switch value, which is what makes a wrong offset read as a
    plausible mode, the trap PROTOCOL.md describes."""
    body = bytearray([0x31] * length)
    body[offset:offset + len(block)] = bytes(block)
    return bytes(body)


class Session(harness.Session):
    """A Soundcore session. "device" in a pin is {"cmd": "06 01", "body": hex},
    the command pair and the body, wrapped the way the device wraps them.
    "sent" is every whole frame the bridge wrote, as hex. The session's
    "uuid" is the vendor UUID, which picks the model's row. "open" is the
    channel coming up, which sends the 01 01 handshake."""

    def __init__(self, uuid=soundcore.DEFAULT_UUID):
        super().__init__()
        self.start(soundcore.Soundcore, address=ADDRESS, uuid=uuid)

    def device(self, spec):
        self.receive(inbound(harness.hexbytes(spec["cmd"]),
                             harness.hexbytes(spec.get("body", ""))))


harness.pin_tests(globals(), "soundcore", Session)


class Framing(unittest.TestCase):
    def test_our_frames(self):
        self.assertEqual(harness.hexstr(QUERY), "08 ee 00 00 00 06 01 0a 00 07")
        self.assertEqual(harness.hexstr(soundcore.packet(soundcore.SOUND_MODES_SET,
                                                         bytes.fromhex("001fff000003"))),
                         "08 ee 00 00 00 06 81 10 00 00 1f ff 00 00 03 ae")

    def test_a_frame_split_anywhere_reads_once(self):
        wire = inbound((0x06, 0x01), bytes.fromhex("00 1f ff 00 00 03"))
        for cut in range(1, len(wire)):
            with self.subTest(cut=cut):
                buffer = bytearray(wire[:cut])
                self.assertEqual(soundcore.unpack(buffer), [])
                buffer += wire[cut:]
                self.assertEqual(soundcore.unpack(buffer),
                                 [((0x06, 0x01), bytes.fromhex("001fff000003"))])

    def test_a_split_marker_is_kept(self):
        wire = inbound((0x06, 0x81))
        buffer = bytearray(b"junk junk junk" + wire[:1])
        self.assertEqual(soundcore.unpack(buffer), [])
        buffer += wire[1:]
        self.assertEqual(soundcore.unpack(buffer), [((0x06, 0x81), b"")])

    def test_a_bad_sum_is_dropped_and_the_next_frame_still_reads(self):
        bad = bytearray(inbound((0x06, 0x01), bytes.fromhex("000000000000" * 3)))
        bad[-1] ^= 0xFF
        good = inbound((0x06, 0x01), bytes.fromhex("02 50 01 01 00 05"))
        self.assertEqual(soundcore.unpack(bytearray(bytes(bad) + good)),
                         [((0x06, 0x01), bytes.fromhex("025001010005"))])

    def test_a_length_too_short_to_be_a_frame_does_not_stall_the_reader(self):
        bogus = bytes([0x09, 0xFF, 0x00, 0x00, 0x01, 0x06, 0x01, 0x00, 0x00])
        good = inbound((0x06, 0x81))
        self.assertEqual(soundcore.unpack(bytearray(bogus + good)), [((0x06, 0x81), b"")])


class ModelLookup(unittest.TestCase):
    def test_space_2_by_uuid_suffix(self):
        self.assertEqual(soundcore.model_for(SPACE_2)["name"], "Space 2")

    def test_case_insensitive(self):
        self.assertEqual(soundcore.model_for(SPACE_2.upper())["name"], "Space 2")

    def test_default_uuid_is_unknown(self):
        self.assertIs(soundcore.model_for(soundcore.DEFAULT_UUID), soundcore.UNKNOWN)

    def test_unseen_model_is_unknown(self):
        self.assertIs(soundcore.model_for("0cf12d31-fac3-4553-bd80-d6832e7ffff0"),
                      soundcore.UNKNOWN)
        self.assertIs(soundcore.model_for(None), soundcore.UNKNOWN)

    def test_every_pinned_model_has_a_row(self):
        for _path, pin in harness.pins_for("soundcore"):
            row = soundcore.model_for(pin["session"]["uuid"])
            self.assertIsNot(row, soundcore.UNKNOWN, pin["model"])


class FakeObject:
    def __init__(self, objects):
        self.objects = objects

    def GetManagedObjects(self, dbus_interface, timeout):
        assert dbus_interface == "org.freedesktop.DBus.ObjectManager"
        return self.objects


class FakeBus:
    def __init__(self, objects):
        self.objects = objects

    def get_object(self, service, path):
        assert (service, path) == ("org.bluez", "/")
        return FakeObject(self.objects)


class LiveHost(harness.FakeHost):
    """A fake host that also has the runtime's bus()."""

    def __init__(self, objects):
        super().__init__()
        self._bus = FakeBus(objects)

    def bus(self):
        return self._bus


def device(address, uuids, connected=False):
    return {"org.bluez.Device1": {"Address": address, "UUIDs": uuids, "Connected": connected}}


class TheUuid(unittest.TestCase):
    def test_a_given_uuid_is_used_as_it_is(self):
        s = Session(SPACE_ONE_PRO)
        (spec,) = s.headset.links()
        self.assertEqual((spec.address, spec.uuid), (ADDRESS, SPACE_ONE_PRO))

    def test_without_one_the_device_s_own_is_looked_up(self):
        host = LiveHost({
            "/org/bluez/hci0/dev_other": device("00:11:22:33:44:55", [SPACE_ONE_PRO]),
            "/org/bluez/hci0/dev_ours": device(ADDRESS, [
                "0000110b-0000-1000-8000-00805f9b34fb", SPACE_2.upper()]),
        })
        headset = soundcore.Soundcore(host, ADDRESS)
        (spec,) = headset.links()
        self.assertEqual(spec.uuid, SPACE_2)
        self.assertEqual(headset.model["name"], "Space 2")

    def test_the_connected_entry_wins(self):
        host = LiveHost({
            "/org/bluez/hci0/dev": device(ADDRESS, [SPACE_2]),
            "/org/bluez/hci1/dev": device(ADDRESS, [LIFE_Q30], connected=True),
        })
        self.assertEqual(soundcore.advertised_uuid(host, ADDRESS), LIFE_Q30)

    def test_no_vendor_uuid_is_the_default_and_unknown(self):
        host = LiveHost({"/d": device(ADDRESS, ["0000110b-0000-1000-8000-00805f9b34fb"])})
        headset = soundcore.Soundcore(host, ADDRESS)
        (spec,) = headset.links()
        self.assertEqual(spec.uuid, soundcore.DEFAULT_UUID)
        self.assertIs(headset.model, soundcore.UNKNOWN)

    def test_a_bus_that_fails_is_left_to_the_link(self):
        class Broken(harness.FakeHost):
            def bus(self):
                raise RuntimeError("no system bus")
        self.assertIsNone(soundcore.advertised_uuid(Broken(), ADDRESS))
        self.assertIsNone(soundcore.advertised_uuid(harness.FakeHost(), ADDRESS))


class Handshake(unittest.TestCase):
    def test_opening_asks_for_the_state(self):
        s = Session(SPACE_2)
        s.do_open()
        # Byte for byte the req_state in docs/captures/soundcore-life-q30.txt.
        self.assertEqual(s.sent, ["08 ee 00 00 00 01 01 0a 00 02"])

    def test_unanswered_handshake_is_transient(self):
        s = Session(SPACE_2)
        s.do_open()
        for _ in range(soundcore.GREET_ATTEMPTS):
            s.fire()
        self.assertEqual(len(s.sent), soundcore.GREET_ATTEMPTS)
        self.assertEqual(s.exit_code, TRANSIENT)
        self.assertEqual(s.lines[-1]["error"],
                         "the Soundcore headphones did not answer the handshake")

    def test_an_answered_handshake_leaves_no_timer(self):
        s = Session(SPACE_2)
        s.do_open()
        s.receive(inbound((0x01, 0x01), state_body(103, 71, [0x01, 0x1F, 0xFF, 0, 0, 3])))
        self.assertEqual(s.timers, [])

    def test_a_closed_channel_is_transient(self):
        s = Session(SPACE_2)
        s.headset.closed("main", "Connection reset by peer")
        self.assertEqual(s.exit_code, TRANSIENT)
        self.assertEqual(s.lines[-1]["error"],
                         "the Soundcore channel closed: Connection reset by peer")


class ShortState(unittest.TestCase):
    def test_a_short_state_on_a_row_that_does_not_ask_is_silent(self):
        s = Session(SPACE_2)
        s.receive(inbound((0x01, 0x01), bytes(40)))
        self.assertEqual(s.sent, [])
        self.assertEqual(s.exit_code, SILENT)

    def test_a_row_that_asks_waits_for_the_reply_first(self):
        # A model nobody has held with a short state, as the Life Q30 was
        # before it had a row: read at 71 there is nothing, but 06 01 can
        # still say.
        s = Session()
        s.receive(inbound((0x01, 0x01), bytes(70)))
        self.assertEqual(s.frames, [QUERY])
        self.assertIsNone(s.exit_code)
        s.receive(inbound((0x06, 0x01), bytes.fromhex("00 01 00 00")))
        self.assertEqual(s.lines[-1]["mode"], "anc")
        s.advance(10)
        self.assertIsNone(s.exit_code)

    def test_and_silence_after_that_is_silent(self):
        s = Session()
        s.receive(inbound((0x01, 0x01), bytes(70)))
        s.advance(soundcore.ANSWER_MS / 1000)
        self.assertEqual(s.exit_code, SILENT)
        self.assertEqual(s.lines, [{"modes": False,
                                    "error": "the headphones did not answer the listening-mode query"}])

    def test_a_byte_that_is_no_mode_is_not_reported(self):
        # The One Pro's state read at 71 gives 0x31 with this padding.
        s = Session()
        s.receive(inbound((0x01, 0x01), state_body(95, 69, [0x31, 0x31])))
        self.assertEqual(s.lines, [])
        s.command("set anc")
        self.assertEqual(s.frames, [QUERY])


class Unknown(unittest.TestCase):
    """A model nobody has held. Not frozen: this is the row that may change."""

    BLOCK = [0x02, 0x50, 0x01, 0x01, 0x00, 0x05]
    STATE = state_body(95, 69, BLOCK)            # the One Pro's, so 71 is wrong

    def test_the_reply_stands_over_the_offset(self):
        s = Session()
        s.receive(inbound((0x01, 0x01), self.STATE))
        self.assertEqual(s.lines[0]["mode"], "ambient")      # 71: the wrong byte
        self.assertEqual(s.frames, [QUERY])
        s.receive(inbound((0x06, 0x01), bytes(self.BLOCK)))
        self.assertEqual(s.lines[-1]["mode"], "off")         # corrected
        s.command("set anc")
        self.assertEqual(s.frames[-1], soundcore.packet(
            soundcore.SOUND_MODES_SET, bytes([0x00, 0x50, 0x01, 0x01, 0x00, 0x05])))
        self.assertEqual(s.timers, [400])

    def test_a_device_that_never_answers_06_01_is_asked_once(self):
        s = Session()
        s.receive(inbound((0x01, 0x01), state_body(103, 71, [0x01, 0x1F, 0xFF, 0, 0, 3])))
        s.command("set anc")
        self.assertEqual([f for f in s.frames if f == QUERY], [QUERY])
        self.assertEqual(s.timers, [])

    def test_a_wider_reply_goes_back_six_of_its_bytes(self):
        # A Soundcore R60i's 06 01 body, 00 51 00 00 00 00 00 00; its owner's
        # write was 01 51 00 00 00 00.
        s = Session()
        s.receive(inbound((0x06, 0x01), bytes.fromhex("0051000000000000")))
        s.command("set ambient")
        self.assertEqual(s.frames[-1], soundcore.packet(
            soundcore.SOUND_MODES_SET, bytes.fromhex("015100000000")))

    def test_a_narrower_reply_goes_back_as_wide_as_it_came(self):
        # The Life Q30's four bytes, on a model without a row: no fixed tail
        # of ours is added, which is what overwrote two of its fields once.
        s = Session()
        s.receive(inbound((0x06, 0x01), bytes.fromhex("00020000")))
        self.assertNotIn("level", s.lines[-1])
        s.command("set ambient")
        self.assertEqual(s.frames[-1], soundcore.packet(
            soundcore.SOUND_MODES_SET, bytes.fromhex("01020000")))

    def test_nothing_seen_nothing_written(self):
        s = Session()
        for line in ("set anc", "level 3", "wind on"):
            s.command(line)
        s.receive(inbound((0x06, 0x01), b"\x00"))         # too short to be a block
        s.command("set anc")
        self.assertEqual(s.frames, [])
        self.assertEqual(s.lines, [])


class FourByteBlock(unittest.TestCase):
    """The Life Q30's block is four bytes: no Ambient level, no wind switch.
    The state is the complete frame body in docs/captures/soundcore-life-q30.txt."""

    STATE = bytes.fromhex(
        "0100fefe9d93949faa8d8f7800000000000000000000000000000000000000000000"
        "010002000030352e32343330323842343634354638353045383800010101000001030407")

    def test_level_and_switch_commands_send_nothing(self):
        s = Session(LIFE_Q30)
        s.receive(inbound((0x01, 0x01), self.STATE))
        frames, lines = list(s.frames), list(s.lines)
        for line in ("level 3", "voice on", "wind on", "wind off"):
            s.command(line)
        self.assertEqual(s.frames, frames)
        self.assertEqual(s.lines, lines)
        self.assertNotIn("level", s.lines[-1])
        self.assertNotIn("voice", s.lines[-1])

    def test_the_mode_write_is_four_bytes_of_the_device_s_own(self):
        s = Session(LIFE_Q30)
        s.receive(inbound((0x01, 0x01), self.STATE))
        s.command("set ambient")
        # 35..38 of the captured state with the mode replaced; the firmware
        # string at 39 stays out of the write.
        self.assertEqual(s.frames[-1], soundcore.packet(
            soundcore.SOUND_MODES_SET, bytes([0x01]) + self.STATE[36:39]))


class SixByteBlock(unittest.TestCase):
    def ready(self):
        s = Session(SPACE_2)
        s.receive(inbound((0x01, 0x01), state_body(103, 71, [0x01, 0x1F, 0xFF, 0, 0, 3])))
        return s

    def test_level_and_wind_are_the_last_two_bytes(self):
        s = self.ready()
        self.assertEqual(s.lines[-1], {"modes": True, "mode": "ambient",
                                       "available": ["off", "anc", "ambient"],
                                       "level": 3, "voice": False})
        s.command("wind on")
        self.assertEqual(harness.hexstr(s.frames[-1]),
                         "08 ee 00 00 00 06 81 10 00 01 1f ff 00 01 03 b0")

    def test_a_set_is_not_reported_until_the_device_says(self):
        s = self.ready()
        s.command("set anc")
        self.assertEqual(s.lines[-1]["mode"], "ambient")

    def test_impossible_commands_send_nothing(self):
        s = self.ready()
        before = list(s.frames)
        for line in ("set talkthru", "set", "level 0", "level 6", "level x",
                     "wind maybe", "voice", "latency on"):
            s.command(line)
        self.assertEqual(s.frames, before)


class Arguments(unittest.TestCase):
    def test_the_address_alone(self):
        self.assertEqual(soundcore.Soundcore.parse([ADDRESS.lower()]),
                         {"address": ADDRESS, "uuid": None})

    def test_with_the_vendor_uuid(self):
        self.assertEqual(soundcore.Soundcore.parse([ADDRESS, SPACE_2.upper()]),
                         {"address": ADDRESS, "uuid": SPACE_2})

    def test_bad_arguments_are_setup(self):
        for args in ([], ["nope"], [ADDRESS, "not-a-uuid"], [ADDRESS, SPACE_2, "x"]):
            with self.subTest(args=args):
                with self.assertRaises(SetupError):
                    soundcore.Soundcore.parse(args)
        self.assertEqual(SETUP, 4)


if __name__ == "__main__":
    unittest.main()
