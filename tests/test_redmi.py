"""Redmi Buds 5 and Buds 3 Pro: the frozen sessions, the framing, the
handshake, and what the earbuds' own announcements get.

    python3 -m unittest tests.test_redmi

Frames are verbatim from docs/captures/redmi-buds-5.txt and
docs/captures/redmi-buds-3-pro.txt unless a test says it is synthetic.
"""
import unittest

from tests import harness
from brands import redmi
from core.headset import SILENT, TRANSIENT, SetupError

ADDRESS = "AA:BB:CC:00:00:01"
BUDS_3_PRO = "Redmi Buds 3 Pro"


class Session(harness.Session):
    """A Redmi session. "device" in a pin is bytes as hex, fed to the bridge
    as one read (it may hold several frames, or part of one). "sent" is every
    frame written, as hex. "open" is the channel coming up, which asks for the
    mode and the battery, or for a model whose row authenticates, sends our
    challenge. A pin's session may give the reported name ("name", the
    bridge's second argument) and fix the 16-byte challenge ("challenge",
    hex) to the one its capture sent."""

    def __init__(self, name=None, challenge=None):
        super().__init__()
        options = redmi.Redmi.parse([ADDRESS] + ([name] if name else []))
        if challenge:
            fixed = harness.hexbytes(challenge)
            options["new_challenge"] = lambda: fixed
        self.start(redmi.Redmi, **options)

    def device(self, spec):
        self.receive(harness.hexbytes(spec) if isinstance(spec, str) else spec)


harness.pin_tests(globals(), "redmi", Session)


def split(stream):
    buffer = bytearray(stream)
    return redmi.unframe(buffer), bytes(buffer)


# ---- the Buds 5's frames

ANSWER = harness.hexbytes(
    "fe dc ba 00 02 00 2c 00 01 05 01 10 38 10 38 05 03 27 17 50 6a 02 04 01 "
    "02 05 00 03 06 10 38 04 07 4b 50 64 02 08 02 02 09 00 02 0c 00 02 0d 02 "
    "02 0e 00 ef")
NOISE_AMBIENT = harness.hexbytes("fe dc ba 00 f3 00 07 00 01 04 00 0b 02 00 ef")
NOISE_ANC = harness.hexbytes("fe dc ba 00 f3 00 07 00 05 04 00 0b 01 00 ef")
NOISE_OFF = harness.hexbytes("fe dc ba 00 f3 00 07 00 07 04 00 0b 00 00 ef")
ANNOUNCED_ANC = harness.hexbytes("fe dc ba c7 f4 00 06 05 04 00 0b 01 00 ef")
SET_ACK = harness.hexbytes("fe dc ba 00 f2 00 02 00 04 ef")
REPORT_ANC = harness.hexbytes("fe dc ba c7 0e 00 04 04 02 04 01 ef")
BATTERY = {"left": 75, "right": 80, "case": 100, "charging": []}


def parsed(raw):
    return redmi.read_frame(raw)


class Frames(unittest.TestCase):
    def test_the_battery_ask_is_get_target_info_for_every_attribute(self):
        self.assertEqual(harness.hexstr(redmi.target_info_get(1)),
                         "fe dc ba c0 02 00 05 01 ff ff ff ff ef")

    def test_the_mode_ask_is_get_device_config_11(self):
        self.assertEqual(harness.hexstr(redmi.noise_get(1)), "fe dc ba c0 f3 00 03 01 00 0b ef")

    def test_the_sets_are_the_configs_the_app_wrote(self):
        # The capture's sets: transparency (sn 2), cancelling (4), off (6).
        self.assertEqual(harness.hexstr(redmi.noise_set(2, 2, 0)),
                         "fe dc ba c0 f2 00 06 02 04 00 0b 02 00 ef")
        self.assertEqual(harness.hexstr(redmi.noise_set(4, 1, 0)),
                         "fe dc ba c0 f2 00 06 04 04 00 0b 01 00 ef")
        self.assertEqual(harness.hexstr(redmi.noise_set(6, 0, 0)),
                         "fe dc ba c0 f2 00 06 06 04 00 0b 00 00 ef")

    def test_the_sequence_number_is_one_byte(self):
        self.assertEqual(redmi.target_info_get(0x101)[7], 0x01)

    def test_the_sn_wraps_in_a_session(self):
        s = Session()
        s.headset.sn = 0xFF
        s.do_open()
        self.assertEqual([f[7] for f in s.frames], [0x00, 0x01])


