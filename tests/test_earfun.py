"""EarFun Air Pro 4: the frozen session, the framing, and the readback.

    python3 -m unittest tests.test_earfun

The frames are verbatim from docs/captures/earfun-air-pro-4.txt, and the
first test checks each of them is there. What a test makes up is marked
synthetic.
"""
import os
import unittest

from tests import harness
from brands import earfun
from core.headset import SILENT, TRANSIENT, SetupError

ADDRESS = "AA:BB:CC:00:00:02"
NAME = "EarFun Air Pro 4"
CAPTURE = os.path.join(harness.ROOT, "docs", "captures", "earfun-air-pro-4.txt")


class Session(harness.Session):
    """An EarFun session. "device" in a pin is bytes as hex, fed as one read
    (it may hold several frames, or part of one). "sent" is every frame
    written, as hex. "open" is the channel coming up, which asks for the mode.
    "recheck" is one tick of the readback after a set, and the "rechecks"
    state is how many readbacks are left."""

    def __init__(self, name=NAME):
        super().__init__()
        self.start(earfun.EarFun, **earfun.EarFun.parse([ADDRESS, name]))

    def device(self, spec):
        self.receive(harness.hexbytes(spec) if isinstance(spec, str) else spec)

    def do_recheck(self):
        self.headset.read_back()

    def state(self, name):
        if name == "rechecks":
            return self.headset.readbacks_left
        return super().state(name)


harness.pin_tests(globals(), "earfun", Session)


# Answers to the mode get (0315): off, noise cancelling, transparency.
OFF = harness.hexbytes("ff 03 00 02 00 0a 83 15 00 00")
ANC = harness.hexbytes("ff 03 00 02 00 0a 83 15 00 01")
AMBIENT = harness.hexbytes("ff 03 00 02 00 0a 83 15 00 02")
ANSWERS = {"off": OFF, "anc": ANC, "ambient": AMBIENT}
# The answer to the ANC-style get (033B): the same shape, another command.
ANC_STYLE = harness.hexbytes("ff 03 00 02 00 0a 83 3b 00 02")
FIRMWARE = harness.hexbytes(
    "ff 03 00 1b 00 0a 83 09 00 54 57 36 30 30 2d 33 30 39 31 5f 32 30 32 35 30 39 31 39"
    " 5f 76 30 2e 34 2e 31")
# What the earbuds send unasked, glued in one read as they came.
OWN_0358_A = harness.hexbytes("ff 03 00 05 00 0a 03 58 00 5c 3b 4d 01")
OWN_0358_B = harness.hexbytes("ff 03 00 05 00 0a 03 58 00 33 4e e5 00")
OWN_034D = harness.hexbytes("ff 03 00 02 00 0a 03 4d 00 03")
OWN_0350 = harness.hexbytes("ff 03 00 02 00 0a 03 50 00 01")
OWN_BURST = OWN_0358_A + OWN_0358_B + OWN_034D
# What the bridge sends.
GET = harness.hexbytes("ff 04 00 00 00 0a 03 15")
SET = {"off": harness.hexbytes("ff 04 00 01 00 0a 03 14 00"),
       "anc": harness.hexbytes("ff 04 00 01 00 0a 03 14 01"),
       "ambient": harness.hexbytes("ff 04 00 01 00 0a 03 14 02")}
AVAILABLE = ["off", "anc", "ambient"]


def flat_capture():
    with open(CAPTURE) as handle:
        return " ".join(handle.read().split())


class Evidence(unittest.TestCase):
    def test_every_frame_here_is_in_the_capture(self):
        flat = flat_capture()
        for frame in (OFF, ANC, AMBIENT, ANC_STYLE, FIRMWARE, OWN_0358_A, OWN_0358_B,
                      OWN_034D, OWN_0350, OWN_BURST, GET, *SET.values()):
            with self.subTest(frame=harness.hexstr(frame)):
                self.assertIn(harness.hexstr(frame), flat)

    def test_the_pin_feeds_only_captured_frames(self):
        flat = flat_capture()
        for _path, pin in harness.pins_for("earfun"):
            self.assertEqual(pin["capture"], "docs/captures/earfun-air-pro-4.txt")
            for step in pin["steps"]:
                for key in ("device", "sent_last"):
                    if key in step:
                        self.assertIn(step[key], flat)
                for frame in step.get("sent", []):
                    self.assertIn(frame, flat)


class Frames(unittest.TestCase):
    def test_the_get_is_the_one_the_earbuds_answered(self):
        self.assertEqual(earfun.mode_get(), GET)

    def test_the_three_sets(self):
        for mode, frame in SET.items():
            self.assertEqual(earfun.mode_set(mode), frame, mode)


