"""JBL Tour Pro 3 over Airoha RACE.

    python3 -m unittest tests.test_airoha

The pins carry the observed conversations; Provenance holds them to the
captures they came from. Everything else here is the edges: how the byte
stream is cut into frames, which frames are deliberately not read, the wear
rule, the two dials that share one write, and how a run starts and ends.
A frame tagged "synthetic" was made up for the test; untagged ones were seen.
"""
import os
import re
import unittest

from tests import harness
from brands import airoha
from core.headset import CLEAN, SILENT, TRANSIENT, SetupError

ADDRESS = "A0:11:22:33:44:55"
CAPTURE_DIR = os.path.join(harness.ROOT, "docs", "captures")
MAIN_CAPTURE = "docs/captures/jbl-tour-pro-3.txt"
MODES = ["off", "anc", "ambient", "talkthru"]

# Seen on the earbuds (main capture, section 3).
ASK = "05 5a 04 00 01 09 05 00"
OFF_REPLY = "05 5b 09 00 01 09 05 00 00 00 00 00 06"
OFF_EVENT = "05 5d 08 00 01 09 05 00 00 00 1e e4"
# An unrelated 0086 notice and the ANC event, as they arrived in one read.
ANC_EVENT_PAIR = "05 5d 05 00 86 00 00 01 00 05 5d 08 00 01 09 05 00 00 01 07 00"
AMBIENT_EVENT = "05 5d 08 00 01 09 05 00 00 0a 07 00"
TALKTHRU_EVENT = "05 5d 08 00 01 09 05 00 00 09 00 04"
SEEN_LEVEL = 7        # what the ANC and Ambient events above carry


def expected(mode, level=None, strength=None, worn=None):
    """The line the bridge should print for this state."""
    out = {"modes": True, "mode": mode, "available": MODES}
    for key, value in (("level", level), ("strength", strength), ("worn", worn)):
        if value is not None:
            out[key] = value
    return out


def wear(ear, inside):
    return "05 5c 05 00 0c 00 00 %02x %02x" % (ear, 1 if inside else 0)


class Session(harness.Session):
    """A Tour Pro 3 session. "device" in a pin is whole or partial RACE frames
    as hex, arriving as one read. "sent" is every frame written, as hex.
    "open" is the channel coming up: it sends the mode get and starts the
    answer deadline, which the first mode answer cancels."""

    def __init__(self):
        super().__init__()
        self.start(airoha.Airoha, address=ADDRESS)

    def device(self, spec):
        self.receive(harness.hexbytes(spec))


harness.pin_tests(globals(), "airoha", Session)


def after(*frames):
    """A session that has been told these frames, one read each."""
    s = Session()
    for frame in frames:
        s.device(frame)
    return s


def capture_text(*names):
    text = []
    for name in set(names):
        with open(os.path.join(harness.ROOT, name)) as handle:
            text.append(handle.read())
    return "\n".join(text)


def frames_of(pin):
    """(what the pin has the device say, what it expects the bridge to send)."""
    heard, written = [], []
    for step in pin["steps"]:
        # One "device" step may hold several frames glued together.
        heard += [f.strip() for f in re.split(r"(?=05 5[abcd] )", step.get("device", "")) if f.strip()]
        written += step.get("sent", [])
        if step.get("sent_last"):
            written.append(step["sent_last"])
    return heard, written


class Provenance(unittest.TestCase):
    def test_each_pin_points_at_a_tour_pro_3_capture_on_disk(self):
        pins = harness.pins_for("airoha")
        self.assertTrue(pins)
        for path, pin in pins:
            with self.subTest(pin=os.path.basename(path)):
                self.assertRegex(pin["capture"], r"^docs/captures/jbl-tour-pro-3")
                self.assertTrue(os.path.isfile(os.path.join(harness.ROOT, pin["capture"])))

    def test_no_pin_uses_a_frame_its_captures_do_not_contain(self):
        for path, pin in harness.pins_for("airoha"):
            # The wear and level pins open on a mode answer from the main capture.
            text = capture_text(pin["capture"], MAIN_CAPTURE)
            heard, written = frames_of(pin)
            for frame in heard + written:
                with self.subTest(pin=os.path.basename(path), frame=frame):
                    self.assertIn(frame, text)

    def test_the_mode_sets_are_the_ones_the_app_transmitted(self):
        text = capture_text(MAIN_CAPTURE)
        for mode, frame in airoha.SET_MODE.items():
            with self.subTest(mode=mode):
                self.assertIn("TX " + harness.hexstr(frame), text)


