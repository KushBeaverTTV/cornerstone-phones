"""JBL Tour Pro 3 (Airoha RACE): the frozen sessions, framing, levels, wear.

    python3 -m unittest tests.test_airoha

Every device frame in the pins is a reply or notification from
docs/captures/jbl-tour-pro-3*.txt, and every frame the bridge sends is one
the JBL app sent (Evidence checks both). Cases marked synthetic are fault
injection, not something the earbuds said.
"""
import os
import re
import unittest

from tests import harness
from brands import airoha
from core.headset import CLEAN, SILENT, TRANSIENT, SetupError

ADDRESS = "A0:11:22:33:44:55"
CAPTURE = os.path.join(harness.ROOT, "docs", "captures", "jbl-tour-pro-3.txt")
GET = "05 5a 04 00 01 09 05 00"
AVAILABLE = ["off", "anc", "ambient", "talkthru"]
REPLY_OFF = "05 5b 09 00 01 09 05 00 00 00 00 00 06"
EVENTS = {
    "off": "05 5d 08 00 01 09 05 00 00 00 1e e4",
    "anc": "05 5d 05 00 86 00 00 01 00 05 5d 08 00 01 09 05 00 00 01 07 00",
    "ambient": "05 5d 08 00 01 09 05 00 00 0a 07 00",
    "talkthru": "05 5d 08 00 01 09 05 00 00 09 00 04",
}


def line(mode, **extra):
    if mode == "ambient":
        extra.setdefault("level", 7)        # EVENTS["ambient"] reports 7
    if mode == "anc":
        extra.setdefault("strength", 7)     # EVENTS["anc"] reports 7
    return dict({"modes": True, "mode": mode, "available": AVAILABLE}, **extra)


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


class Evidence(unittest.TestCase):
    def test_every_pin_names_a_capture_that_exists(self):
        pins = harness.pins_for("airoha")
        self.assertTrue(pins)
        for _path, pin in pins:
            self.assertTrue(pin["capture"].startswith("docs/captures/jbl-tour-pro-3"))
            self.assertTrue(os.path.exists(os.path.join(harness.ROOT, pin["capture"])))

    def test_every_frame_in_the_pins_is_in_the_captures(self):
        for _path, pin in harness.pins_for("airoha"):
            # A wear pin borrows its mode answer from the main capture.
            text = ""
            for name in {pin["capture"], "docs/captures/jbl-tour-pro-3.txt"}:
                with open(os.path.join(harness.ROOT, name)) as handle:
                    text += handle.read()
            for step in pin["steps"]:
                if "device" in step:
                    for frame in re.split(r"(?=05 5[abcd] )", step["device"]):
                        if frame.strip():
                            self.assertIn(frame.strip(), text)
                for sent in step.get("sent", []) + [step.get("sent_last")]:
                    if sent:
                        self.assertIn(sent, text)

    def test_the_sets_are_the_apps_frames(self):
        with open(CAPTURE) as handle:
            text = handle.read()
        for mode, frame in airoha.SET_MODE.items():
            self.assertIn("TX " + harness.hexstr(frame), text, mode)


class Framing(unittest.TestCase):
    def feed(self, *chunks):
        s = Session()
        for chunk in chunks:
            s.device(chunk)
        return s

    def test_a_stream_split_at_every_byte_reads_the_same(self):
        stream = harness.hexbytes(" ".join([REPLY_OFF, EVENTS["anc"], EVENTS["ambient"]]))
        for cut in range(1, len(stream)):
            s = Session()
            s.receive(stream[:cut])
            s.receive(stream[cut:])
            self.assertEqual(s.lines, [line("off"), line("anc"), line("ambient")], cut)

    def test_frames_glued_together_are_all_read(self):
        self.assertEqual(self.feed(REPLY_OFF + " " + EVENTS["anc"]).lines,
                         [line("off"), line("anc")])

    def test_synthetic_noise_before_and_between_frames_is_skipped(self):
        s = self.feed("00 ff 05 05 5e " + EVENTS["ambient"] + " 05 5d ff ff " + EVENTS["off"])
        self.assertEqual(s.lines, [line("ambient"), line("off")])
        self.assertEqual(len(s.headset.buffer), 0)

    def test_a_synthetic_oversized_length_is_not_waited_for(self):
        self.assertEqual(self.feed("05 5d ff 7f " + EVENTS["talkthru"]).lines, [line("talkthru")])

    def test_a_partial_frame_is_kept_for_the_next_read(self):
        buffer = bytearray(harness.hexbytes(REPLY_OFF)[:7])
        self.assertEqual(airoha.frames(buffer), [])
        self.assertEqual(len(buffer), 7)