class ModeOf(unittest.TestCase):
    def test_the_three_answers(self):
        for mode, frame in ANSWERS.items():
            self.assertEqual(earfun.mode_of(frame), mode)

    def test_another_answer_of_the_same_shape_is_not_the_mode(self):
        self.assertIsNone(earfun.mode_of(ANC_STYLE))
        self.assertIsNone(earfun.mode_of(FIRMWARE))

    def test_the_earbuds_own_frames_are_not_the_mode(self):
        for frame in (OWN_0358_A, OWN_0358_B, OWN_034D, OWN_0350):
            self.assertIsNone(earfun.mode_of(frame))

    def test_our_own_frames_echoed_back_are_not_answers(self):
        self.assertIsNone(earfun.mode_of(GET))
        for frame in SET.values():
            self.assertIsNone(earfun.mode_of(frame))

    # Synthetic below: nothing but status 00 and values 00-02 was seen.
    def test_an_error_status_is_not_a_mode(self):
        failed = bytearray(ANC)
        failed[8] = 0x01
        self.assertIsNone(earfun.mode_of(bytes(failed)))

    def test_an_unknown_value_is_not_a_mode(self):
        odd = bytearray(ANC)
        odd[9] = 0x03
        self.assertIsNone(earfun.mode_of(bytes(odd)))

    def test_another_vendor_is_not_a_mode(self):
        other = bytearray(ANC)
        other[5] = 0x1D
        self.assertIsNone(earfun.mode_of(bytes(other)))

    def test_a_payload_of_another_length_is_not_a_mode(self):
        self.assertIsNone(earfun.mode_of(harness.hexbytes("ff 03 00 01 00 0a 83 15 01")))
        self.assertIsNone(earfun.mode_of(harness.hexbytes("ff 03 00 03 00 0a 83 15 00 01 00")))


def split(data):
    buffer = bytearray(data)
    return earfun.unframe(buffer), bytes(buffer)


class Framing(unittest.TestCase):
    def test_every_split_point_of_a_recorded_answer(self):
        for frame in (ANC, FIRMWARE):
            for cut in range(1, len(frame)):
                with self.subTest(frame=frame[:8].hex(), cut=cut):
                    buffer = bytearray(frame[:cut])
                    self.assertEqual(earfun.unframe(buffer), [])
                    buffer += frame[cut:]
                    self.assertEqual(earfun.unframe(buffer), [frame])
                    self.assertEqual(buffer, bytearray())

    def test_the_glued_read_the_earbuds_sent(self):
        self.assertEqual(split(OWN_BURST), ([OWN_0358_A, OWN_0358_B, OWN_034D], b""))

    def test_an_answer_glued_after_the_earbuds_own_frames(self):
        self.assertEqual(split(OWN_034D + AMBIENT)[0], [OWN_034D, AMBIENT])

    # Synthetic damage.
    def test_garbage_before_a_frame(self):
        self.assertEqual(split(b"\x00\x13\x37" + ANC), ([ANC], b""))

    def test_a_start_byte_without_the_earbuds_version_and_flags_resyncs(self):
        self.assertEqual(split(b"\xff\x04\x00" + ANC)[0], [ANC])
        self.assertEqual(split(b"\xff\x03\x01" + ANC)[0], [ANC])
        self.assertEqual(split(b"\xff\xff" + ANC)[0], [ANC])

    def test_a_truncated_frame_waits_and_nothing_is_read_from_it(self):
        s = Session()
        s.do_open()
        s.device(ANC[:-1])
        self.assertEqual(s.lines, [])
        self.assertEqual(bytes(s.headset.buffer), ANC[:-1])

    def test_a_lone_start_byte_is_kept(self):
        self.assertEqual(split(b"\x00\xff"), ([], b"\xff"))

    def test_nothing_and_no_start_byte(self):
        self.assertEqual(split(b""), ([], b""))
        self.assertEqual(split(b"\x01\x02\x03"), ([], b""))


def ready(answer=OFF):
    s = Session()
    s.do_open()
    s.device(answer)
    s.host.writes.clear()
    return s


