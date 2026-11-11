"""Sony MDR v1 and v2: the frozen sessions, the framing, and the rules that
hold across models (the question order, who is asked about wearing, what a
silent or a NO_MODES headset does).

    python3 -m unittest tests.test_sony
"""
import unittest

from tests import harness
from brands import sony
from core.headset import SETUP, SILENT, TRANSIENT, SetupError

ADDRESS = "94:DB:56:D0:F0:F0"
UUIDS = {"v2": sony.UUID_V2, "v1": sony.UUID_V1}


class Session(harness.Session):
    """A Sony session. "device" in a pin is an MDR payload as hex, wrapped
    the way the headset wraps one (kind 0x0C, seq 0, length, sum, stuffing),
    or {"wire": hex} for an exact captured frame. "ack" is the headset's ACK
    with seq 1, which frees the queue. "sent" is the payload of every 0x0C
    frame the bridge wrote, as hex; the ACKs it wrote are left out of "sent"
    but are in `frames`, which is every frame whole. "open" is the channel
    coming up, which sends the handshake. Pins name the UUID "v1" or "v2"."""

    def __init__(self, uuid="v2", name=""):
        super().__init__()
        self.start(sony.Sony, address=ADDRESS, uuid=UUIDS.get(uuid, uuid), name=name)

    def device(self, spec):
        if isinstance(spec, dict):
            self.receive(harness.hexbytes(spec["wire"]))
        else:
            self.receive(sony.frame(sony.COMMAND, 0, harness.hexbytes(spec)))

    def ack(self):
        self.receive(sony.frame(sony.ACK, 1))

    @property
    def sent(self):
        out = []
        for data in self.frames:
            decoded = sony.read_frame(data)
            if decoded and decoded[0] == sony.COMMAND:
                out.append(harness.hexstr(decoded[2]))
        return out


harness.pin_tests(globals(), "sony", Session)

V2_HANDSHAKE = "01 00 03 00 10 02 00 00"
V2_HANDSHAKE_XM = "01 00 03 00 20 16 00 00"
V1_HANDSHAKE = "01 00 70 00"


def ready(name="WH-CH720N"):
    s = Session(name=name)
    s.device(V2_HANDSHAKE)
    s.ack()
    s.device("67 17 01 01 00 00 0e")        # ANC, level 14
    return s


class Framing(unittest.TestCase):
    def test_round_trip(self):
        wire = sony.frame(sony.COMMAND, 0, bytes([sony.NCASM_GET, 0x17]))
        self.assertEqual(wire.hex(), "3e0c000000000266178b3c")
        self.assertEqual(sony.read_frame(wire), (sony.COMMAND, 0, b"\x66\x17"))

    def test_a_marker_in_the_payload_is_stuffed(self):
        wire = sony.frame(sony.COMMAND, 0, bytes([sony.NCASM_SET, 0x17, 1, 1, 1, 0, 0x3C]))
        self.assertNotIn(0x3C, wire[1:-1])
        self.assertEqual(sony.read_frame(wire)[2][-1], 0x3C)

    def test_every_marker_byte_survives_stuffing(self):
        payload = bytes([0x3C, 0x3D, 0x3E, 0x2C, 0x2D, 0x2E])
        self.assertEqual(sony.unstuff(sony.stuff(payload)), payload)

    def test_a_frame_split_anywhere_reads_once_whole(self):
        wire = sony.frame(sony.COMMAND, 0, bytes([sony.NCASM_NTFY, 0x17, 1, 1, 1, 0, 0x3C]))
        for cut in range(1, len(wire)):
            with self.subTest(cut=cut):
                buffer = bytearray(wire[:cut])
                self.assertEqual(sony.unframe(buffer), [])
                buffer += wire[cut:]
                self.assertEqual(len(sony.unframe(buffer)), 1)
                self.assertEqual(buffer, bytearray())

    def test_a_bad_sum_is_dropped_and_not_acknowledged(self):
        s = ready()
        before = list(s.frames)
        bad = bytearray(sony.frame(sony.COMMAND, 0, harness.hexbytes("69 17 01 01 01 00 0e")))
        bad[-2] ^= 1
        s.receive(bytes(bad))
        self.assertEqual(s.frames, before)
        self.assertEqual(s.lines[-1]["mode"], "anc")

    def test_noise_before_a_frame_is_skipped(self):
        s = ready()
        s.receive(b"noise\x00\xff" + sony.frame(sony.COMMAND, 0,
                                                 harness.hexbytes("69 17 01 00 00 00 0e")))
        self.assertEqual(s.lines[-1]["mode"], "off")


