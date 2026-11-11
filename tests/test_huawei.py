"""HUAWEI FreeBuds SE 2: the frozen session, the framing against captured
frames, damage the reader must survive, timing, and the ways a run ends.

    python3 -m unittest tests.test_huawei

Frames the earbuds send are verbatim from docs/captures/huawei-freebuds-se-2.txt.
Damaged bytes and failed dials are synthetic fault injection.
"""
import errno
import os
import unittest

from tests import harness
from brands import huawei
from core.headset import MAIN, SETUP, SILENT, TRANSIENT, SetupError
from core.links import Rfcomm

NAME = "HUAWEI FreeBuds SE 2"
ADDRESS = "AC:33:28:8C:A2:CD"

HELLO = "5a 00 03 00 01 06 3e bd"
EVENT_98_98_23 = "5a 00 14 00 01 27 01 01 62 02 03 62 62 17 03 03 00 00 00 04 02 14 0a 36 a2"
ANSWER_98_98_23 = "5a 00 14 00 01 08 01 01 62 02 03 62 62 17 03 03 00 00 00 04 02 14 0a c2 c7"
# The left bud going into the case, and coming out.
LID_SHUT = "5a 00 06 00 2b 5f 01 01 01 13 24"
LEFT_IN_CASE = "5a 00 14 00 01 27 01 01 61 02 03 00 61 17 03 03 00 00 00 04 02 14 0a 18 77"
LID_OPEN = "5a 00 06 00 2b 5f 01 01 00 03 05"
# Session 3: the case byte 01, then 00, with the left bud docked throughout.
CASE_BYTE_ON = "5a 00 14 00 01 27 01 01 5f 02 03 5f 60 3f 03 03 00 00 01 04 02 14 0a c2 6a"
CASE_BYTE_OFF = "5a 00 14 00 01 27 01 01 5f 02 03 5f 60 40 03 03 00 00 00 04 02 14 0a e1 0b"
CAPTURED = (HELLO, EVENT_98_98_23, ANSWER_98_98_23, LID_SHUT, LEFT_IN_CASE, LID_OPEN,
            CASE_BYTE_ON, CASE_BYTE_OFF)

QUERY = "5a 00 09 00 01 08 01 00 02 00 03 00 fb b9"


class Session(harness.Session):
    """A FreeBuds session. "device" in a pin is one read off the channel, as
    hex: a whole frame exactly as the earbuds sent it. "sent" is every frame
    written, as hex. "open" is the channel coming up, which asks for the
    battery."""

    def __init__(self, name=NAME):
        super().__init__()
        self.start(huawei.Huawei, **huawei.Huawei.parse([ADDRESS, name]))

    def device(self, spec):
        self.receive(harness.hexbytes(spec))


harness.pin_tests(globals(), "huawei", Session)


class Framing(unittest.TestCase):
    def test_the_query_is_the_one_the_earbuds_answered(self):
        self.assertEqual(harness.hexstr(huawei.BATTERY_QUERY), QUERY)

    def test_the_crc_reproduces_every_captured_frame(self):
        for text in CAPTURED:
            with self.subTest(frame=text):
                buffer = bytearray(harness.hexbytes(text))
                self.assertEqual(len(huawei.unframe(buffer)), 1)
                self.assertEqual(buffer, bytearray())

    def test_the_hello_and_two_battery_frames_in_one_read(self):
        buffer = bytearray(harness.hexbytes(" ".join((HELLO, EVENT_98_98_23, ANSWER_98_98_23))))
        frames = huawei.unframe(buffer)
        self.assertEqual([(s, c) for s, c, _ in frames], [(1, 0x06), (1, 0x27), (1, 0x08)])
        self.assertEqual(buffer, bytearray())
        self.assertEqual(huawei.battery_levels(frames[2][2]), {"left": 98, "right": 98, "case": 23})

    def test_a_real_answer_split_at_every_byte_reads_once(self):
        raw = harness.hexbytes(ANSWER_98_98_23)
        for cut in range(1, len(raw)):
            with self.subTest(cut=cut):
                buffer = bytearray(raw[:cut])
                self.assertEqual(huawei.unframe(buffer), [])
                buffer += raw[cut:]
                self.assertEqual(len(huawei.unframe(buffer)), 1)

    def test_a_bud_in_a_closed_case_reads_0_and_is_left_out(self):
        frames = huawei.unframe(bytearray(harness.hexbytes(LID_SHUT + " " + LEFT_IN_CASE)))
        self.assertEqual([(s, c) for s, c, _ in frames], [(0x2B, 0x5F), (1, 0x27)])
        self.assertEqual(huawei.battery_levels(frames[1][2]), {"right": 97, "case": 23})

    def test_the_case_byte_on_and_off_a_cable_changes_only_the_level(self):
        # The case byte is relayed by a docked bud and goes stale without
        # one, so it is not a charging state.
        for text, case in ((CASE_BYTE_ON, 63), (CASE_BYTE_OFF, 64)):
            ((_, _, body),) = huawei.unframe(bytearray(harness.hexbytes(text)))
            self.assertEqual(huawei.battery_levels(body), {"left": 95, "right": 96, "case": case})

    def test_bytes_before_a_frame_are_skipped(self):
        buffer = bytearray(b"\x00\x13\x37" + harness.hexbytes(ANSWER_98_98_23))
        self.assertEqual(len(huawei.unframe(buffer)), 1)