class Battery(unittest.TestCase):
    def test_left_right_case_from_the_captured_answer(self):
        self.assertEqual(redmi.target_battery(parsed(ANSWER)), BATTERY)

    def test_the_buds_5_status_report_carries_no_battery(self):
        self.assertIsNone(redmi.target_battery(parsed(REPORT_ANC)))
        self.assertIsNone(redmi.report_battery(parsed(REPORT_ANC)))

    # Synthetic below: the Buds 5 capture has no charging earbud and no empty one.
    def test_the_charging_bit_and_no_reading(self):
        self.assertEqual(redmi.battery_levels([0xCB, 0x50, 0xFF]),
                         {"left": 75, "right": 80, "charging": ["left"]})

    def test_no_part_reading_is_none(self):
        self.assertIsNone(redmi.battery_levels([0xFF, 0xFF, 0xFF]))

    def test_a_level_past_100_is_dropped(self):
        self.assertEqual(redmi.battery_levels([0x7F, 0x50, 0x64]),
                         {"right": 80, "case": 100, "charging": []})

    def test_an_error_status_carries_no_battery(self):
        failed = bytearray(ANSWER)
        failed[7] = 0x01
        self.assertIsNone(redmi.target_battery(parsed(bytes(failed))))

    def test_another_opcode_carries_no_battery(self):
        other = bytearray(ANSWER)
        other[4] = 0x05
        self.assertIsNone(redmi.target_battery(parsed(bytes(other))))

    def test_our_own_asks_echoed_back_are_not_answers(self):
        self.assertIsNone(redmi.target_battery(parsed(redmi.target_info_get(1))))
        self.assertIsNone(redmi.noise_control(parsed(redmi.noise_get(1))))


class NoiseControl(unittest.TestCase):
    def test_the_three_states(self):
        self.assertEqual(redmi.noise_control(parsed(NOISE_AMBIENT)), (2, 0))
        self.assertEqual(redmi.noise_control(parsed(NOISE_ANC)), (1, 0))
        self.assertEqual(redmi.noise_control(parsed(NOISE_OFF)), (0, 0))

    def test_the_announcement_after_a_set(self):
        self.assertEqual(redmi.noise_control(parsed(ANNOUNCED_ANC)), (1, 0))

    def test_the_set_ack_and_the_status_report_say_nothing(self):
        self.assertIsNone(redmi.noise_control(parsed(SET_ACK)))
        self.assertIsNone(redmi.noise_control(parsed(REPORT_ANC)))

    # Synthetic: no state but 0, 1, 2 was seen.
    def test_an_unknown_state_is_not_a_mode(self):
        odd = bytearray(NOISE_ANC)
        odd[12] = 0x09
        self.assertIsNone(redmi.noise_control(parsed(bytes(odd))))

    def test_another_config_is_not_the_noise_control(self):
        other = bytearray(NOISE_ANC)
        other[11] = 0x25
        self.assertIsNone(redmi.noise_control(parsed(bytes(other))))

    def test_a_config_too_short_for_state_and_level(self):
        short = harness.hexbytes("fe dc ba 00 f3 00 06 00 01 03 00 0b 02 ef")
        self.assertIsNone(redmi.noise_control(parsed(short)))


