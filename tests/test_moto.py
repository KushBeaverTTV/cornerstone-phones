"""Moto Buds+: the frozen session, the framing, and the failure paths.

    python3 -m unittest tests.test_moto
"""
import unittest

from tests import harness
from brands import moto
from core.headset import SETUP, SILENT, SetupError

ADDRESS = "54:2F:04:11:D5:E8"


class Session(harness.Session):
    """A Moto Buds+ session. "device" in a pin is one whole packet as hex,
    HEAD to TAIL, the way the buds send it (0 in the CRC slot). "sent" is
    every packet the bridge wrote, as hex. "open" is the channel coming up."""

    def __init__(self):
        super().__init__()
        self.start(moto.Moto, address=ADDRESS, model=moto.MODELS["moto buds+"])

    def device(self, spec):
        self.receive(harness.hexbytes(spec))


harness.pin_tests(globals(), "moto", Session)


def from_buds(opcode, payload, kind=moto.RESPONSE):
    raw = moto.packet(opcode, payload, kind)
    return raw[:-8] + b"\x00\x00\x00\x00" + moto.TAIL


class Framing(unittest.TestCase):
    def test_the_battery_get_is_the_one_the_app_sends(self):
        # HEAD, length 8, opcode 00 05, request, payload length 0, seq 0,
        # crc32 over all of that, TAIL. Byte for byte what the old bridge
        # sent, which the buds took.
        self.assertEqual(
            harness.hexstr(moto.packet(moto.BATTERY)),
            "48 45 41 44 08 00 00 05 80 00 00 00 00 00 c1 58 f1 91 54 41 49 4c")

    def test_a_packet_split_anywhere_reads_once_whole(self):
        wire = from_buds(moto.MODE_GET, b"\x01\x01")
        for cut in range(1, len(wire)):
            with self.subTest(cut=cut):
                buffer = bytearray(wire[:cut])
                self.assertEqual(moto.unpack(buffer), [])
                buffer += wire[cut:]
                self.assertEqual(moto.unpack(buffer), [(moto.MODE_GET, moto.RESPONSE, b"\x01\x01")])
                self.assertEqual(buffer, bytearray())

    def test_noise_before_a_packet_is_skipped(self):
        buffer = bytearray(b"\x00\x01HE" + from_buds(moto.BATTERY, b"\x50\x51\xff"))
        self.assertEqual(moto.unpack(buffer), [(moto.BATTERY, moto.RESPONSE, b"\x50\x51\xff")])

    def test_a_length_that_lies_does_not_swallow_the_next_packet(self):
        broken = bytearray(from_buds(moto.MODE_GET, b"\x00\x00"))
        broken[10] = 3          # payload length now overruns the real TAIL
        good = from_buds(moto.MODE_CHANGED, b"\x02\x00", moto.NOTIFY)
        buffer = bytearray(bytes(broken) + good + b"padding")
        self.assertEqual(moto.unpack(buffer), [(moto.MODE_CHANGED, moto.NOTIFY, b"\x02\x00")])

    def test_a_partial_head_at_the_end_is_kept(self):
        buffer = bytearray(b"junkHEA")
        self.assertEqual(moto.unpack(buffer), [])
        self.assertEqual(bytes(buffer), b"HEA")


class Battery(unittest.TestCase):
    def test_parts_that_do_not_report_are_left_out(self):
        self.assertEqual(moto.battery_levels(b"\x5c\x5a\xff"), {"left": 92, "right": 90})
        self.assertEqual(moto.battery_levels(b"\xff\x40\x64"), {"right": 64, "case": 100})
        self.assertEqual(moto.battery_levels(b"\xff\xff\xff"), {})
        self.assertEqual(moto.battery_levels(b"\x65"), {})


class Lifecycle(unittest.TestCase):
    def test_silence_parks_the_address(self):
        s = Session()
        s.do_open()
        s.fire()                      # the second ask, then the deadline
        self.assertEqual(s.exit_code, SILENT)
        self.assertEqual(s.lines, [{"modes": False,
                                    "error": "the Moto buds did not answer the listening-mode query"}])

    def test_the_second_ask_goes_out_only_while_silent(self):
        s = Session()
        s.do_open()
        s.device(harness.hexstr(from_buds(moto.MODE_GET, b"\x01\x01")))
        before = len(s.sent)
        s.advance(2.0)
        self.assertEqual(len(s.sent), before)
        s.advance(4.0)
        self.assertIsNone(s.exit_code)

    def test_the_poll_asks_for_battery_and_mode(self):
        s = Session()
        s.do_open()
        s.device(harness.hexstr(from_buds(moto.MODE_GET, b"\x00\x00")))
        before = len(s.sent)
        s.advance(15.0)
        self.assertEqual(s.sent[before:], [harness.hexstr(moto.packet(moto.BATTERY)),
                                           harness.hexstr(moto.packet(moto.MODE_GET))])

    def test_commands_wait_for_the_first_answer(self):
        s = Session()
        s.do_open()
        before = len(s.sent)
        s.command("set anc")
        self.assertEqual(len(s.sent), before)

    def test_an_unknown_mode_byte_is_not_reported(self):
        s = Session()
        s.do_open()
        s.device(harness.hexstr(from_buds(moto.MODE_GET, b"\x07\x00")))
        self.assertEqual(s.lines, [])


class Arguments(unittest.TestCase):
    def test_the_name_is_matched_without_regard_to_case(self):
        options = moto.Moto.parse([ADDRESS.lower(), "Moto Buds+"])
        self.assertEqual(options["address"], ADDRESS)
        self.assertIs(options["model"], moto.MODELS["moto buds+"])

    def test_an_unknown_model_or_a_bad_address_is_setup(self):
        for args in ([ADDRESS, "moto buds"], ["nope", "moto buds+"], [ADDRESS]):
            with self.subTest(args=args):
                with self.assertRaises(SetupError):
                    moto.Moto.parse(args)
        self.assertEqual(SETUP, 4)

    def test_the_link_is_the_vendor_profile(self):
        s = Session()
        (spec,) = s.headset.links()
        self.assertEqual((spec.address, spec.uuid), (ADDRESS, moto.UUID))


if __name__ == "__main__":
    unittest.main()
