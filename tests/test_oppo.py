"""OPPO Enco Air3 Pro: the frozen session, the frames, and the clock.

    python3 -m unittest tests.test_oppo

The buds answered one exact query payload, and the bridge may keep sending
only the frames that were seen to work. The answers below are the ones the
Enco Air3 Pro (product id 065C10) gave on the wire.
"""
import unittest

from tests import harness
from brands import oppo
from core.headset import MAIN, SETUP, SILENT, SetupError

ADDRESS = "28:6F:40:D9:A5:A7"

ANSWER_OFF = "aa 0c 00 00 0c 81 02 05 00 00 01 01 08 00"
ANSWER_ANC = "aa 0c 00 00 0c 81 02 05 00 00 01 01 10 00"
ANSWER_AMBIENT = "aa 0c 00 00 0c 81 02 05 00 00 01 01 00 01"
ANSWER_BATTERY = "aa 0d 00 00 06 81 03 06 00 00 02 01 50 02 50"
SET_ACK = "aa 08 00 00 04 84 21 01 00 00"

LINE_OFF = {"modes": True, "mode": "off", "available": ["off", "anc", "ambient"]}


class Session(harness.Session):
    """An Enco Air3 Pro session. "device" in a pin is whole HeyMelody frames
    as hex, AA onwards. "sent" is every frame written, as hex. "ask_mode",
    the pin's opening call, is the channel coming up: that is what asks the
    mode, with the first sequence number."""

    def __init__(self):
        super().__init__()
        self.start(oppo.Oppo, address=ADDRESS)

    def device(self, spec):
        self.receive(harness.hexbytes(spec))

    def do_ask_mode(self):
        self.headset.opened(MAIN)


harness.pin_tests(globals(), "oppo", Session)


def answered(*frames):
    """A session whose buds have answered the mode query with each frame."""
    s = Session()
    s.do_open()
    for spec in frames or (ANSWER_OFF,):
        s.device(spec)
    return s


class Frames(unittest.TestCase):
    def test_the_mode_query_is_the_one_the_buds_answer(self):
        s = Session()
        s.do_open()
        self.assertEqual(s.sent, ["aa 09 00 00 0c 01 01 02 00 01 01"])

    def test_the_battery_query(self):
        self.assertEqual(harness.hexstr(oppo.frame(oppo.BATTERY_QUERY, 0x03)),
                         "aa 07 00 00 06 01 03 00 00")

    def test_the_three_sets_are_the_observed_frames(self):
        s = answered()
        del s.host.writes[:]
        s.headset.sequence = 0x11
        for line in ("set off", "set anc", "set ambient", "set talkthru", "set", "level 3"):
            s.command(line)
        self.assertEqual(s.sent, [
            "aa 0a 00 00 04 04 11 03 00 01 01 01",
            "aa 0a 00 00 04 04 12 03 00 01 01 02",
            "aa 0a 00 00 04 04 13 03 00 01 01 04",
        ])

    def test_the_documented_set_anc_frame(self):
        self.assertEqual(harness.hexstr(oppo.frame(oppo.MODE_SET, 0x21, b"\x01\x01\x02")),
                         "aa 0a 00 00 04 04 21 03 00 01 01 02")

    def test_glued_frames_are_both_read(self):
        buffer = bytearray(harness.hexbytes(ANSWER_OFF + " " + ANSWER_BATTERY))
        frames = oppo.unframe(buffer)
        self.assertEqual([f[0] for f in frames], [oppo.MODE_ANSWER, oppo.BATTERY_ANSWER])
        self.assertEqual(frames[0], (oppo.MODE_ANSWER, 0x02, harness.hexbytes("00 01 01 08 00")))
        self.assertEqual(buffer, bytearray())

    def test_a_frame_split_anywhere_reads_once(self):
        wire = harness.hexbytes(ANSWER_BATTERY)
        for cut in range(1, len(wire)):
            with self.subTest(cut=cut):
                buffer = bytearray(wire[:cut])
                self.assertEqual(oppo.unframe(buffer), [])
                buffer += wire[cut:]
                self.assertEqual(len(oppo.unframe(buffer)), 1)
                self.assertEqual(buffer, bytearray())

    def test_noise_and_a_stray_start_byte_do_not_hold_up_a_frame(self):
        # SYNTHETIC: junk, then an AA whose lengths disagree, then a real answer.
        buffer = bytearray(harness.hexbytes("01 02 aa 30 00 00 0c 81") + harness.hexbytes(ANSWER_ANC))
        self.assertEqual(oppo.unframe(buffer),
                         [(oppo.MODE_ANSWER, 0x02, harness.hexbytes("00 01 01 10 00"))])
        self.assertEqual(buffer, bytearray())

    def test_a_long_length_is_a_varint(self):
        payload = bytes(200)
        wire = oppo.frame(oppo.MODE_ANSWER, 1, payload)
        self.assertEqual(wire[1:3], b"\xcf\x01")       # 207, in two bytes
        self.assertEqual(oppo.unframe(bytearray(wire)), [(oppo.MODE_ANSWER, 1, payload)])