class Framing(unittest.TestCase):
    def test_a_frame_split_anywhere_reads_once_whole(self):
        for cut in range(1, len(ANSWER)):
            with self.subTest(cut=cut):
                buffer = bytearray(ANSWER[:cut])
                self.assertEqual(redmi.unframe(buffer), [])
                buffer += ANSWER[cut:]
                self.assertEqual(redmi.unframe(buffer), [ANSWER])
                self.assertEqual(buffer, bytearray())

    def test_three_frames_in_one_read_as_after_a_set(self):
        self.assertEqual(split(REPORT_ANC + ANNOUNCED_ANC + SET_ACK),
                         ([REPORT_ANC, ANNOUNCED_ANC, SET_ACK], b""))

    # Synthetic damage.
    def test_garbage_before_two_frames(self):
        self.assertEqual(split(b"\x00\x01" + ANSWER + ANSWER), ([ANSWER, ANSWER], b""))

    def test_an_absurd_length_does_not_stall(self):
        self.assertEqual(split(b"\xfe\xdc\xba\x00\x02\xff\xff" + ANSWER)[0], [ANSWER])

    def test_a_bad_tail_resyncs_on_the_next_frame(self):
        broken = bytearray(ANSWER)
        broken[-1] = 0x00
        self.assertEqual(split(bytes(broken) + ANSWER)[0], [ANSWER])

    def test_a_header_cut_in_two_is_kept(self):
        self.assertEqual(split(b"\x00\x00\xfe\xdc"), ([], b"\xfe\xdc"))
        self.assertEqual(split(b"\x00\xfe"), ([], b"\xfe"))
        self.assertEqual(split(b"\xfe\xdc\x00"), ([], b""))
        self.assertEqual(split(b"\xfe\xdc" + ANSWER[2:])[0], [ANSWER])

    def test_empty_and_header_only_input(self):
        self.assertEqual(split(b"")[0], [])
        self.assertEqual(split(b"\xfe\xdc\xba"), ([], b"\xfe\xdc\xba"))

    def test_a_command_with_no_body_has_no_sn(self):
        # Synthetic: a body-less command, which must not be answered with the tail as its sn.
        frame = parsed(harness.hexbytes("fe dc ba c7 f4 00 00 ef"))
        self.assertTrue(frame.is_command)
        self.assertIsNone(frame.sn)


def ready(name=None):
    """A Buds 5 session that has said its mode and its battery."""
    s = Session(name)
    s.do_open()
    s.device(NOISE_AMBIENT)
    s.device(ANSWER)
    s.host.writes.clear()
    return s