class Acknowledgement(unittest.TestCase):
    def test_every_device_frame_is_acked_with_the_other_seq(self):
        s = ready()
        s.receive(sony.frame(sony.COMMAND, 0, harness.hexbytes("69 17 01 01 01 00 0e")))
        self.assertEqual(s.frames[-1].hex(), "3e010100000000023c")
        s.receive(sony.frame(sony.COMMAND, 1, harness.hexbytes("69 17 01 00 00 00 0e")))
        self.assertEqual(s.frames[-1].hex(), "3e010000000000013c")

    def test_our_seq_is_the_last_acks(self):
        s = ready()
        s.command("set off")
        self.assertEqual(sony.read_frame(s.frames[-1])[1], 1)
        s.receive(sony.frame(sony.ACK, 0))
        s.command("set anc")
        self.assertEqual(sony.read_frame(s.frames[-1])[1], 0)

    def test_one_command_at_a_time(self):
        s = ready()
        s.command("set off")
        s.command("set ambient")
        self.assertEqual(s.sent[-1], "68 17 01 00 00 00 0e")
        s.ack()
        self.assertEqual(s.sent[-1], "68 17 01 01 01 00 0e")

    def test_a_lost_ack_frees_the_queue_after_its_deadline_only(self):
        s = ready()
        s.command("set off")
        s.command("set ambient")
        s.advance(2.9)
        self.assertEqual(s.sent[-1], "68 17 01 00 00 00 0e")
        s.advance(0.1)
        self.assertEqual(s.sent[-1], "68 17 01 01 01 00 0e")
        self.assertTrue(s.headset.waiting)
        self.assertIsNone(s.exit_code)

    def test_an_acked_command_leaves_no_deadline_behind(self):
        s = ready()
        s.command("set off")
        s.ack()
        s.command("set ambient")
        s.command("set anc")
        s.advance(2.0)
        s.ack()                                 # frees "set anc"
        s.advance(1.5)                          # past the first command's 3 s
        self.assertEqual(s.sent[-1], "68 17 01 01 00 00 0e")
        self.assertTrue(s.headset.waiting)


class Handshake(unittest.TestCase):
    def test_opening_sends_the_handshake(self):
        s = Session(name="WH-CH720N")
        s.do_open()
        self.assertEqual(s.sent, ["00 00"])

    def test_unanswered_handshake_is_transient_not_silent(self):
        s = Session(name="WH-CH720N")
        s.do_open()
        for _ in range(sony.GREET_ATTEMPTS):
            s.fire()
        self.assertEqual(s.sent, ["00 00"] * sony.GREET_ATTEMPTS)
        self.assertEqual(s.exit_code, TRANSIENT)
        self.assertEqual(s.lines[-1]["error"], "the Sony headset did not answer the handshake")

    def test_an_answered_handshake_is_not_sent_again(self):
        s = Session(name="WH-CH720N")
        s.do_open()
        s.device(V2_HANDSHAKE)
        s.ack()
        s.device("67 17 01 01 00 00 0e")
        s.advance(30)
        self.assertEqual(s.sent.count("00 00"), 1)
        self.assertIsNone(s.exit_code)

    def test_a_closed_channel_ends_the_run_once(self):
        s = ready()
        s.headset.closed("main", None)
        self.assertEqual(s.exit_code, TRANSIENT)
        self.assertEqual(s.lines[-1], {"modes": False, "error": "the Sony channel closed"})
        count = len(s.lines)
        s.headset.fail(0, "second ending")
        self.assertEqual(len(s.lines), count)
        self.assertEqual(s.exit_code, TRANSIENT)


