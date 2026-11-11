"""Bose QC45 and QC35: the frozen sessions, the framing, the init probe,
the [31.3] -> [1.6] fallback, and the clock.

    python3 -m unittest tests.test_bose

Frames marked SYNTHETIC are built to exercise a branch; every other byte is
from docs/captures/bose-qc45.txt or docs/captures/bose-qc35.txt.
"""
import unittest

from tests import harness
from brands import bose
from core.headset import MAIN, SETUP, SILENT, TRANSIENT, SetupError

ADDRESS = "AC:BF:71:64:56:B9"

INIT_REPLY = "00 01 03 05 31 2e 31 2e 30"     # [0.1] STATUS "1.1.0", the QC45
QC35_INIT_REPLY = "00 01 03 05 31 2e 30 2e 34"  # "1.0.4"

QC45_BATTERY = "02 02 03 04 5a ff ff 00"      # 90%
AWARE = "1f 03 03 01 01"                      # [31.3] STATUS idx 1
QUIET = "1f 03 03 01 00"                      # [31.3] STATUS idx 0
PROCESSING = "1f 03 07 00"

MODES_ERROR = "1f 03 04 01 03"                # [31.3] ERROR: no audio modes here
NOISE_LOW = "01 06 03 02 01 0b"               # [1.6] STATUS: value 1, mask 0b1011
NOISE_HIGH = "01 06 03 02 03 0b"
NOISE_OFF = "01 06 03 02 00 0b"
NOISE_REFUSED = "01 06 04 01 06"              # [1.6] ERROR: a value outside the mask
QC35_BATTERY = "02 02 03 01 46"               # 70%, one byte
# What the QC35 sent unasked after the init: [5.1] STATUS and [4.2] RESULT.
CHATTER = "05 01 03 09 00 02 01 00 00 00 00 00 00" " 04 02 06 06 08 b4 d2 34 88 65"


class Session(harness.Session):
    """A Bose session. "device" in a pin is BMAP bytes as hex, any number of
    frames or part of one, as the socket would deliver them. "sent" is every
    frame written on the channel, as hex. "open" is the channel coming up
    with the [0.1] init already answered (the link's probe does that before
    the headset hears of it). "fire" is one poll, as it was in the old
    bridge's Session: the poll timer coming due and nothing else."""

    def __init__(self):
        super().__init__()
        self.start(bose.Bose, address=ADDRESS)

    def device(self, spec):
        self.receive(harness.hexbytes(spec))

    def fire(self):
        self.headset.poll()


harness.pin_tests(globals(), "bose", Session)


def qc45():
    """A QC45 session that has answered its first two questions: Aware, 90%."""
    s = Session()
    s.do_open()
    s.device(QC45_BATTERY)
    s.device(AWARE)
    return s


def qc35(*frames):
    """A session on the [1.6] path, as far as the QC35 pin's first steps."""
    s = Session()
    s.do_open()
    s.device(QC35_BATTERY)
    s.device(MODES_ERROR)
    for frame in frames or (NOISE_LOW,):
        s.device(frame)
    return s