class Malformed(unittest.TestCase):
    """Bytes bent on purpose; none of this is something the earbuds sent."""

    def test_a_flipped_crc_byte_drops_the_frame(self):
        damaged = bytearray(harness.hexbytes(ANSWER_98_98_23))
        damaged[-1] ^= 0xFF
        self.assertEqual(huawei.unframe(damaged), [])

    def test_a_damaged_frame_does_not_hide_the_next(self):
        damaged = bytearray(harness.hexbytes(ANSWER_98_98_23))
        damaged[-2] ^= 0xFF
        buffer = bytearray(bytes(damaged) + harness.hexbytes(EVENT_98_98_23))
        self.assertEqual([(s, c) for s, c, _ in huawei.unframe(buffer)], [(1, 0x27)])

    def test_a_length_too_short_for_a_command_is_dropped(self):
        raw = bytearray(b"\x5a\x00\x01\x00")
        raw += huawei.codec.crc16_xmodem(raw).to_bytes(2, "big")
        self.assertEqual(huawei.unframe(raw), [])

    def test_a_tlv_that_runs_off_the_end_is_nothing(self):
        self.assertIsNone(huawei.battery_levels(harness.hexbytes("02 03 62 62")))
        self.assertIsNone(huawei.battery_levels(harness.hexbytes("01")))

    def test_no_tag_2_or_the_wrong_size_is_nothing(self):
        self.assertIsNone(huawei.battery_levels(harness.hexbytes("01 01 62")))
        self.assertIsNone(huawei.battery_levels(harness.hexbytes("02 02 62 62")))

    def test_a_level_over_100_is_dropped_not_clamped(self):
        self.assertIsNone(huawei.battery_levels(harness.hexbytes("02 03 65 62 17")))

    def test_the_charging_bytes_are_not_read(self):
        self.assertEqual(huawei.battery_levels(harness.hexbytes("02 03 62 62 17 03 03 01 01 01")),
                         {"left": 98, "right": 98, "case": 23})

    def test_a_frame_for_another_command_prints_nothing(self):
        s = Session()
        s.do_open()
        s.device(HELLO)
        s.device(LID_SHUT)
        self.assertEqual(s.lines, [])