class Modes(unittest.TestCase):
    def test_open_asks_once_and_the_first_answer_is_printed(self):
        s = Session()
        s.do_open()
        self.assertEqual(s.frames, [GET])
        s.device(OFF)
        self.assertEqual(s.lines, [{"modes": True, "mode": "off", "available": AVAILABLE}])

    def test_a_set_sends_one_frame_and_does_not_claim_the_mode(self):
        s = ready()
        s.command("set anc")
        self.assertEqual(s.frames, [SET["anc"]])
        self.assertEqual(len(s.lines), 1)

    def test_the_readback_asks_every_second_until_the_earbuds_agree(self):
        s = ready()
        s.command("set anc")
        s.host.writes.clear()
        s.advance(1.0)
        self.assertEqual(s.frames, [GET])
        s.device(OFF)                   # the change is not done yet
        self.assertEqual(len(s.lines), 1)
        s.advance(1.0)
        self.assertEqual(s.frames, [GET, GET])
        s.device(ANC)
        self.assertEqual(s.lines[-1]["mode"], "anc")
        self.assertEqual(s.headset.readbacks_left, 0)
        s.advance(5.0)
        self.assertEqual(s.frames, [GET, GET])
        self.assertNotIn(earfun.READBACK_MS, s.timers)

    def test_the_readback_gives_up_after_its_count(self):
        s = ready()
        s.command("set ambient")
        s.host.writes.clear()
        s.advance(9.0)
        self.assertEqual(s.frames, [GET] * earfun.READBACKS)
        self.assertEqual(s.lines[-1]["mode"], "off")
        self.assertNotIn(earfun.READBACK_MS, s.timers)

    def test_a_second_set_during_the_readback_refills_it_on_the_same_timer(self):
        s = ready()
        s.command("set anc")
        s.advance(1.0)
        s.command("set ambient")
        self.assertEqual(s.timers.count(earfun.READBACK_MS), 1)
        self.assertEqual(s.headset.readbacks_left, earfun.READBACKS)
        self.assertEqual(s.headset.wanted, "ambient")

    def test_nothing_is_sent_before_the_earbuds_have_said_their_mode(self):
        s = Session()
        s.do_open()
        s.host.writes.clear()
        s.command("set anc")
        self.assertEqual(s.frames, [])

    def test_unknown_commands_send_nothing(self):
        s = ready()
        for line in ("set talkthru", "set wind", "set", "level 3", "voice on",
                     "set anc now", "SET ANC", ""):
            s.command(line)
        self.assertEqual(s.frames, [])

    def test_two_commands_go_out_in_order(self):
        s = ready()
        s.command("set anc")
        s.command("set off")
        self.assertEqual(s.frames, [SET["anc"], SET["off"]])

    def test_the_same_state_is_not_printed_twice(self):
        s = ready()
        s.device(OFF)
        s.device(OWN_BURST + OFF)
        self.assertEqual(len(s.lines), 1)

    def test_the_poll_asks_every_10_s(self):
        s = ready()
        s.advance(9.9)
        self.assertEqual(s.frames, [])
        s.advance(0.1)
        self.assertEqual(s.frames, [GET])
        s.advance(10.0)
        self.assertEqual(s.frames, [GET, GET])

    def test_a_change_made_elsewhere_is_printed_when_the_poll_reads_it(self):
        # The frames are recorded; that the change came from elsewhere is the
        # synthetic part: the earbuds were never seen to change on their own.
        s = ready(AMBIENT)
        s.advance(10.0)
        s.device(ANC)
        self.assertEqual([line["mode"] for line in s.lines], ["ambient", "anc"])

    def test_replies_in_pieces(self):
        s = Session()
        s.do_open()
        for piece in (OWN_BURST[:7], OWN_BURST[7:20], OWN_BURST[20:] + AMBIENT[:4], AMBIENT[4:]):
            s.device(piece)
        self.assertEqual(s.lines, [{"modes": True, "mode": "ambient", "available": AVAILABLE}])

    def test_echoes_and_junk_print_nothing(self):
        s = Session()
        s.do_open()
        for chunk in (GET + SET["anc"], b"\x00\x01\x02", ANC_STYLE, FIRMWARE):
            s.device(chunk)
        self.assertEqual(s.lines, [])
        self.assertIsNone(s.exit_code)

    def test_nothing_is_written_once_stopped(self):
        s = ready()
        s.command("set anc")
        s.headset.stop()
        s.host.writes.clear()
        s.command("set ambient")
        s.headset.ask()
        s.headset.read_back()
        s.advance(30)
        self.assertEqual(s.frames, [])