class OneTypeAtATime(unittest.TestCase):
    def test_a_block_of_another_type_is_dropped_once_one_answered(self):
        s = Session(name="WH-CH720N")
        s.device(V2_HANDSHAKE)
        s.ack()
        s.device("67 17 01 01 01 00 14")
        s.device("69 02 01 02 02 01 00 00")     # v1-numbered, on a 0x17 headset
        self.assertEqual(len(s.lines), 1)
        self.assertEqual(s.headset.inquired, 0x17)

    def test_a_short_block_is_not_read(self):
        self.assertEqual(sony.CANDIDATES_V1, (0x02,))
        self.assertIsNone(sony.read_ncasm(bytes([sony.NCASM_RET, 0x02, 0x01, 0x02])))
        self.assertIsNone(sony.read_ncasm(harness.hexbytes("69 19 01 01 01 00 0a")))

    def test_a_volunteered_0x19_does_not_unsettle_0x17(self):
        # The XM6 answers its GET on 0x17 and volunteers 0x19. Both keep
        # reading, in either order, and the SET stays the 0x17 one.
        s = Session(name="WH-1000XM6")
        s.device(V2_HANDSHAKE_XM)
        s.ack()
        s.device("67 17 01 01 01 00 0f")
        s.device("69 19 01 01 00 00 0f 00 00")
        self.assertEqual(s.lines[-1]["mode"], "anc")
        s.device("69 17 01 00 00 00 0f")
        self.assertEqual(s.lines[-1]["mode"], "off")
        self.assertEqual(s.headset.inquired, 0x17)
        s.ack()                                 # the f2 10 behind 66 17
        s.command("set ambient")
        self.assertEqual(s.sent[-1], "68 17 01 01 01 00 0f")

    def test_a_0x19_that_comes_first_settles_on_0x17(self):
        s = Session(name="WH-1000XM6")
        s.device(V2_HANDSHAKE_XM)
        s.ack()
        s.device("69 19 01 01 01 00 0a 00 00")
        self.assertEqual(s.headset.inquired, 0x17)
        s.device("67 17 01 01 00 00 0a")
        self.assertEqual(s.lines[-1]["mode"], "anc")


class QuestionOrder(unittest.TestCase):
    """The handshake orders the questions. It never removes one."""

    def test_v2_handshake_asks_v2_first_and_v1_last(self):
        self.assertEqual(sony.question_order(8), (0x17, 0x15, 0x22, 0x02))

    def test_v1_handshake_asks_v1_first_and_v2_after(self):
        self.assertEqual(sony.question_order(4), (0x02, 0x17, 0x15, 0x22))

    def test_a_short_handshake_still_reaches_the_v2_questions(self):
        s = Session()
        s.device(V1_HANDSHAKE)
        self.assertEqual(s.sent, ["66 02"])
        s.ack()
        s.fire()                                # 0x02 goes unanswered
        s.ack()
        self.assertEqual(s.sent[-1], "66 17")
        s.device("67 17 01 01 01 00 14")
        self.assertEqual(s.headset.inquired, 0x17)
        self.assertEqual(s.lines[-1]["mode"], "ambient")
        self.assertIsNone(s.exit_code)

    def test_an_answer_stops_the_questions(self):
        s = ready()
        s.advance(10)
        self.assertEqual([p for p in s.sent if p.startswith("66")], ["66 17"])

    def test_every_candidate_has_a_mode_list(self):
        for candidate in sony.CANDIDATES:
            self.assertIn(candidate, sony.AVAILABLE)


class WearQuestion(unittest.TestCase):
    """Who is asked f2 10: a model with no row, and nobody without a name."""

    def test_a_model_nobody_has_held_is_asked(self):
        s = Session(name="WF-1000XM5")
        s.device(V2_HANDSHAKE_XM)
        s.ack()
        self.assertEqual(s.sent, ["66 17", "f2 10"])

    def test_no_name_is_the_old_caller_and_gets_the_old_frames(self):
        s = Session()
        s.device(V2_HANDSHAKE_XM)
        s.ack()
        self.assertEqual(s.sent, ["66 17"])

    def test_every_pinned_model_has_a_row(self):
        for _path, pin in harness.pins_for("sony"):
            name = pin["session"]["name"]
            self.assertTrue(name in sony.MODELS or name in sony.NO_MODES, pin["model"])

    def test_worn_waits_for_a_mode_to_ride_on(self):
        s = Session(name="WH-1000XM6")
        s.device(V2_HANDSHAKE_XM)
        s.device("f3 10 01")
        self.assertEqual(s.lines, [])
        s.device("67 17 01 01 00 00 0a")
        self.assertIs(s.lines[-1]["worn"], False)