class NotRead(unittest.TestCase):
    def test_other_frames_the_earbuds_sent_say_nothing(self):
        s = Session()
        for frame in (
            "05 5c 06 00 01 09 65 00 00 4b",           # connect-time notification
            "05 5d 1e 00 82 00 00 01 01 00 01 01 01 01 01 01 05 06 04 00 00 00 00 00 00 01 01 01 01 01 01 01 01 01",
            "05 5d 0a 00 93 00 00 aa b1 04 02 03 01 01",  # periodic status
            "05 5b 08 00 06 0e 00 0a 01 00 00 00",     # write acknowledgement
            "05 5d 0c 00 15 0e 02 00 00 00 00 00 00 00 00 00",
            "05 5b 07 00 01 09 07 00 00 07 00",        # the level answer, no mode
            GET,                                       # an echo of the get itself
        ):
            s.device(frame)
        self.assertEqual(s.lines, [])

    def test_a_synthetic_unknown_mode_byte_prints_nothing(self):
        s = Session()
        s.device("05 5d 08 00 01 09 05 00 00 7f 07 00")
        self.assertEqual(s.lines, [])
        self.assertIsNone(s.headset.mode)

    def test_commands_it_does_not_know_send_nothing(self):
        s = Session()
        s.device(REPLY_OFF)
        for text in ("set wind", "set", "set anc extra", "level 3", "voice on", "", "anc"):
            s.command(text)
        self.assertEqual(s.sent, [])

    def test_nothing_is_sent_before_the_first_answer(self):
        s = Session()
        for text in ("set anc", "level 3", "strength 3"):
            s.command(text)
        self.assertEqual(s.sent, [])


class Wear(unittest.TestCase):
    def lines_for(self, *frames):
        s = Session()
        s.device(REPLY_OFF)
        for frame in frames:
            s.device(frame)
        return s.lines

    def test_a_wear_frame_not_of_that_exact_shape_says_nothing(self):
        for frame in (
            "05 5c 05 00 0c 00 00 02 01",       # synthetic: an ear that does not exist
            "05 5c 05 00 0c 00 00 00 02",       # synthetic: a state that is neither
            "05 5c 05 00 0c 00 01 00 01",       # synthetic: a non-zero status
            "05 5d 05 00 0c 00 00 00 01",       # synthetic: the other notification type
            "05 5c 05 00 0b 00 00 00 01",       # synthetic: another command
        ):
            self.assertEqual(self.lines_for(frame), [line("off")], frame)

    def test_both_ears_out_is_not_worn_and_one_ear_in_is_worn(self):
        out = self.lines_for("05 5c 05 00 0c 00 00 00 00", "05 5c 05 00 0c 00 00 01 00")
        self.assertIs(out[-1]["worn"], False)
        one = self.lines_for("05 5c 05 00 0c 00 00 00 00", "05 5c 05 00 0c 00 00 01 01")
        self.assertIs(one[-1]["worn"], True)

    def test_one_ear_heard_out_is_not_a_verdict(self):
        self.assertEqual(self.lines_for("05 5c 05 00 0c 00 00 00 00"), [line("off")])

    def test_worn_is_left_out_until_an_ear_has_spoken(self):
        self.assertNotIn("worn", self.lines_for()[-1])


class Levels(unittest.TestCase):
    AMBIENT = "05 5b 09 00 01 09 05 00 00 0a 04 00 06"        # synthetic: level 4

    def ambient(self):
        s = Session()
        s.device(self.AMBIENT)
        return s

    def test_the_level_is_the_one_the_earbuds_reported_in_ambient(self):
        self.assertEqual(self.ambient().lines, [line("ambient", level=4)])

    def test_synthetic_levels_outside_one_to_seven_are_not_read(self):
        for level in ("00", "08", "ff"):
            s = Session()
            s.device("05 5b 09 00 01 09 05 00 00 0a %s 00 06" % level)
            self.assertEqual(s.lines, [{"modes": True, "mode": "ambient", "available": AVAILABLE}])
            s.device("05 5b 07 00 01 09 07 00 00 %s 00" % level)
            self.assertEqual(len(s.lines), 1, level)

    def test_synthetic_level_answers_of_the_wrong_shape_say_nothing(self):
        s = self.ambient()
        for frame in (
            "05 5d 07 00 01 09 07 00 00 02 00",       # a notification, not the reply
            "05 5b 07 00 01 09 06 00 00 02 00",       # another query
            "05 5b 07 00 01 09 07 00 01 02 00",       # a non-zero status
            "05 5b 08 00 01 09 07 00 00 02 00 00",    # a longer frame
        ):
            s.device(frame)
        self.assertEqual(s.lines, [line("ambient", level=4)])

    def test_every_level_is_set_with_the_apps_frame(self):
        for level in range(1, 8):
            s = self.ambient()
            s.command("level %d" % level)
            self.assertEqual(s.sent[-1], "05 5a 06 00 06 0e 00 1e %02x 00" % level)

    def test_a_level_is_not_printed_for_having_been_sent(self):
        s = self.ambient()
        s.command("level 6")
        self.assertEqual(s.lines, [line("ambient", level=4)])

    def test_a_burst_of_levels_is_confirmed_once_after_the_last(self):
        s = self.ambient()
        for level in (2, 3, 5):
            s.command("level %d" % level)
            s.advance(0.1)
        self.assertEqual(s.timers, [airoha.READBACK_MS])
        s.advance(0.3)
        self.assertEqual(s.sent.count("05 5a 04 00 01 09 07 00"), 1)

    def test_no_read_back_once_the_run_has_stopped(self):
        s = self.ambient()
        s.command("level 6")
        s.headset.stop()
        sent = len(s.sent)
        s.fire()
        self.assertEqual(len(s.sent), sent)