class Stream(unittest.TestCase):
    """Cutting the RFCOMM byte stream into RACE frames."""

    def test_where_a_read_ends_does_not_matter(self):
        wire = harness.hexbytes(" ".join((OFF_REPLY, ANC_EVENT_PAIR, AMBIENT_EVENT)))
        want = [expected("off"), expected("anc", strength=SEEN_LEVEL),
                expected("ambient", level=SEEN_LEVEL)]
        for cut in range(1, len(wire)):
            with self.subTest(cut=cut):
                s = Session()
                s.receive(wire[:cut])
                s.receive(wire[cut:])
                self.assertEqual(s.lines, want)

    def test_two_frames_in_one_read_are_both_read(self):
        s = after(OFF_REPLY + " " + ANC_EVENT_PAIR)
        self.assertEqual(s.lines, [expected("off"), expected("anc", strength=SEEN_LEVEL)])

    def test_synthetic_garbage_around_frames_is_skipped_and_nothing_lingers(self):
        s = after("00 ff 05 05 5e " + AMBIENT_EVENT + " 05 5d ff ff " + OFF_EVENT)
        self.assertEqual(s.lines, [expected("ambient", level=SEEN_LEVEL), expected("off")])
        self.assertEqual(bytes(s.headset.buffer), b"")

    def test_a_synthetic_huge_length_is_skipped_not_awaited(self):
        self.assertEqual(after("05 5d ff 7f " + TALKTHRU_EVENT).lines, [expected("talkthru")])

    def test_an_unfinished_frame_waits_for_the_rest(self):
        pending = bytearray(harness.hexbytes(OFF_REPLY)[:7])
        self.assertEqual(airoha.frames(pending), [])
        self.assertEqual(len(pending), 7)


class Ignored(unittest.TestCase):
    """Frames and commands the bridge deliberately does nothing with."""

    UNREAD = (
        "05 5c 06 00 01 09 65 00 00 4b",                         # a notice at connect
        "05 5d 1e 00 82 00 00 01 01 00 01 01 01 01 01 01 05 06 04 00 00"
        " 00 00 00 00 01 01 01 01 01 01 01 01 01",
        "05 5d 0a 00 93 00 00 aa b1 04 02 03 01 01",             # periodic status
        "05 5b 08 00 06 0e 00 0a 01 00 00 00",                   # a set's echo
        "05 5d 0c 00 15 0e 02 00 00 00 00 00 00 00 00 00",
        "05 5b 07 00 01 09 07 00 00 07 00",                      # level answer: no mode in it
        ASK,                                                     # our own get, echoed
    )

    def test_seen_frames_that_carry_no_mode_print_nothing(self):
        self.assertEqual(after(*self.UNREAD).lines, [])

    def test_a_synthetic_mode_byte_nobody_has_seen_is_not_a_mode(self):
        s = after("05 5d 08 00 01 09 05 00 00 7f 07 00")
        self.assertEqual(s.lines, [])
        self.assertIsNone(s.headset.mode)

    def test_lines_that_are_not_our_commands_write_nothing(self):
        s = after(OFF_REPLY)
        for text in ("set wind", "set", "set anc extra", "level 3", "voice on", "", "anc"):
            s.command(text)
        self.assertEqual(s.sent, [])

    def test_until_the_earbuds_answer_no_command_is_written(self):
        s = Session()
        for text in ("set anc", "level 3", "strength 3"):
            s.command(text)
        self.assertEqual(s.sent, [])