class Framing(unittest.TestCase):
    """The length byte is the framer, and nothing is guessed."""

    def test_the_init_answer_parses(self):
        self.assertEqual(bose.take_frames(bytearray(harness.hexbytes(INIT_REPLY))),
                         [(bose.VERSION, bose.STATUS, b"1.1.0")])

    def test_our_frames_are_the_ones_the_headsets_took(self):
        self.assertEqual(harness.hexstr(bose.INIT), "00 01 01 00")
        self.assertEqual(harness.hexstr(bose.ASK_BATTERY), "02 02 01 00")
        self.assertEqual(harness.hexstr(bose.ASK_AUDIO_MODE), "1f 03 01 00")
        self.assertEqual(harness.hexstr(bose.ASK_NOISE), "01 06 01 00")
        self.assertEqual(harness.hexstr(bose.start_audio_mode(1)), "1f 03 05 02 01 00")
        self.assertEqual(harness.hexstr(bose.set_noise(3)), "01 06 02 01 03")

    def test_a_reply_split_at_every_byte_reads_once(self):
        for reply in (AWARE, NOISE_HIGH, QC45_BATTERY):
            wire = harness.hexbytes(reply)
            for cut in range(1, len(wire)):
                with self.subTest(reply=reply, cut=cut):
                    buffer = bytearray(wire[:cut])
                    self.assertEqual(bose.take_frames(buffer), [])
                    buffer += wire[cut:]
                    self.assertEqual(len(bose.take_frames(buffer)), 1)
                    self.assertEqual(buffer, bytearray())

    def test_two_answers_in_one_read_are_both_taken(self):
        buffer = bytearray(harness.hexbytes(QC45_BATTERY + " " + AWARE))
        self.assertEqual([f[0] for f in bose.take_frames(buffer)], [bose.BATTERY, bose.AUDIO_MODE])
        self.assertEqual(buffer, bytearray())

    def test_a_length_that_has_not_arrived_waits(self):
        buffer = bytearray(harness.hexbytes("1f 03 03 05 01 01"))
        self.assertEqual(bose.take_frames(buffer), [])
        buffer += harness.hexbytes("00 00 00")
        self.assertEqual(len(bose.take_frames(buffer)), 1)

    def test_a_truncated_header_waits(self):
        buffer = bytearray(b"\x1f\x03")
        self.assertEqual(bose.take_frames(buffer), [])
        self.assertEqual(buffer, bytearray(b"\x1f\x03"))

    def test_the_chatter_the_qc35_sends_unasked_is_stepped_over(self):
        s = Session()
        s.do_open()
        s.device(MODES_ERROR)
        s.device(CHATTER + " " + NOISE_LOW)
        self.assertEqual(s.headset.mode, "anc")
        self.assertEqual(s.lines[-1]["ancLevel"], "low")
        self.assertIsNone(s.exit_code)


class InitProbe(unittest.TestCase):
    """What links.Rfcomm is told counts as BMAP answering on a channel."""

    def test_the_link_dials_8_then_2_then_9_and_probes_with_the_init(self):
        s = Session()
        (spec,) = s.headset.links()
        self.assertEqual(spec.address, ADDRESS)
        self.assertEqual(tuple(spec.channels), (8, 2, 9))
        self.assertEqual(spec.probe, harness.hexbytes("00 01 01 00"))
        self.assertIs(spec.accepts, bose.init_answered)
        self.assertEqual((spec.attempts, spec.retry_ms, spec.timeout_s, spec.probe_timeout_s),
                         (10, 1500, 10.0, 1.0))

    def test_both_versions_seen_are_accepted_whole(self):
        for reply in (INIT_REPLY, QC35_INIT_REPLY):
            raw = harness.hexbytes(reply)
            self.assertEqual(bose.init_answered(raw), len(raw))

    def test_an_echo_an_error_or_a_malformed_version_is_not_bmap(self):
        for raw in ("00 01 01 00", "00 01 04 01 01", "00 01 03 01 00", "00 01 03 03 31 2e 31"):
            with self.subTest(raw=raw):
                self.assertFalse(bose.init_answered(harness.hexbytes(raw)))

    def test_a_split_init_is_waited_for_and_what_follows_is_left(self):
        init, after = harness.hexbytes(INIT_REPLY), harness.hexbytes(QUIET)
        for cut in range(1, len(init)):
            with self.subTest(cut=cut):
                self.assertFalse(bose.init_answered(init[:cut]))
                raw = init + after[:3]
                self.assertEqual(raw[bose.init_answered(raw):], after[:3])

    def test_frames_before_the_init_answer_are_consumed_with_it(self):
        raw = harness.hexbytes("00 01 01 00 " + INIT_REPLY + " " + QUIET)
        self.assertEqual(raw[bose.init_answered(raw):], harness.hexbytes(QUIET))

    def test_what_followed_the_init_is_read_after_the_open(self):
        # The core delivers the bytes after the accepted reply as the first
        # received data, once opened() has run: here, half a mode answer
        # the probe read, then the rest from the socket.
        quiet = harness.hexbytes(QUIET)
        s = Session()
        s.do_open()
        s.receive(quiet[:3])
        s.receive(quiet[3:])
        self.assertEqual(s.lines[-1]["mode"], "anc")

    def test_channels_that_opened_but_never_answered_park_the_address(self):
        s = Session()
        s.headset.unavailable(MAIN, None, SILENT)
        self.assertEqual(s.exit_code, SILENT)
        self.assertEqual(s.lines, [{"modes": False,
                                    "error": "the headset answered on no Bose BMAP channel"}])

    def test_a_channel_that_never_opened_is_transient(self):
        s = Session()
        s.headset.unavailable(MAIN, "cannot open the Bose channel: Connection refused", TRANSIENT)
        self.assertEqual(s.exit_code, TRANSIENT)
        self.assertEqual(s.lines[-1]["error"], "cannot open the Bose channel: Connection refused")