class Strength(unittest.TestCase):
    ANC = "05 5b 09 00 01 09 05 00 00 01 04 00 00"           # synthetic: strength 4

    def anc(self):
        s = Session()
        s.device(self.ANC)
        return s

    def test_the_strength_is_the_one_the_earbuds_reported_in_anc(self):
        self.assertEqual(self.anc().lines, [line("anc", strength=4)])

    def test_synthetic_strengths_outside_one_to_seven_are_not_read(self):
        for level in ("00", "08", "ff"):
            s = Session()
            s.device("05 5b 09 00 01 09 05 00 00 01 %s 00 00" % level)
            self.assertEqual(s.lines, [{"modes": True, "mode": "anc", "available": AVAILABLE}])

    def test_every_strength_is_set_with_the_apps_frame(self):
        for level in range(1, 8):
            s = self.anc()
            s.command("strength %d" % level)
            self.assertEqual(s.sent[-1], "05 5a 06 00 06 0e 00 1e %02x 00" % level)

    def test_a_strength_is_not_printed_for_having_been_sent(self):
        s = self.anc()
        s.command("strength 6")
        self.assertEqual(s.lines, [line("anc", strength=4)])

    def test_neither_slider_writes_the_other_modes_setting(self):
        s = Session()
        s.device(Levels.AMBIENT)
        s.command("strength 5")
        self.assertEqual(s.sent, [])
        t = self.anc()
        t.command("level 5")
        self.assertEqual(t.sent, [])


class Lifecycle(unittest.TestCase):
    def test_silence_after_the_get_parks_the_address(self):
        s = Session()
        s.do_open()
        self.assertEqual(s.sent, [GET])
        self.assertEqual(s.timers, [airoha.ANSWER_MS])
        s.fire()
        self.assertEqual(s.exit_code, SILENT)
        self.assertEqual(s.lines, [{"modes": False,
                                    "error": "the earbuds did not answer for their mode"}])

    def test_an_answer_cancels_the_verdict(self):
        s = Session()
        s.do_open()
        s.device(REPLY_OFF)
        self.assertEqual(s.timers, [])
        s.headset.give_up_if_silent()
        self.assertIsNone(s.exit_code)

    def test_only_a_frame_that_reads_as_a_mode_counts_as_an_answer(self):
        s = Session()
        s.do_open()
        s.device("05 5d 0a 00 93 00 00 aa b1 04 02 00 01 00")
        s.device("05 5c 05 00 0c 00 00 00 01")
        s.fire()
        self.assertEqual(s.exit_code, SILENT)

    def test_a_closed_channel_is_transient(self):
        s = Session()
        s.do_open()
        s.device(REPLY_OFF)
        s.headset.closed(harness.MAIN, None)
        self.assertEqual((s.exit_code, s.lines[-1]),
                         (TRANSIENT, {"modes": False, "error": "the Airoha channel closed"}))

    def test_it_stops_once(self):
        s = Session()
        s.headset.stop()
        s.headset.fail(TRANSIENT, "late")
        self.assertEqual(s.exit_code, CLEAN)
        self.assertEqual(s.lines, [])


class Arguments(unittest.TestCase):
    def test_its_own_model_is_accepted(self):
        self.assertEqual(airoha.Airoha.parse([ADDRESS.lower(), "JBL Tour Pro 3"]),
                         {"address": ADDRESS})

    def test_bad_addresses_and_missing_names_are_setup_failures(self):
        for args in ([], [ADDRESS], ["nope", "JBL Tour Pro 3"],
                     [ADDRESS, "JBL Tour Pro 3", "extra"]):
            with self.subTest(args=args):
                with self.assertRaises(SetupError):
                    airoha.Airoha.parse(args)

    def test_only_its_own_model_has_a_row(self):
        for name in ("", "JBL Tour Pro 2", "JBL Tour Pro 3 ", "jbl tour pro 3", "JBL TUNE230NC TWS"):
            with self.subTest(name=name):
                with self.assertRaisesRegex(SetupError, "no Airoha model row"):
                    airoha.Airoha.parse([ADDRESS, name])

    def test_the_link_is_the_vendor_profile(self):
        (spec,) = Session().headset.links()
        self.assertEqual((spec.address, spec.uuid), (ADDRESS, airoha.UUID))


if __name__ == "__main__":
    unittest.main()