class Wear(unittest.TestCase):
    def worn_after(self, *notices):
        """The last line's worn value (None if absent) after an Off answer."""
        return after(OFF_REPLY, *notices).lines[-1].get("worn")

    def test_both_known_and_out_is_not_worn(self):
        self.assertIs(self.worn_after(wear(0, False), wear(1, False)), False)

    def test_one_in_is_enough_to_be_worn(self):
        self.assertIs(self.worn_after(wear(0, False), wear(1, True)), True)

    def test_one_ear_out_and_the_other_unheard_says_nothing_yet(self):
        s = after(OFF_REPLY, wear(0, False))
        self.assertEqual(s.lines, [expected("off")])

    def test_no_wear_key_before_any_ear_speaks(self):
        self.assertIsNone(self.worn_after())

    def test_synthetic_near_misses_of_the_wear_notice_are_ignored(self):
        for notice in ("05 5c 05 00 0c 00 00 02 01",     # ear 2
                       "05 5c 05 00 0c 00 00 00 02",     # state 2
                       "05 5c 05 00 0c 00 01 00 01",     # status 1
                       "05 5d 05 00 0c 00 00 00 01",     # type 5d
                       "05 5c 05 00 0b 00 00 00 01"):    # command 000b
            with self.subTest(notice=notice):
                self.assertEqual(after(OFF_REPLY, notice).lines, [expected("off")])


class Dial:
    """What the Ambient level and the ANC strength have in common: the same
    1e write, read back rather than trusted, numbers 1 to 7 only. A subclass
    names its mode, its command and what its mode answer looks like."""

    mode = verb = key = None
    mode_byte = None

    def mode_answer(self, value):
        return "05 5b 09 00 01 09 05 00 00 %s %s 00 06" % (self.mode_byte, value)

    def at_four(self):
        return after(self.mode_answer("04"))       # synthetic: the dial at 4

    def test_the_answer_puts_the_figure_on_the_line(self):
        self.assertEqual(self.at_four().lines, [expected(self.mode, **{self.key: 4})])

    def test_synthetic_figures_outside_1_to_7_are_left_off(self):
        for value in ("00", "08", "ff"):
            with self.subTest(value=value):
                self.assertEqual(after(self.mode_answer(value)).lines, [expected(self.mode)])

    def test_each_figure_goes_out_as_the_apps_1e_frame(self):
        for value in range(1, 8):
            with self.subTest(value=value):
                s = self.at_four()
                s.command("%s %d" % (self.verb, value))
                self.assertEqual(s.sent[-1], "05 5a 06 00 06 0e 00 1e %02x 00" % value)

    def test_writing_a_figure_does_not_print_it(self):
        s = self.at_four()
        s.command("%s 6" % self.verb)
        self.assertEqual(s.lines, [expected(self.mode, **{self.key: 4})])


class AmbientLevel(Dial, unittest.TestCase):
    mode, verb, key, mode_byte = "ambient", "level", "level", "0a"
    LEVEL_GET = "05 5a 04 00 01 09 07 00"

    def test_synthetic_level_answers_outside_1_to_7_are_ignored(self):
        for value in ("00", "08", "ff"):
            with self.subTest(value=value):
                s = after(self.mode_answer(value), "05 5b 07 00 01 09 07 00 00 %s 00" % value)
                self.assertEqual(len(s.lines), 1)

    def test_synthetic_lookalikes_of_the_level_answer_are_ignored(self):
        s = self.at_four()
        for frame in ("05 5d 07 00 01 09 07 00 00 02 00",     # notified, not replied
                      "05 5b 07 00 01 09 06 00 00 02 00",     # query 06
                      "05 5b 07 00 01 09 07 00 01 02 00",     # status 1
                      "05 5b 08 00 01 09 07 00 00 02 00 00"):  # one byte too long
            s.device(frame)
        self.assertEqual(s.lines, [expected("ambient", level=4)])

    def test_dragging_the_dial_asks_once_after_the_last_step(self):
        s = self.at_four()
        for value in (2, 3, 5):
            s.command("level %d" % value)
            s.advance(0.1)
        self.assertEqual(s.timers, [airoha.READBACK_MS])
        s.advance(0.3)
        self.assertEqual(s.sent.count(self.LEVEL_GET), 1)

    def test_a_stopped_run_does_not_ask(self):
        s = self.at_four()
        s.command("level 6")
        s.headset.stop()
        before = list(s.sent)
        s.fire()
        self.assertEqual(s.sent, before)

    def test_the_strength_command_is_refused_in_ambient(self):
        s = self.at_four()
        s.command("strength 5")
        self.assertEqual(s.sent, [])