class AudioModes(unittest.TestCase):
    """The QC45: Quiet and Aware, set by START, settled by the readback."""

    def test_a_set_sends_the_start_and_waits_for_the_device_to_say(self):
        s = qc45()
        first = dict(s.lines[-1])
        s.command("set anc")
        self.assertEqual(s.sent[-1], "1f 03 05 02 00 00")
        s.device(PROCESSING)
        self.assertEqual(s.lines[-1], first)
        self.assertEqual(s.headset.mode, "ambient")
        s.fire()
        s.device(QUIET)
        self.assertEqual(s.lines[-1]["mode"], "anc")

    def test_off_talkthru_levels_and_switches_are_never_sent(self):
        s = qc45()
        for command in ("set off", "set talkthru", "level 5", "level high",
                        "voice on", "latency off", "set", "bogus"):
            s.command(command)
        self.assertEqual(s.sent, ["02 02 01 00", "1f 03 01 00"])

    def test_an_unnamed_custom_slot_is_not_forced_into_a_name(self):
        s = qc45()
        settled = dict(s.lines[-1])
        s.device("1f 03 03 01 02")   # slot 2, unconfigured on this unit
        self.assertEqual(s.lines[-1], settled)
        self.assertEqual(s.headset.mode, "ambient")
        self.assertIsNone(s.exit_code)

    def test_nothing_is_written_before_the_mode_is_known(self):
        s = Session()
        s.command("set anc")
        s.command("set ambient")
        self.assertEqual(s.sent, [])
        s.do_open()
        s.command("set anc")
        self.assertEqual(len(s.sent), 2)

    def test_the_line_has_no_strength_row(self):
        s = qc45()
        self.assertEqual(s.lines[-1]["available"], ["anc", "ambient"])
        self.assertNotIn("ancLevels", s.lines[-1])


class Battery(unittest.TestCase):
    def test_the_first_byte_is_the_level_in_both_shapes(self):
        self.assertEqual(bose.battery_level(harness.hexbytes("5a ff ff 00")), 90)
        self.assertEqual(bose.battery_level(harness.hexbytes("46")), 70)

    def test_a_level_over_100_is_dropped_not_clamped(self):
        self.assertIsNone(bose.battery_level(harness.hexbytes("65 ff ff 00")))
        self.assertIsNone(bose.battery_level(harness.hexbytes("65")))   # SYNTHETIC

    def test_an_empty_payload_is_no_level(self):
        self.assertIsNone(bose.battery_level(b""))

    def test_the_battery_waits_for_the_mode_and_invents_no_earpieces(self):
        s = Session()
        s.do_open()
        s.device(QC45_BATTERY)
        self.assertEqual(s.headset.battery, 90)
        self.assertEqual(s.lines, [])
        self.assertIsNone(s.exit_code)
        s.device(AWARE)
        self.assertEqual(s.lines[-1]["battery"], {"headset": 90, "charging": []})

    def test_a_battery_change_alone_moves_the_line(self):
        s = qc45()
        s.device("02 02 03 04 59 ff ff 00")   # SYNTHETIC 89%
        self.assertEqual(s.lines[-1]["battery"], {"headset": 89, "charging": []})
        self.assertEqual(s.lines[-1]["mode"], "ambient")

    def test_it_rides_on_the_noise_line_too(self):
        s = qc35()
        self.assertEqual(s.lines[-1]["battery"], {"headset": 70, "charging": []})
        s.device("02 02 03 01 45")            # SYNTHETIC 69%
        self.assertEqual(s.lines[-1]["battery"], {"headset": 69, "charging": []})
        self.assertEqual(s.lines[-1]["ancLevel"], "low")