class Silence(unittest.TestCase):
    def test_silence_asks_once_more_then_is_exit_3(self):
        s = Session()
        s.do_open()
        s.advance(2.0)
        self.assertEqual(s.frames, [GET, GET])
        self.assertIsNone(s.exit_code)
        s.advance(3.0)
        self.assertEqual(s.exit_code, SILENT)
        self.assertEqual(s.lines, [{"modes": False, "error": "the EarFun earbuds did not answer"}])

    def test_the_earbuds_own_frames_alone_are_not_an_answer(self):
        s = Session()
        s.do_open()
        s.device(OWN_BURST + OWN_0350)
        s.advance(5.0)
        self.assertEqual(s.exit_code, SILENT)

    def test_an_answer_means_the_timers_find_nothing_to_do(self):
        s = ready()
        s.advance(5.0)
        self.assertIsNone(s.exit_code)
        self.assertEqual(s.frames, [])

    def test_the_link_closing_is_exit_1(self):
        s = ready()
        s.headset.closed("main", None)
        self.assertEqual(s.exit_code, TRANSIENT)
        self.assertEqual(s.lines[-1], {"modes": False, "error": "the EarFun channel closed"})


class SlowEarbuds:
    """Earbuds that answer the get 30 ms later and take `delay` seconds to
    make a set, answering the old mode until then, as the captured ones did
    (0.7 to 2.1 s). Synthetic where told to ignore a set."""

    def __init__(self, session, mode="off", delay=1.5, ignore_sets=False):
        self.s = session
        self.mode = mode
        self.delay = delay
        self.ignore_sets = ignore_sets
        self.handled = 0
        self.gets = 0
        session.host.send = self.write

    def write(self, link, data):
        self.s.host.writes.append((link, data))
        command = int.from_bytes(data[6:8], "big")
        if command == earfun.NOISE_GET:
            self.gets += 1
            self.s.host.later(30, self.answer, (), False)
        elif command == earfun.NOISE_SET and not self.ignore_sets:
            self.s.host.later(self.delay * 1000, self.change, (earfun.MODE_NAMES[data[8]],), False)

    def answer(self):
        self.s.device(ANSWERS[self.mode])

    def change(self, mode):
        self.mode = mode


class AgainstTheEarbuds(unittest.TestCase):
    def test_the_slowest_change_seen_is_waited_for(self):
        s = Session()
        buds = SlowEarbuds(s, "off", delay=2.1)
        s.do_open()
        s.advance(0.1)
        s.command("set anc")
        s.advance(4.0)
        self.assertEqual([line["mode"] for line in s.lines], ["off", "anc"])
        self.assertEqual(buds.gets, 1 + 3)        # the open, then three readbacks

    def test_a_cycle_through_every_mode_is_reported_mode_by_mode(self):
        s = Session()
        SlowEarbuds(s, "off", delay=0.7)
        s.do_open()
        s.advance(0.1)
        for mode in ("anc", "ambient", "off"):
            s.command("set " + mode)
            s.advance(3.0)
        self.assertEqual([line["mode"] for line in s.lines], ["off", "anc", "ambient", "off"])

    def test_earbuds_that_ignore_a_set_are_asked_the_count_and_not_believed(self):
        s = Session()
        buds = SlowEarbuds(s, "ambient", ignore_sets=True)
        s.do_open()
        s.advance(0.1)
        s.command("set anc")
        s.advance(9.0)
        self.assertEqual(buds.gets, 1 + earfun.READBACKS)
        self.assertEqual([line["mode"] for line in s.lines], ["ambient"])

    def test_an_answer_glued_after_the_earbuds_own_frames_is_read(self):
        s = Session()
        s.do_open()
        s.device(OWN_BURST + ANC)
        self.assertEqual(s.lines[-1]["mode"], "anc")


class Arguments(unittest.TestCase):
    def test_the_name_picks_the_row(self):
        options = earfun.EarFun.parse([ADDRESS.lower(), NAME])
        self.assertEqual(options["address"], ADDRESS)
        self.assertIs(options["model"], earfun.MODELS[NAME])

    def test_a_model_without_a_row_is_setup(self):
        for name in ("", "EarFun Air Pro 3", "EarFun Air Pro 4 ", "earfun air pro 4"):
            with self.subTest(name=name):
                with self.assertRaises(SetupError) as raised:
                    earfun.EarFun.parse([ADDRESS, name])
                self.assertIn("no EarFun model row", str(raised.exception))

    def test_bad_arguments_are_setup(self):
        for args in ([], [ADDRESS], [ADDRESS, NAME, "extra"], ["nope", NAME],
                     ["AA:BB:CC:00:00", NAME], ["AA:BB:CC:00:00:0G", NAME],
                     ["; rm -rf /", NAME], ["AA:BB:CC:00:00:02 --evil", NAME]):
            with self.subTest(args=args):
                with self.assertRaises(SetupError):
                    earfun.EarFun.parse(args)

    def test_the_link_is_spp_through_a_profile(self):
        s = Session()
        (spec,) = s.headset.links()
        self.assertEqual((spec.address, spec.uuid), (ADDRESS, earfun.UUID))


if __name__ == "__main__":
    unittest.main()