class Modes(unittest.TestCase):
    def test_open_asks_for_the_mode_then_the_battery(self):
        s = Session()
        s.do_open()
        self.assertEqual([f[4] for f in s.frames], [redmi.GET_CONFIG, redmi.TARGET_INFO])

    def test_a_set_sends_the_config_and_does_not_claim_the_mode(self):
        s = ready()
        s.command("set anc")
        self.assertEqual(len(s.frames), 1)
        self.assertEqual(s.frames[0][4], redmi.SET_CONFIG)
        self.assertEqual(s.frames[0][-5:-1], bytes([0x00, 0x0B, 1, 0]))
        self.assertEqual(s.lines[-1]["mode"], "ambient")

    def test_the_announcement_moves_the_mode(self):
        s = ready()
        s.device(ANNOUNCED_ANC)
        self.assertEqual(s.lines[-1]["mode"], "anc")

    def test_a_set_keeps_the_level_the_state_was_last_seen_at(self):
        # Synthetic: every level in the capture was 0.
        s = ready()
        deep = bytearray(ANNOUNCED_ANC)
        deep[12] = 2
        s.device(bytes(deep))
        s.device(NOISE_AMBIENT)
        s.host.writes.clear()
        s.command("set anc")
        self.assertEqual(s.frames[0][-5:-1], bytes([0x00, 0x0B, 1, 2]))

    def test_nothing_is_sent_before_the_earbuds_have_said_their_mode(self):
        s = Session()
        s.do_open()
        s.device(ANSWER)
        s.host.writes.clear()
        s.command("set anc")
        self.assertEqual(s.frames, [])

    def test_unknown_commands_send_nothing(self):
        s = ready()
        for line in ("set talkthru", "set", "level 3", "voice on", "set anc now", "SET ANC", ""):
            s.command(line)
        self.assertEqual(s.frames, [])

    def test_the_mode_is_read_back_800_ms_after_a_set(self):
        s = ready()
        s.command("set off")
        s.host.writes.clear()
        s.advance(0.7)
        self.assertEqual(s.frames, [])
        s.advance(0.1)
        self.assertEqual([f[4] for f in s.frames], [redmi.GET_CONFIG])

    def test_the_battery_goes_out_alone_when_no_mode_comes(self):
        s = Session()
        s.do_open()
        s.device(ANSWER)
        self.assertEqual(s.lines, [])
        s.advance(2.0)
        self.assertEqual(s.lines, [{"modes": True, "available": [], "battery": BATTERY}])
        s.device(NOISE_OFF)
        self.assertEqual(s.lines[-1], {"modes": True, "available": ["off", "anc", "ambient"],
                                       "mode": "off", "battery": BATTERY})

    def test_the_same_state_is_not_printed_twice(self):
        s = ready()
        before = len(s.lines)
        s.device(NOISE_AMBIENT)
        s.device(ANSWER)
        self.assertEqual(len(s.lines), before)

    def test_the_poll_asks_for_the_mode_and_the_battery_every_30_s(self):
        s = ready()
        s.advance(29.9)
        self.assertEqual(s.frames, [])
        s.advance(0.1)
        self.assertEqual([f[4] for f in s.frames], [redmi.GET_CONFIG, redmi.TARGET_INFO])
        s.advance(30.0)
        self.assertEqual(len(s.frames), 4)

    def test_replies_in_pieces_and_glued(self):
        s = Session()
        s.do_open()
        stream = ANSWER + NOISE_ANC
        for piece in (stream[:10], stream[10:30], stream[30:len(ANSWER) + 6], stream[len(ANSWER) + 6:]):
            s.device(piece)
        self.assertEqual(s.lines[-1]["mode"], "anc")
        self.assertEqual(s.lines[-1]["battery"]["left"], 75)

    def test_echoes_and_junk_print_nothing(self):
        s = Session()
        s.do_open()
        s.device(redmi.noise_get(1) + redmi.target_info_get(2))
        s.device(b"\x00\x01\x02")
        s.device(SET_ACK)
        self.assertEqual(s.lines, [])
        self.assertIsNone(s.exit_code)

    def test_nothing_is_written_once_stopped(self):
        s = ready()
        s.headset.stop()
        s.command("set anc")
        s.headset.ask()
        s.advance(60)
        self.assertEqual(s.frames, [])


class Silence(unittest.TestCase):
    def test_silence_is_exit_3(self):
        s = Session()
        s.do_open()
        s.advance(5.0)
        self.assertEqual(s.exit_code, SILENT)
        self.assertEqual(s.lines, [{"modes": False, "error": "the Redmi Buds did not answer"}])

    def test_a_mode_answer_alone_is_an_answer(self):
        s = Session()
        s.do_open()
        s.device(NOISE_AMBIENT)
        s.advance(5.0)
        self.assertIsNone(s.exit_code)

    def test_the_link_closing_is_exit_1(self):
        s = ready()
        s.headset.closed("main", "Connection reset by peer")
        self.assertEqual(s.exit_code, TRANSIENT)
        self.assertEqual(s.lines[-1], {"modes": False,
                                       "error": "the Redmi channel closed: Connection reset by peer"})


# ---- Redmi Buds 3 Pro: the same frames behind an authentication, from the
#      capture's third run unless marked.