class PathSwitch(unittest.TestCase):
    """Which question a headset is asked is the headset's to answer."""

    def test_the_qc45_question_goes_out_first(self):
        s = Session()
        s.do_open()
        self.assertEqual(s.sent, ["02 02 01 00", "1f 03 01 00"])
        self.assertIsInstance(s.headset.control, bose.AudioModes)

    def test_an_error_asks_the_other_question_once(self):
        s = Session()
        s.do_open()
        s.device(MODES_ERROR)
        self.assertIsInstance(s.headset.control, bose.NoiseCancelling)
        self.assertEqual(s.sent[-1], "01 06 01 00")
        s.device(MODES_ERROR)
        self.assertEqual(s.sent.count("01 06 01 00"), 1)

    def test_an_error_alone_is_not_a_mode(self):
        s = Session()
        s.do_open()
        s.device(MODES_ERROR)
        self.assertIsNone(s.headset.mode)
        self.assertEqual(s.lines, [])

    def test_a_headset_that_answers_the_audio_modes_is_never_asked(self):
        s = qc45()
        s.fire()
        s.command("set anc")
        s.advance(30)
        self.assertIsInstance(s.headset.control, bose.AudioModes)
        self.assertNotIn("01 06 01 00", s.sent)

    def test_an_error_after_the_audio_modes_answered_keeps_the_qc45_question(self):
        s = qc45()
        s.command("set anc")
        s.device(MODES_ERROR)                 # SYNTHETIC: the START refused
        s.fire()
        self.assertIsInstance(s.headset.control, bose.AudioModes)
        self.assertNotIn("01 06 01 00", s.sent)
        self.assertEqual(s.headset.mode, "ambient")

    def test_an_error_after_an_unnamed_slot_keeps_the_qc45_question(self):
        s = Session()
        s.do_open()
        s.device("1f 03 03 01 02")            # SYNTHETIC: a custom slot
        s.device(MODES_ERROR)                 # SYNTHETIC: an ERROR after it
        self.assertIsInstance(s.headset.control, bose.AudioModes)
        self.assertNotIn("01 06 01 00", s.sent)

    def test_a_noise_reply_before_the_switch_is_ignored(self):
        s = Session()
        s.do_open()
        s.device(NOISE_LOW)
        self.assertIsNone(s.headset.mode)
        self.assertEqual(s.lines, [])

    def test_an_audio_mode_reply_after_the_switch_is_ignored(self):
        s = qc35()
        settled = dict(s.lines[-1])
        s.device(AWARE)                       # SYNTHETIC: a late [31.3] STATUS
        self.assertEqual(s.lines[-1], settled)
        self.assertIsInstance(s.headset.control, bose.NoiseCancelling)

    def test_the_poll_follows_the_path(self):
        s = qc35()
        s.fire()
        self.assertEqual(s.sent[-1], "01 06 01 00")
        self.assertNotIn("1f 03 01 00", s.sent[2:])

    def test_the_error_and_the_answer_in_one_read_are_both_taken(self):
        s = Session()
        s.do_open()
        s.device(MODES_ERROR + " " + NOISE_HIGH)
        self.assertEqual(s.headset.mode, "anc")
        self.assertEqual(s.headset.control.strength, "high")

    def test_a_noise_reply_split_at_every_byte_reads_once(self):
        whole = harness.hexbytes(NOISE_HIGH)
        for cut in range(1, len(whole)):
            with self.subTest(cut=cut):
                s = Session()
                s.do_open()
                s.device(MODES_ERROR)
                s.receive(whole[:cut])
                s.receive(whole[cut:])
                self.assertEqual(s.headset.control.strength, "high")