class Lifecycle(unittest.TestCase):
    def opened(self):
        s = Session()
        s.do_open()
        return s

    def test_an_answer_prints_the_battery(self):
        s = self.opened()
        s.device(HELLO + " " + EVENT_98_98_23)
        self.assertEqual(s.sent, [QUERY])
        self.assertEqual(s.lines, [{"modes": True, "available": [],
                                    "battery": {"left": 98, "right": 98, "case": 23,
                                                "caseStale": False}}])

    def test_silence_through_the_deadline_is_exit_3(self):
        s = self.opened()
        s.advance(4.9)
        self.assertIsNone(s.exit_code)
        s.advance(0.1)
        self.assertEqual(s.exit_code, SILENT)
        self.assertEqual(s.lines, [{"modes": False,
                                    "error": "the earbuds did not answer the battery query"}])
        self.assertEqual(s.sent, [QUERY], "asked once, not again")

    def test_only_a_hello_is_still_silence(self):
        s = self.opened()
        s.device(HELLO)
        s.advance(5)
        self.assertEqual(s.exit_code, SILENT)

    def test_an_answer_in_time_keeps_the_run_going(self):
        s = self.opened()
        s.device(ANSWER_98_98_23)
        s.advance(300)
        self.assertIsNone(s.exit_code)

    def test_the_poll_asks_again_after_the_interval(self):
        s = self.opened()
        s.device(ANSWER_98_98_23)
        s.advance(huawei.POLL_MS / 2000)
        self.assertEqual(s.sent, [QUERY], "not before the interval")
        s.advance(huawei.POLL_MS / 2000)
        self.assertEqual(s.sent, [QUERY, QUERY])
        s.advance(huawei.POLL_MS / 1000)
        self.assertEqual(len(s.sent), 3)

    def test_an_unchanged_reading_prints_once(self):
        s = self.opened()
        s.device(EVENT_98_98_23)
        s.device(ANSWER_98_98_23)
        self.assertEqual(len(s.lines), 1)

    def test_commands_send_nothing(self):
        s = self.opened()
        s.device(ANSWER_98_98_23)
        for line in ("set anc", "set off", "level 5", "voice on", "latency on"):
            s.command(line)
        self.assertEqual(s.sent, [QUERY])

    def test_the_link_dropping_is_exit_1(self):
        s = self.opened()
        s.device(ANSWER_98_98_23)
        s.headset.closed(MAIN, None)
        self.assertEqual(s.exit_code, TRANSIENT)
        self.assertEqual(s.lines[-1], {"modes": False, "error": "the Huawei channel closed"})

    def test_a_read_error_is_exit_1_and_says_what(self):
        s = self.opened()
        s.headset.closed(MAIN, "Connection reset by peer")
        self.assertEqual(s.exit_code, TRANSIENT)
        self.assertEqual(s.lines[-1]["error"], "the Huawei channel closed: Connection reset by peer")


class Dialling(unittest.TestCase):
    def test_the_rows_channel_with_the_old_retry_budget(self):
        (spec,) = Session().headset.links()
        self.assertIsInstance(spec, Rfcomm)
        self.assertEqual((spec.address, list(spec.channels)), (ADDRESS, [1]))
        self.assertEqual((spec.attempts, spec.retry_ms, spec.timeout_s), (6, 1500, 5.0))
        self.assertIsNone(spec.probe)

    def test_refused_says_another_device_may_hold_the_channel(self):
        # What the earbuds answer while a phone's AI Life app holds channel 1.
        s = Session()
        s.headset.unavailable(MAIN, "cannot open the Huawei channel: %s"
                              % os.strerror(errno.ECONNREFUSED), TRANSIENT)
        self.assertEqual(s.exit_code, TRANSIENT)
        self.assertEqual(s.lines, [{"modes": False, "error": huawei.REFUSED}])
        self.assertIn("another device", s.lines[-1]["error"])
        self.assertIn("Huawei AI Life", s.lines[-1]["error"])

    def test_any_other_failure_says_what_it_was(self):
        s = Session()
        s.headset.unavailable(MAIN, "cannot open the Huawei channel: Connection timed out", TRANSIENT)
        self.assertEqual(s.lines[-1]["error"], "cannot open the Huawei channel: Connection timed out")

    def test_a_setup_failure_passes_through(self):
        s = Session()
        s.headset.unavailable(MAIN, "this Python has no Bluetooth sockets", SETUP)
        self.assertEqual(s.exit_code, SETUP)
        self.assertEqual(s.lines[-1]["error"], "this Python has no Bluetooth sockets")


class Arguments(unittest.TestCase):
    def test_the_row_is_picked_by_the_exact_name(self):
        options = huawei.Huawei.parse([ADDRESS.lower(), NAME])
        self.assertEqual(options["address"], ADDRESS)
        self.assertIs(options["model"], huawei.MODELS[NAME])

    def test_a_bad_address_is_setup(self):
        with self.assertRaises(SetupError):
            huawei.Huawei.parse(["not-an-address", NAME])
        self.assertEqual(SETUP, 4)

    def test_a_model_with_no_row_is_setup_and_says_so(self):
        for args in ([ADDRESS, ""], [ADDRESS, "HUAWEI FreeBuds 5i"], [ADDRESS, "TOZO NC9 Pro"],
                     [ADDRESS, "huawei freebuds se 2"], [ADDRESS]):
            with self.subTest(args=args):
                with self.assertRaises(SetupError) as raised:
                    huawei.Huawei.parse(args)
                self.assertIn("no row", str(raised.exception))

    def test_no_arguments_or_too_many_is_setup(self):
        for args in ([], [ADDRESS, NAME, "extra"]):
            with self.subTest(args=args):
                with self.assertRaises(SetupError):
                    huawei.Huawei.parse(args)


if __name__ == "__main__":
    unittest.main()