OUR_CHALLENGE = harness.hexbytes("dd3b5c5dfbe669841c2e8184758e6600")
THEIR_ANSWER = harness.hexbytes(
    "fe dc ba 04 50 00 13 00 01 01 bd 04 53 94 ac 59 2f f4 4e be c2 18 fe 55 63 e9 ef")
CONFIRM_ACK = harness.hexbytes("fe dc ba 04 51 00 03 00 02 01 ef")
THEIR_CHALLENGE = harness.hexbytes(
    "fe dc ba c0 50 00 12 00 01 78 5a f7 fb b2 60 9c c7 74 c2 7d 69 d1 36 21 b9 ef")
OUR_ANSWER = harness.hexbytes(
    "fe dc ba 04 50 00 13 00 00 01 ff dc 81 66 35 b6 d1 a4 88 7a e1 42 f6 22 7c cc ef")
THEIR_CONFIRM = harness.hexbytes("fe dc ba c0 51 00 03 01 01 00 ef")
HANDSHAKE = THEIR_ANSWER + CONFIRM_ACK + THEIR_CHALLENGE + THEIR_CONFIRM
B3P_AMBIENT = harness.hexbytes("fe dc ba 00 f3 00 07 00 03 04 00 0b 02 00 ef")
B3P_ANSWER = harness.hexbytes(
    "fe dc ba 00 02 00 38 00 04 11 00 52 65 64 6d 69 20 42 75 64 73 20 33 20 50 72 6f "
    "05 01 11 11 11 11 02 02 5e 05 03 5a 4d ea 0b 02 04 01 02 05 00 03 06 11 11 02 08 "
    "01 04 07 5e 5e 2c 02 0d 02 ef")
CONFIG_12 = harness.hexbytes("fe dc ba c7 f4 00 05 02 03 00 0c 0c ef")
# The fourth run: status reports with the batteries, the left bud going in.
REPORT_BATTERY = harness.hexbytes("fe dc ba c7 0e 00 06 08 04 00 5c 5c 2c ef")
REPORT_NO_READING = harness.hexbytes("fe dc ba c7 0e 00 06 09 04 00 ff ff ff ef")
REPORT_CHARGING = harness.hexbytes("fe dc ba c7 0e 00 06 0a 04 00 dc 5c 2c ef")

AFTER_HANDSHAKE = [
    harness.hexstr(redmi.challenge_frame(1, OUR_CHALLENGE)),
    "fe dc ba c4 51 00 03 02 01 00 ef",
    harness.hexstr(OUR_ANSWER),
    "fe dc ba 04 51 00 03 00 01 01 ef",
    "fe dc ba c0 f3 00 03 03 00 0b ef",
    "fe dc ba c0 02 00 05 04 ff ff ff ff ef",
]


def buds_3_pro():
    s = Session(BUDS_3_PRO, harness.hexstr(OUR_CHALLENGE))
    s.do_open()
    return s


def authenticated():
    s = buds_3_pro()
    s.device(HANDSHAKE)
    return s


