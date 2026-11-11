"""Xiaomi Buds 5 Pro: the frozen session, the framing, the byte never sent.

    python3 -m unittest tests.test_xiaomi
"""
import unittest

from tests import harness
from brands import xiaomi
from core.headset import SETUP, SILENT, TRANSIENT, SetupError

ADDRESS = "64:8F:DB:87:06:CB"

HANDSHAKE = "ff 03 00 00 00 0a 03 00"
HANDSHAKE_ANSWER = "ff 03 00 04 00 0a 83 00 00 03 03 01"
MODE_GET = "ff 03 00 00 00 1d 10 03"
ANSWER_ANC = "ff 03 00 05 00 1d 11 03 01 01 01 00 00"


class Session(harness.Session):
    """A Xiaomi session. "device" in a pin is a whole Compact GAIA frame as
    hex, FF onwards. "sent" is every frame the bridge wrote, as hex. "open"
    is the SPP channel coming up: the handshake goes out at once."""

    def __init__(self):
        super().__init__()
        self.start(xiaomi.Xiaomi, address=ADDRESS)

    def device(self, spec):
        self.receive(harness.hexbytes(spec))


harness.pin_tests(globals(), "xiaomi", Session)


def answering(mode_byte):
    s = Session()
    s.do_open()
    s.device(HANDSHAKE_ANSWER)
    s.device(harness.hexstr(xiaomi.frame(3, xiaomi.DEVICE, xiaomi.MODE_ANSWER,
                                         bytes([mode_byte, 1, 0, 0, 0]))))
    return s


class NeverSent(unittest.TestCase):
    def test_0x04_has_no_name(self):
        self.assertNotIn(0x04, xiaomi.MODE_BYTES.values())
        self.assertIn(0x04, xiaomi.NEVER_SEND)

    def test_0x04_is_refused_even_if_the_table_drifts(self):
        # SET 0x04 reboots the buds. Should a name for it ever creep into the
        # table, the command still sends nothing.
        xiaomi.MODE_BYTES["talkthru"] = 0x04
        try:
            s = answering(0x01)
            before = list(s.sent)
            s.command("set talkthru")
            self.assertEqual(s.sent, before)
            self.assertEqual(s.sent[-1], MODE_GET)
        finally:
            del xiaomi.MODE_BYTES["talkthru"]


class Silence(unittest.TestCase):
    def test_an_unanswered_handshake_is_transient(self):
        s = Session()
        s.do_open()
        for _ in range(xiaomi.HANDSHAKE_TRIES):
            s.fire()
        self.assertEqual(s.sent, [HANDSHAKE] * xiaomi.HANDSHAKE_TRIES)
        self.assertEqual(s.exit_code, TRANSIENT)
        self.assertEqual(s.lines, [{"modes": False,
                                    "error": "the Xiaomi buds did not answer the handshake"}])

    def test_an_unanswered_mode_query_parks_the_address(self):
        s = Session()
        s.do_open()
        s.device(HANDSHAKE_ANSWER)
        self.assertEqual(s.sent, [HANDSHAKE, MODE_GET])
        s.fire()
        self.assertEqual(s.exit_code, SILENT)
        self.assertEqual(s.lines[-1]["error"],
                         "the buds did not answer the listening-mode query")

    def test_an_answer_in_time_keeps_the_run(self):
        s = answering(0x00)
        s.advance(10)
        self.assertIsNone(s.exit_code)
        self.assertEqual(s.sent.count(MODE_GET), 1 + 4)     # the first ask, then 4 polls


class Modes(unittest.TestCase):
    def test_commands_wait_for_the_first_answer(self):
        s = Session()
        s.do_open()
        s.device(HANDSHAKE_ANSWER)
        s.command("set anc")
        self.assertEqual(s.sent, [HANDSHAKE, MODE_GET])

    def test_an_unnamed_byte_is_reported_blank(self):
        s = answering(0x03)
        self.assertEqual(s.lines, [{"modes": True, "mode": "", "available": xiaomi.AVAILABLE}])

    def test_a_repeated_answer_prints_nothing_new(self):
        s = answering(0x01)
        s.device(ANSWER_ANC)
        self.assertEqual(len(s.lines), 1)

    def test_the_version_the_buds_use_is_the_one_sent(self):
        s = Session()
        s.do_open()
        s.device("ff 02 00 04 00 0a 83 00 00 03 03 01")
        self.assertEqual(s.sent[-1], "ff 02 00 00 00 1d 10 03")


class Framing(unittest.TestCase):
    def test_a_frame_split_anywhere_reads_once_whole(self):
        wire = harness.hexbytes(ANSWER_ANC)
        for cut in range(1, len(wire)):
            with self.subTest(cut=cut):
                buffer = bytearray(wire[:cut])
                self.assertEqual(xiaomi.unframe(buffer), [])
                buffer += wire[cut:]
                self.assertEqual(xiaomi.unframe(buffer),
                                 [(3, xiaomi.DEVICE, xiaomi.MODE_ANSWER, b"\x01\x01\x01\x00\x00")])
                self.assertEqual(buffer, bytearray())

    def test_two_frames_in_one_read(self):
        buffer = bytearray(harness.hexbytes(HANDSHAKE_ANSWER + " " + ANSWER_ANC))
        self.assertEqual([f[2] for f in xiaomi.unframe(buffer)],
                         [xiaomi.HANDSHAKE_ANSWER, xiaomi.MODE_ANSWER])

    def test_noise_and_nonzero_flags_are_skipped(self):
        buffer = bytearray(b"\x00\x12" + b"\xff\x03\x01\x00" + harness.hexbytes(ANSWER_ANC))
        self.assertEqual(xiaomi.unframe(buffer),
                         [(3, xiaomi.DEVICE, xiaomi.MODE_ANSWER, b"\x01\x01\x01\x00\x00")])


class Arguments(unittest.TestCase):
    def test_the_address_is_checked_and_upper_cased(self):
        self.assertEqual(xiaomi.Xiaomi.parse([ADDRESS.lower()]), {"address": ADDRESS})
        for args in ([], ["nope"], [ADDRESS, "extra"]):
            with self.subTest(args=args):
                with self.assertRaises(SetupError):
                    xiaomi.Xiaomi.parse(args)
        self.assertEqual(SETUP, 4)

    def test_the_link_is_standard_spp(self):
        s = Session()
        (spec,) = s.headset.links()
        self.assertEqual((spec.address, spec.uuid),
                         (ADDRESS, "00001101-0000-1000-8000-00805f9b34fb"))


if __name__ == "__main__":
    unittest.main()