class NoModes(unittest.TestCase):
    """NO_MODES names are asked nothing at all, and nobody else is affected."""

    HANDSHAKE = "01 00 03 00 10 01 00 00"

    def test_a_no_modes_name_is_asked_nothing_and_parked(self):
        s = Session(name="WH-CH520")
        s.device(self.HANDSHAKE)
        self.assertEqual(s.sent, [])
        self.assertEqual(s.exit_code, SILENT)

    def test_a_models_row_name_is_unaffected(self):
        s = Session(name="WH-CH720N")
        s.device(self.HANDSHAKE)
        s.ack()
        self.assertEqual(s.sent, ["66 17"])
        self.assertIsNone(s.exit_code)

    def test_no_modes_and_models_never_share_a_name(self):
        self.assertEqual(set(sony.NO_MODES) & set(sony.MODELS), set())

    def test_controls_and_pending_timers_send_nothing_after_parking(self):
        s = Session(name="WH-CH520")
        s.do_open()
        s.device(self.HANDSHAKE)
        before = list(s.frames)
        for command in ("set anc", "set off", "set ambient", "level 10", "voice on"):
            s.command(command)
        s.advance(30)
        s.headset.greet(2)
        s.headset.ask(0)
        self.assertEqual(s.frames, before)
        self.assertEqual(s.sent, ["00 00"])
        self.assertEqual(s.exit_code, SILENT)

    def test_reconnect_parks_again_without_changing_another_sony(self):
        peer = Session(name="WH-CH720N")
        peer.device(self.HANDSHAKE)
        peer_frames, peer_lines = list(peer.frames), list(peer.lines)
        for _ in range(2):
            s = Session(name="WH-CH520")
            s.device(self.HANDSHAKE)
            self.assertEqual(s.sent, [])
            self.assertEqual(s.exit_code, SILENT)
        self.assertEqual(peer.frames, peer_frames)
        self.assertEqual(peer.lines, peer_lines)
        self.assertIsNone(peer.exit_code)

    def test_unanswered_handshake_keeps_transient_failure(self):
        s = Session(name="WH-CH520")
        s.headset.greet(sony.GREET_ATTEMPTS + 1)
        self.assertEqual(s.exit_code, TRANSIENT)
        self.assertEqual(s.sent, [])


class V1AmbientLevel(unittest.TestCase):
    """The WH-1000XM4 reports level 0 in noise cancelling. Frames from
    docs/captures/sony-wh-1000xm4-ambient.txt."""

    def session(self):
        s = Session("v1", "WH-1000XM4")
        s.device(V1_HANDSHAKE)
        s.ack()
        s.device("67 02 01 02 02 01 00 00")     # RET: ANC, level 0
        return s

    def test_ambient_returns_to_the_last_reported_level(self):
        s = self.session()
        s.device("69 02 01 02 00 01 00 14")     # ambient, level 20
        s.device("69 02 01 02 02 01 00 00")     # ANC, level 0
        s.command("set ambient")
        self.assertEqual(s.sent[-1], "68 02 11 01 00 01 00 14")

    def test_nothing_remembered_starts_at_one(self):
        s = self.session()
        s.command("set ambient")
        self.assertEqual(s.sent[-1], "68 02 11 01 00 01 00 01")

    def test_a_level_asked_for_is_sent_as_asked(self):
        s = self.session()
        s.device("69 02 01 02 00 01 00 14")
        s.device("69 02 01 02 02 01 00 00")
        s.command("level 5")
        self.assertEqual(s.sent[-1], "68 02 11 01 00 01 00 05")

    def test_the_level_kept_while_off_is_remembered_too(self):
        s = self.session()
        s.device("69 02 00 02 00 01 00 14")     # effect off, level 20 kept
        s.device("69 02 01 02 02 01 00 00")
        s.command("set ambient")
        self.assertEqual(s.sent[-1], "68 02 11 01 00 01 00 14")

    def test_v2_headsets_are_sent_what_they_were(self):
        # A v2 level is not remembered: the Ambient SET carries the reported
        # level, zero included.
        s = Session("v2", "WH-CH720N")
        s.device(V2_HANDSHAKE)
        s.ack()
        s.device("67 17 01 01 01 00 0c")        # ambient, level 12
        s.device("69 17 01 01 00 00 00")        # ANC, level 0
        self.assertEqual(s.headset.ambient_level, 0)
        s.command("set ambient")
        self.assertEqual(s.sent[-1], "68 17 01 01 01 00 00")