class Authentication(unittest.TestCase):
    def test_the_response_to_every_captured_challenge(self):
        # Ours, answered by the earbuds; theirs, whose answer they accepted.
        self.assertEqual(b"\x01" + redmi.auth_response(OUR_CHALLENGE), parsed(THEIR_ANSWER).payload)
        self.assertEqual(redmi.auth_response(THEIR_CHALLENGE[9:25]), OUR_ANSWER[10:26])
        # The capture's second run, both ways.
        self.assertEqual(redmi.auth_response(harness.hexbytes("f72c67873c8a3a67a5ffff5c018f4b0a")),
                         harness.hexbytes("1bf76a9f5b2bce90ba6dc4e311022f16"))
        self.assertEqual(redmi.auth_response(harness.hexbytes("cf8f239c78fb41ea13a8b8932a6e9441")),
                         harness.hexbytes("5497ce0afc1bb3845683eb21a98b99c5"))

    def test_the_challenge_bytes_are_not_changed(self):
        challenge = bytes(OUR_CHALLENGE)
        redmi.auth_response(challenge)
        self.assertEqual(challenge, OUR_CHALLENGE)

    def test_the_challenge_comes_from_the_injected_source(self):
        s = buds_3_pro()
        self.assertEqual(s.headset.challenge, OUR_CHALLENGE)
        self.assertEqual(len(redmi.random_challenge()), 16)

    def test_only_the_challenge_goes_out_until_both_sides_confirmed(self):
        s = buds_3_pro()
        self.assertEqual(s.sent, [harness.hexstr(redmi.challenge_frame(1, OUR_CHALLENGE))])
        s.headset.ask()
        s.headset.read_back()
        s.command("set anc")
        s.advance(4.9)
        self.assertEqual(len(s.frames), 1)

    def test_after_the_handshake_the_reads_are_the_buds_5_s(self):
        s = authenticated()
        self.assertEqual(s.sent, AFTER_HANDSHAKE)
        self.assertTrue(s.headset.authenticated)

    def test_a_wrong_answer_to_our_challenge_is_exit_3_and_nothing_more(self):
        # Synthetic damage: one byte of the captured answer changed.
        s = buds_3_pro()
        wrong = bytearray(THEIR_ANSWER)
        wrong[12] ^= 0x01
        s.device(bytes(wrong) + THEIR_CHALLENGE)
        self.assertEqual(s.exit_code, SILENT)
        self.assertEqual(len(s.frames), 1)
        self.assertEqual(s.lines, [{"modes": False,
                                    "error": "the Redmi Buds did not answer the authentication"}])

    def test_an_error_status_to_our_challenge_is_exit_3(self):
        # Synthetic: status 1 on the captured answer.
        s = buds_3_pro()
        failed = bytearray(THEIR_ANSWER)
        failed[7] = 0x01
        s.device(bytes(failed))
        self.assertEqual(s.exit_code, SILENT)

    def test_their_confirmation_before_ours_was_checked_is_not_taken(self):
        s = buds_3_pro()
        s.device(THEIR_CONFIRM)
        self.assertEqual(len(s.frames), 1)
        self.assertFalse(s.headset.authenticated)

    def test_our_answer_checked_twice_is_confirmed_once(self):
        # Synthetic: the earbuds' answer arriving again.
        s = buds_3_pro()
        s.device(THEIR_ANSWER + THEIR_ANSWER)
        self.assertEqual(s.sent, AFTER_HANDSHAKE[:2])

    def test_a_silent_handshake_is_exit_3(self):
        s = buds_3_pro()
        s.advance(5.0)
        self.assertEqual(s.exit_code, SILENT)
        self.assertEqual(len(s.frames), 1)

    def test_a_handshake_that_stalls_halfway_is_exit_3_too(self):
        s = buds_3_pro()
        s.device(THEIR_ANSWER)
        s.advance(5.0)
        self.assertEqual(s.exit_code, SILENT)

    def test_the_handshake_split_at_every_byte(self):
        stream = HANDSHAKE + B3P_AMBIENT + B3P_ANSWER
        for cut in range(1, len(stream)):
            with self.subTest(cut=cut):
                s = buds_3_pro()
                s.device(stream[:cut])
                s.device(stream[cut:])
                self.assertEqual(s.lines[-1]["mode"], "ambient")
                self.assertEqual(s.lines[-1]["battery"]["left"], 94)
                self.assertEqual(s.sent, AFTER_HANDSHAKE)

    def test_the_handshake_and_the_answers_in_one_read(self):
        s = buds_3_pro()
        s.device(HANDSHAKE + CONFIG_12 + B3P_AMBIENT + B3P_ANSWER)
        self.assertEqual(s.lines[-1]["battery"], {"left": 94, "right": 94, "case": 44,
                                                  "charging": []})
        self.assertEqual(s.sent[-1], "fe dc ba 04 f4 00 02 00 02 ef")

    def test_a_stop_during_the_handshake_writes_nothing_more(self):
        s = buds_3_pro()
        s.headset.stop()
        s.device(HANDSHAKE)
        s.advance(60)
        self.assertEqual(len(s.frames), 1)

    def test_the_buds_5_are_not_asked_to_authenticate(self):
        s = Session()
        s.do_open()
        self.assertNotIn(redmi.AUTH_CHALLENGE, [f[4] for f in s.frames])