class Mask(unittest.TestCase):
    """The second byte of a [1.6] STATUS, and what is built on it."""

    def test_the_observed_reply_names_off_and_both_strengths(self):
        self.assertEqual(bose.noise_reading(harness.hexbytes("01 0b")), (1, [0, 1, 3]))

    def test_a_reply_without_the_mask_falls_back_to_the_named_values(self):
        self.assertEqual(bose.noise_reading(b"\x01"), (1, None))
        s = qc35("01 06 03 01 01")            # SYNTHETIC: one byte
        self.assertEqual(s.lines[-1]["ancLevels"], ["low", "high"])
        self.assertEqual(s.lines[-1]["available"], ["off", "anc"])

    def test_an_empty_payload_is_no_value(self):
        self.assertEqual(bose.noise_reading(b""), (None, None))

    def test_a_mask_without_the_off_bit_does_not_offer_off(self):
        s = qc35("01 06 03 02 01 0a")         # SYNTHETIC: bits 1 and 3
        self.assertEqual(s.lines[-1]["available"], ["anc"])
        s.command("set off")
        self.assertNotIn("01 06 02 01 00", s.sent)

    def test_a_mask_with_one_strength_offers_one(self):
        s = qc35("01 06 03 02 01 03")         # SYNTHETIC: bits 0 and 1
        self.assertEqual(s.lines[-1]["ancLevels"], ["low"])
        self.assertEqual(s.lines[-1]["available"], ["off", "anc"])

    def test_an_unnamed_value_is_not_forced_into_a_name(self):
        s = qc35()
        settled = dict(s.lines[-1])
        s.device("01 06 03 02 02 0b")         # SYNTHETIC: the refused value 2
        self.assertEqual(s.lines[-1], settled)
        self.assertEqual((s.headset.mode, s.headset.control.strength), ("anc", "low"))
        self.assertIsNone(s.exit_code)

    def test_the_refusal_of_a_value_outside_the_mask_is_not_a_change(self):
        s = qc35(NOISE_HIGH)
        settled = dict(s.lines[-1])
        s.device(NOISE_REFUSED)
        self.assertEqual(s.lines[-1], settled)
        self.assertIsNone(s.exit_code)


class Strengths(unittest.TestCase):
    """The two strengths, named by the owner listening to them."""

    def test_the_names_come_from_the_hardware_not_the_published_tables(self):
        # 0x00 was the loudest and 0x03 the quietest, ranked unnamed, twice.
        # This keeps a well-meaning correction from putting High on the
        # weaker setting.
        self.assertEqual(bose.STRENGTH_NAMES, {0x01: "low", 0x03: "high"})
        self.assertEqual(qc35(NOISE_LOW).lines[-1]["ancLevel"], "low")
        self.assertEqual(qc35(NOISE_HIGH).lines[-1]["ancLevel"], "high")

    def test_off_is_a_mode_and_not_a_strength(self):
        s = qc35(NOISE_OFF)
        self.assertEqual(s.lines[-1]["mode"], "off")
        self.assertEqual(s.lines[-1]["ancLevels"], ["low", "high"])

    def test_the_strength_last_seen_is_kept_across_off(self):
        s = qc35(NOISE_HIGH, NOISE_OFF)
        self.assertEqual((s.lines[-1]["mode"], s.lines[-1]["ancLevel"]), ("off", "high"))

    def test_set_anc_asks_for_the_strength_last_seen(self):
        s = qc35(NOISE_HIGH, NOISE_OFF)
        s.command("set anc")
        self.assertEqual(s.sent[-1], "01 06 02 01 03")

    def test_set_anc_with_no_strength_yet_asks_for_the_strongest(self):
        s = qc35(NOISE_OFF)
        self.assertEqual(s.lines[-1]["ancLevel"], "high")
        self.assertIsNone(s.headset.control.strength)
        s.command("set anc")
        self.assertEqual(s.sent[-1], "01 06 02 01 03")

    def test_each_strength_is_asked_for_by_its_own_value(self):
        s = qc35()
        s.command("level high")
        self.assertEqual(s.sent[-1], "01 06 02 01 03")
        s.command("level low")
        self.assertEqual(s.sent[-1], "01 06 02 01 01")

    def test_nothing_this_headset_lacks_is_ever_sent(self):
        s = qc35()
        before = list(s.sent)
        for command in ("set ambient", "set talkthru", "level mid", "level adaptive",
                        "level 5", "voice on", "latency on"):
            s.command(command)
        self.assertEqual(s.sent, before)

    def test_nothing_is_sent_before_the_headset_has_answered(self):
        s = Session()
        s.do_open()
        s.device(MODES_ERROR)
        before = list(s.sent)
        s.command("set off")
        s.command("level high")
        self.assertEqual(s.sent, before)