class AncStrength(Dial, unittest.TestCase):
    mode, verb, key, mode_byte = "anc", "strength", "strength", "01"

    def test_the_level_command_is_refused_in_anc(self):
        s = self.at_four()
        s.command("level 5")
        self.assertEqual(s.sent, [])


class Lifecycle(unittest.TestCase):
    def opened(self):
        s = Session()
        s.do_open()
        return s

    def test_opening_asks_once_and_arms_the_deadline(self):
        s = self.opened()
        self.assertEqual((s.sent, s.timers), ([ASK], [airoha.ANSWER_MS]))

    def test_no_answer_by_the_deadline_is_silent(self):
        s = self.opened()
        s.fire()
        self.assertEqual(s.exit_code, SILENT)
        self.assertEqual(s.lines, [{"modes": False,
                                    "error": "the earbuds did not answer for their mode"}])

    def test_the_first_answer_disarms_the_deadline(self):
        s = self.opened()
        s.device(OFF_REPLY)
        self.assertEqual(s.timers, [])
        s.headset.give_up_if_silent()
        self.assertIsNone(s.exit_code)

    def test_frames_that_are_not_a_mode_do_not_count_as_an_answer(self):
        s = self.opened()
        s.device("05 5d 0a 00 93 00 00 aa b1 04 02 00 01 00")
        s.device(wear(0, True))
        s.fire()
        self.assertEqual(s.exit_code, SILENT)

    def test_losing_the_channel_is_transient(self):
        s = self.opened()
        s.device(OFF_REPLY)
        s.headset.closed(harness.MAIN, None)
        self.assertEqual(s.exit_code, TRANSIENT)
        self.assertEqual(s.lines[-1], {"modes": False, "error": "the Airoha channel closed"})

    def test_a_clean_stop_is_not_overridden(self):
        s = Session()
        s.headset.stop()
        s.headset.fail(TRANSIENT, "late")
        self.assertEqual((s.exit_code, s.lines), (CLEAN, []))


class Arguments(unittest.TestCase):
    def parse(self, *args):
        return airoha.Airoha.parse(list(args))

    def test_the_tour_pro_3_is_taken_and_its_address_normalised(self):
        self.assertEqual(self.parse(ADDRESS.lower(), "JBL Tour Pro 3"), {"address": ADDRESS})

    def test_wrong_arity_or_address_is_a_setup_error(self):
        for args in ((), (ADDRESS,), ("nope", "JBL Tour Pro 3"),
                     (ADDRESS, "JBL Tour Pro 3", "extra")):
            with self.subTest(args=args):
                self.assertRaises(SetupError, self.parse, *args)

    def test_the_name_must_match_the_row_exactly(self):
        for name in ("", "JBL Tour Pro 2", "JBL Tour Pro 3 ", "jbl tour pro 3", "JBL TUNE230NC TWS"):
            with self.subTest(name=name):
                with self.assertRaisesRegex(SetupError, "no Airoha model row"):
                    self.parse(ADDRESS, name)

    def test_it_dials_the_vendor_uuid(self):
        (spec,) = Session().headset.links()
        self.assertEqual((spec.address, spec.uuid), (ADDRESS, airoha.UUID))


if __name__ == "__main__":
    unittest.main()