class Acknowledgement(unittest.TestCase):
    def test_each_announcement_is_answered_with_its_sn(self):
        s = authenticated()
        s.host.writes.clear()
        s.device(CONFIG_12)
        s.device(REPORT_ANC)
        self.assertEqual(s.sent, ["fe dc ba 04 f4 00 02 00 02 ef", "fe dc ba 04 0e 00 02 00 04 ef"])

    def test_answers_and_set_acks_are_not_answered(self):
        s = authenticated()
        s.host.writes.clear()
        s.device(B3P_AMBIENT + B3P_ANSWER + SET_ACK)
        self.assertEqual(s.frames, [])

    def test_the_buds_5_are_sent_no_acknowledgement(self):
        s = ready()
        s.device(ANNOUNCED_ANC + REPORT_ANC)
        self.assertEqual(s.frames, [])

    def test_announcements_before_the_handshake_are_not_answered(self):
        s = buds_3_pro()
        s.device(CONFIG_12)
        self.assertEqual(len(s.frames), 1)


class RepeatingEarbuds:
    """Buds 3 Pro that repeat every unanswered announcement two seconds on,
    as they did in the capture's second run. Fed on the session's clock."""

    REPEAT_S = 2.0

    def __init__(self, session):
        self.s = session
        self.unanswered = {}      # (opcode, sn) -> frame
        self.seen = 0

    def announce(self, raw):
        frame = parsed(raw)
        self.unanswered[(frame.opcode, frame.sn)] = raw
        self.s.device(raw)
        self.settle()
        if (frame.opcode, frame.sn) in self.unanswered:
            self.s.host.later(self.REPEAT_S * 1000, self.repeat, ((frame.opcode, frame.sn),), False)

    def settle(self):
        for raw in self.s.frames[self.seen:]:
            frame = parsed(raw)
            if not frame.is_command:
                self.unanswered.pop((frame.opcode, frame.sn), None)
        self.seen = len(self.s.frames)

    def repeat(self, key):
        raw = self.unanswered.get(key)
        if raw is not None:
            self.s.device(raw)
            self.settle()
            if key in self.unanswered:
                self.s.host.later(self.REPEAT_S * 1000, self.repeat, (key,), False)


class StaleAnnouncements(unittest.TestCase):
    """Why the row answers announcements: the capture's second run."""

    RUN_2 = ["fe dc ba c7 0e 00 04 03 02 04 00 ef", "fe dc ba c7 f4 00 06 04 04 00 0b 00 00 ef",
             "fe dc ba c7 0e 00 04 05 02 04 01 ef", "fe dc ba c7 f4 00 06 06 04 00 0b 01 00 ef",
             "fe dc ba c7 0e 00 04 07 02 04 02 ef", "fe dc ba c7 f4 00 06 08 04 00 0b 02 00 ef"]

    def run_with(self, model):
        s = Session()
        s.start(redmi.Redmi, address=ADDRESS, model=model, new_challenge=lambda: OUR_CHALLENGE)
        s.do_open()
        s.device(HANDSHAKE + B3P_AMBIENT + B3P_ANSWER)
        earbuds = RepeatingEarbuds(s)
        for spec in self.RUN_2:          # off, cancelling, transparency, 0.3 s apart
            earbuds.announce(harness.hexbytes(spec))
            s.advance(0.15)
        settled = len(s.lines)
        self.assertEqual(s.lines[-1]["mode"], "ambient")
        s.advance(10.0)
        return [line["mode"] for line in s.lines[settled:]]

    def test_answered_the_last_mode_announced_stands(self):
        self.assertEqual(self.run_with(redmi.MODELS[BUDS_3_PRO]), [])

    def test_unanswered_an_earlier_mode_comes_back(self):
        # Synthetic row: the Buds 3 Pro's without the answers.
        later = self.run_with(redmi.Model(authenticate=True, report_battery=True))
        self.assertIn("off", later)