class OtherLayouts(unittest.TestCase):
    """0x15 and 0x22 are asked of every v2 headset; no pin has one answering."""

    def answered(self, block):
        s = Session(name="WH-CH720N")
        s.device(V2_HANDSHAKE)
        s.ack()
        s.fire()
        s.ack()                                 # 0x15 asked
        s.device(block)
        return s

    def test_0x15_echoes_the_nc_value_it_reported(self):
        s = self.answered("67 15 01 01 00 03 00 0a")
        self.assertEqual(s.lines[-1]["mode"], "anc")
        s.command("set ambient")
        self.assertEqual(s.sent[-1], "68 15 01 01 01 03 00 0a")

    def test_0x22_is_ambient_only(self):
        s = self.answered("67 22 01 01 00 07")
        self.assertEqual(s.lines[-1]["available"], ["off", "ambient"])
        self.assertEqual(s.lines[-1]["mode"], "ambient")
        before = len(s.sent)
        s.command("set anc")
        self.assertEqual(len(s.sent), before)
        s.command("set off")
        self.assertEqual(s.sent[-1], "68 22 01 00 00 07")


class Commands(unittest.TestCase):
    def test_nothing_is_sent_before_a_type_answers(self):
        s = Session(name="WH-CH720N")
        for command in ("set anc", "level 5", "voice on"):
            s.command(command)
        self.assertEqual(s.frames, [])

    def test_impossible_commands_send_nothing(self):
        s = ready()
        before = list(s.frames)
        for command in ("", "set", "set talkthru", "set nonsense", "level nan",
                        "level 21", "level -1", "voice maybe", "latency on"):
            s.command(command)
        self.assertEqual(s.frames, before)

    def test_settings_change_only_when_the_device_reports_them(self):
        s = ready()
        s.command("level 5")
        s.ack()
        self.assertEqual(s.lines[-1]["level"], 14)
        s.device("69 17 01 01 01 00 05")
        self.assertEqual(s.lines[-1]["level"], 5)
        s.command("set off")
        self.assertEqual(s.sent[-1], "68 17 01 00 00 00 05")


class Silent(unittest.TestCase):
    def test_nothing_answers_and_the_address_is_parked(self):
        s = Session()
        s.device(V2_HANDSHAKE)
        for _ in sony.CANDIDATES:
            s.ack()
            s.fire()
        self.assertEqual(s.sent, ["66 17", "66 15", "66 22", "66 02"])
        self.assertEqual(s.exit_code, SILENT)
        self.assertEqual(s.lines[-1]["error"], "the headset did not answer the listening-mode query")


class Arguments(unittest.TestCase):
    def test_the_uuid_defaults_to_v2_and_the_name_to_none(self):
        options = sony.Sony.parse([ADDRESS.lower()])
        self.assertEqual(options, {"address": ADDRESS, "uuid": sony.UUID_V2, "name": ""})

    def test_all_three(self):
        options = sony.Sony.parse([ADDRESS, sony.UUID_V1.upper(), " WH-1000XM4 "])
        self.assertEqual(options, {"address": ADDRESS, "uuid": sony.UUID_V1, "name": "WH-1000XM4"})

    def test_an_empty_uuid_is_the_default(self):
        self.assertEqual(sony.Sony.parse([ADDRESS, "", "WH-CH720N"])["uuid"], sony.UUID_V2)

    def test_bad_arguments_are_setup(self):
        for args in ([], ["nope"], [ADDRESS, "not-a-uuid"], [ADDRESS, sony.UUID_V2, "x", "y"]):
            with self.subTest(args=args):
                with self.assertRaises(SetupError):
                    sony.Sony.parse(args)
        self.assertEqual(SETUP, 4)

    def test_the_link_is_the_profile_for_the_given_uuid(self):
        for alias, uuid in UUIDS.items():
            s = Session(alias)
            (spec,) = s.headset.links()
            self.assertEqual((spec.address, spec.uuid), (ADDRESS, uuid))


if __name__ == "__main__":
    unittest.main()