class Clock(unittest.TestCase):
    """The poll, the readback and the deadline, on the fake clock."""

    def test_silence_parks_the_address_after_ten_seconds_of_polling(self):
        s = Session()
        s.do_open()
        s.advance(9.9)
        self.assertIsNone(s.exit_code)
        s.advance(0.1)
        self.assertEqual(s.exit_code, SILENT)
        self.assertGreater(s.headset.polls, 0)
        self.assertEqual(s.lines, [{"modes": False,
                                    "error": "the headset did not answer the listening-mode query"}])

    def test_a_refusal_then_silence_parks_the_address(self):
        s = Session()
        s.do_open()
        s.device(MODES_ERROR)
        s.advance(10)
        self.assertEqual(s.exit_code, SILENT)
        self.assertIsInstance(s.headset.control, bose.NoiseCancelling)
        self.assertIn("01 06 01 00", s.sent[3:])   # the polls asked [1.6]
        self.assertFalse(s.lines[-1]["modes"])

    def test_an_answer_in_time_holds_the_deadline_off(self):
        s = qc45()
        s.advance(60)
        self.assertIsNone(s.exit_code)

    def test_the_poll_asks_every_four_seconds_and_the_battery_every_fourth(self):
        s = qc45()
        s.advance(16)
        self.assertEqual(s.sent[2:], ["1f 03 01 00"] * 4 + ["02 02 01 00"])
        self.assertEqual(s.headset.polls, 4)

    def test_the_readback_comes_two_seconds_after_a_set_and_is_not_a_poll(self):
        s = qc45()
        s.command("set anc")
        s.advance(1.9)
        self.assertEqual(s.sent[-1], "1f 03 05 02 00 00")
        s.advance(0.2)
        self.assertEqual(s.sent[-1], "1f 03 01 00")
        self.assertEqual(s.headset.polls, 0)
        s.device(QUIET)
        self.assertEqual(s.headset.mode, "anc")

    def test_the_readback_asks_the_noise_question_on_that_path(self):
        s = qc35()
        s.command("level high")
        s.advance(2.1)
        self.assertEqual(s.sent[-2:], ["01 06 02 01 03", "01 06 01 00"])
        self.assertEqual(s.headset.polls, 0)
        self.assertNotIn("1f 03 01 00", s.sent[2:])
        s.device(NOISE_HIGH)
        self.assertEqual(s.headset.control.strength, "high")

    def test_a_second_set_moves_the_readback_rather_than_adding_one(self):
        s = qc45()
        s.command("set anc")
        s.advance(1.5)
        s.command("set ambient")
        s.advance(1.5)
        self.assertEqual(s.sent[-1], "1f 03 05 02 01 00")
        s.advance(0.5)
        self.assertEqual(s.sent[2:], ["1f 03 05 02 00 00", "1f 03 05 02 01 00", "1f 03 01 00"])

    def test_the_link_going_away_is_transient(self):
        for reason in ("Connection reset by peer", None):
            with self.subTest(reason=reason):
                s = qc35()
                s.headset.closed(MAIN, reason)
                self.assertEqual(s.exit_code, TRANSIENT)
                self.assertFalse(s.lines[-1]["modes"])
                self.assertIn("the Bose channel closed", s.lines[-1]["error"])

    def test_nothing_runs_after_the_run_has_ended(self):
        s = qc45()
        s.headset.stop()
        before = list(s.sent)
        s.advance(30)
        s.command("set anc")
        self.assertEqual(s.sent, before)


class Isolation(unittest.TestCase):
    def test_a_qc35_and_a_qc45_do_not_disturb_each_other(self):
        first = qc45()
        settled = dict(first.lines[-1])
        second = qc35()
        self.assertIsInstance(first.headset.control, bose.AudioModes)
        self.assertEqual((first.headset.mode, first.headset.battery), ("ambient", 90))
        self.assertEqual(first.lines[-1], settled)
        self.assertEqual((second.headset.mode, second.headset.battery), ("anc", 70))


class Arguments(unittest.TestCase):
    def test_the_address_is_checked_and_upper_cased(self):
        self.assertEqual(bose.Bose.parse([ADDRESS.lower()]), {"address": ADDRESS})

    def test_anything_else_is_setup(self):
        for args in ([], ["nope"], [ADDRESS, "extra"]):
            with self.subTest(args=args):
                with self.assertRaises(SetupError):
                    bose.Bose.parse(args)
        self.assertEqual(SETUP, 4)


if __name__ == "__main__":
    unittest.main()