class Modes(unittest.TestCase):
    def test_each_observed_window_names_its_mode(self):
        for spec, mode in ((ANSWER_OFF, "off"), (ANSWER_ANC, "anc"), (ANSWER_AMBIENT, "ambient")):
            with self.subTest(mode=mode):
                self.assertEqual(answered(spec).lines, [dict(LINE_OFF, mode=mode)])

    def test_an_unseen_window_leaves_the_mode(self):
        s = answered()
        # A bitmap position these buds never answered: the panel keeps
        # showing what it showed.
        s.receive(oppo.frame(oppo.MODE_ANSWER, 0x09, b"\x00\x01\x01\x20\x00"))
        self.assertEqual(s.lines, [LINE_OFF])
        self.assertEqual(s.headset.mode, "off")

    def test_a_window_without_its_second_byte_reads_as_00(self):
        # SYNTHETIC: never seen from these buds, which always sent both.
        self.assertEqual(oppo.mode_of(harness.hexbytes("00 01 01 10")), "anc")
        self.assertIsNone(oppo.mode_of(harness.hexbytes("00 01 01")))
        self.assertIsNone(oppo.mode_of(b"\x00"))

    def test_the_ack_asks_again(self):
        s = answered()
        del s.host.writes[:]
        s.headset.sequence = 0x21
        s.device(SET_ACK)
        self.assertEqual(s.sent, ["aa 09 00 00 0c 01 21 02 00 01 01"])

    def test_nothing_is_set_before_the_first_answer(self):
        s = Session()
        s.do_open()
        s.command("set anc")
        self.assertEqual(len(s.sent), 1)

    def test_the_sequence_counts_01_to_fe_and_wraps(self):
        s = answered()
        s.headset.sequence = 0xFE
        s.command("set anc")
        s.command("set off")
        self.assertEqual([frame[6] for frame in s.frames[-2:]], [0xFE, 0x01])


class Battery(unittest.TestCase):
    def test_the_answer_joins_the_line(self):
        s = answered()
        s.device(ANSWER_BATTERY)
        self.assertEqual(s.lines[-1], dict(LINE_OFF, battery={"left": 80, "right": 80,
                                                              "charging": []}))

    def test_it_is_asked_once_the_mode_is_known(self):
        s = Session()
        s.do_open()
        self.assertEqual(len(s.sent), 1)
        s.device(ANSWER_OFF)
        self.assertEqual(s.sent[-1], "aa 07 00 00 06 01 02 00 00")

    def test_it_waits_for_the_mode(self):
        s = Session()
        s.do_open()
        s.device(ANSWER_BATTERY)
        self.assertEqual(s.lines, [])
        s.device(ANSWER_OFF)
        self.assertEqual(s.lines[-1]["battery"]["left"], 80)

    def test_bit_7_is_charging_and_the_case_is_read(self):
        # SYNTHETIC: the case and a charging bud; the buds sent neither here.
        self.assertEqual(oppo.battery_of(harness.hexbytes("00 03 01 d0 02 50 03 64")),
                         ({"left": 80, "right": 80, "case": 100}, ["left"]))

    def test_a_level_over_100_refuses_the_answer(self):
        self.assertIsNone(oppo.battery_of(harness.hexbytes("00 02 01 50 02 65")))

    def test_short_or_unknown_answers_are_refused(self):
        for raw in ("", "00", "00 02 01 50", "00 01 07 50"):
            with self.subTest(raw=raw):
                self.assertIsNone(oppo.battery_of(harness.hexbytes(raw)))


class Clock(unittest.TestCase):
    def test_silence_after_the_query_parks_the_address(self):
        s = Session()
        s.do_open()
        s.advance(3)
        self.assertEqual(s.exit_code, SILENT)
        self.assertEqual(s.lines, [{"modes": False,
                                    "error": "the buds did not answer the listening-mode query"}])

    def test_an_answer_holds_the_deadline_off(self):
        s = answered()
        s.advance(60)
        self.assertIsNone(s.exit_code)

    def test_the_poll_asks_the_mode_and_every_tenth_time_the_battery(self):
        s = answered()
        before = len(s.sent)
        s.advance(30)
        polls = s.sent[before:]
        self.assertEqual(len(polls), 10)
        self.assertTrue(all(" 0c 01 " in frame for frame in polls[:9]))
        self.assertEqual(polls[9][:20], "aa 07 00 00 06 01 0c")

    def test_the_poll_starts_once(self):
        s = answered(ANSWER_OFF, ANSWER_ANC, ANSWER_OFF)
        self.assertEqual(s.timers, [3000, 3000])   # the deadline and one poll


class Arguments(unittest.TestCase):
    def test_the_address_is_checked_and_upper_cased(self):
        self.assertEqual(oppo.Oppo.parse([ADDRESS.lower()]), {"address": ADDRESS})

    def test_anything_else_is_setup(self):
        for args in ([], ["nope"], [ADDRESS, "extra"]):
            with self.subTest(args=args):
                with self.assertRaises(SetupError):
                    oppo.Oppo.parse(args)
        self.assertEqual(SETUP, 4)

    def test_the_link_is_the_heymelody_profile(self):
        (spec,) = Session().headset.links()
        self.assertEqual((spec.address, spec.uuid), (ADDRESS, oppo.UUID))


if __name__ == "__main__":
    unittest.main()