class ReportBattery(unittest.TestCase):
    def ready(self):
        s = authenticated()
        s.device(B3P_AMBIENT + B3P_ANSWER)
        return s

    def test_the_status_report_s_battery(self):
        self.assertEqual(redmi.report_battery(parsed(REPORT_BATTERY)),
                         {"left": 92, "right": 92, "case": 44, "charging": []})
        self.assertEqual(redmi.report_battery(parsed(REPORT_CHARGING)),
                         {"left": 92, "right": 92, "case": 44, "charging": ["left"]})
        self.assertIsNone(redmi.report_battery(parsed(REPORT_NO_READING)))
        self.assertIsNone(redmi.report_battery(parsed(REPORT_ANC)))

    def test_a_charging_bud_is_printed_and_no_reading_keeps_the_last(self):
        s = self.ready()
        s.device(REPORT_BATTERY)
        count = len(s.lines)
        s.device(REPORT_NO_READING)
        self.assertEqual(len(s.lines), count)
        s.device(REPORT_CHARGING)
        self.assertEqual(s.lines[-1]["battery"]["charging"], ["left"])
        self.assertEqual(s.lines[-1]["mode"], "ambient")

    def test_our_own_answer_echoed_back_is_not_a_report(self):
        echo = redmi.response(redmi.STATUS_REPORT, 8, b"\x04\x00\x5c\x5c\x2c")
        self.assertIsNone(redmi.report_battery(parsed(echo)))

    def test_the_buds_5_do_not_read_a_battery_from_a_status_report(self):
        # Synthetic for them: their reports carried none.
        s = ready()
        before = list(s.lines)
        s.device(REPORT_CHARGING)
        self.assertEqual(s.lines, before)


class Arguments(unittest.TestCase):
    def test_no_name_is_the_buds_5_and_the_name_picks_the_row(self):
        self.assertIs(redmi.Redmi.parse([ADDRESS])["model"], redmi.BUDS_5)
        self.assertIs(redmi.Redmi.parse([ADDRESS, BUDS_3_PRO])["model"], redmi.MODELS[BUDS_3_PRO])
        self.assertEqual(redmi.BUDS_5, redmi.Model(authenticate=False, acknowledge=False,
                                                   report_battery=False))
        self.assertEqual(redmi.Redmi.parse([ADDRESS.lower()])["address"], ADDRESS)

    def test_an_unknown_name_is_setup(self):
        with self.assertRaises(SetupError) as raised:
            redmi.Redmi.parse([ADDRESS, "Redmi Buds 5"])
        self.assertIn("no Redmi model row", str(raised.exception))

    def test_bad_arguments_are_setup(self):
        for args in ([], [ADDRESS, BUDS_3_PRO, "extra"], ["nope"], ["AA:BB:CC:00:00"],
                     ["AA:BB:CC:00:00:0G"], ["; rm -rf /"], ["AA:BB:CC:00:00:01 --evil"]):
            with self.subTest(args=args):
                with self.assertRaises(SetupError):
                    redmi.Redmi.parse(args)

    def test_the_link_is_the_fd2d_profile(self):
        s = Session()
        (spec,) = s.headset.links()
        self.assertEqual((spec.address, spec.uuid), (ADDRESS, redmi.UUID))


if __name__ == "__main__":
    unittest.main()
