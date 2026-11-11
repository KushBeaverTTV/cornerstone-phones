"""The two reference headsets, against their owner's recordings.

    python3 -m unittest tests.test_reference

Sony WH-CH720N and JBL TUNE230NC TWS were recorded end to end by their
owner (ncr): tests/fixtures/canonical.json names each frame and the packet
of the capture it was cut from. These tests do two things with that:

  Evidence      every fixture frame, and every device byte in the two
                reference pins, really occurs in the named capture
  Sony, Jbl     the recorded frames played at the brand modules, with the
                faults a radio link produces (splits, glue, noise, bad
                checksums, silence) mixed in. The fault bytes are made up;
                the frames they wrap are not
"""
import json
import pathlib
import re
import unittest

from tests import harness
from tests.test_jbl import Session as JblSession
from tests.test_sony import Session as SonySession
from brands import sony
from core.headset import CLEAN, TRANSIENT

ROOT = pathlib.Path(harness.ROOT)
REFERENCE = json.loads((ROOT / "tests/fixtures/canonical.json").read_text())


def recorded(brand, label, group="rx"):
    return bytes.fromhex(REFERENCE[brand][group][label]["hex"])


def capture_packets(brand):
    """{packet number: bytes} from a capture's decoded dump: a header line
    with "#<n>", then indented hex rows."""
    text = (ROOT / REFERENCE[brand]["capture"]).read_text()
    packets = {}
    for block in re.split(r"(?=^\S)", text, flags=re.M):
        if not block:
            continue
        number = re.search(r"#(\d+)\s", block.splitlines()[0])
        if number:
            rows = re.finditer(r"^        ((?:[0-9a-f]{2} ?)+)(?=  |$)", block, re.M)
            packets[int(number.group(1))] = b"".join(bytes.fromhex(row.group(1)) for row in rows)
    return packets


class Evidence(unittest.TestCase):
    def test_every_fixture_frame_is_in_its_packet(self):
        for brand, model in REFERENCE.items():
            packets = capture_packets(brand)
            for group in ("rx", "gfps"):
                for label, entry in model[group].items():
                    with self.subTest(brand=brand, frame=label):
                        self.assertIn(entry["packet"], packets)
                        self.assertIn(bytes.fromhex(entry["hex"]), packets[entry["packet"]])

    def test_the_reference_pins_use_only_recorded_bytes(self):
        for brand, stem in (("sony", "wh-ch720n"), ("jbl", "tune230nc-tws")):
            pin = json.loads((ROOT / "tests/pins" / brand / (stem + "-canonical.json")).read_text())
            self.assertEqual(pin["owner"], "ncr")
            self.assertEqual(pin["capture"], REFERENCE[brand]["capture"])
            packets = list(capture_packets(brand).values())
            for step in pin["steps"]:
                wire = self.radio_bytes(brand, step)
                if wire is not None:
                    with self.subTest(brand=brand, bytes=wire.hex()):
                        self.assertTrue(any(wire in packet for packet in packets))

    @staticmethod
    def radio_bytes(brand, step):
        if "device" in step:
            device = step["device"]
            if isinstance(device, dict):
                return bytes.fromhex(device["wire"])
            if "data bytes): " in device:
                return bytes.fromhex(device.split("data bytes): ")[1])
            return None     # btgatt-client's own chatter, not radio bytes
        if "sent_last" in step:
            sent = step["sent_last"]
            if brand == "jbl":      # "write-value 0x0010 0xaa 0x91 ..."
                return bytes.fromhex(" ".join(sent.split()[2:]).replace("0x", ""))
            return bytes.fromhex(sent)
        return None

    def test_the_sdp_records_are_the_owners_devices(self):
        for model in REFERENCE.values():
            text = (ROOT / model["uuid_capture"]).read_text()
            self.assertIn(model["address"], text)
            self.assertIn("UUID:", text)


ACK_SEQ_0 = "3e010100000000023c"    # the headset's frame ACKed, as recorded


class Sony(unittest.TestCase):
    def ready(self):
        s = SonySession(name="WH-CH720N")
        s.receive(recorded("sony", "handshake"))
        s.ack()
        s.receive(recorded("sony", "initial"))
        return s

    def test_a_split_frame_does_nothing_until_it_is_whole(self):
        wire = recorded("sony", "ambient")
        for cut in range(1, len(wire)):
            with self.subTest(cut=cut):
                s = self.ready()
                frames = list(s.frames)
                s.receive(wire[:cut])
                self.assertEqual(s.lines[-1]["mode"], "anc")
                self.assertEqual(s.frames, frames)
                s.receive(wire[cut:])
                self.assertEqual(s.lines[-1]["mode"], "ambient")
                self.assertEqual(s.frames[-1].hex(), ACK_SEQ_0)

    def test_glued_frames_and_a_repeat_are_each_acknowledged(self):
        s = self.ready()
        frames = len(s.frames)
        s.receive(recorded("sony", "ambient") * 2 + recorded("sony", "off"))
        self.assertEqual([line["mode"] for line in s.lines], ["anc", "ambient", "off"])
        self.assertEqual(len(s.frames), frames + 3)
        self.assertEqual(s.headset.buffer, bytearray())

    def test_a_bad_checksum_is_neither_acknowledged_nor_shown(self):
        s = self.ready()
        frames = list(s.frames)
        broken = bytearray(recorded("sony", "ambient"))
        broken[-2] ^= 1
        s.receive(bytes(broken))
        self.assertEqual(s.frames, frames)
        self.assertEqual(s.lines[-1]["mode"], "anc")
        s.receive(recorded("sony", "ambient"))
        self.assertEqual(s.lines[-1]["mode"], "ambient")

    def test_noise_in_front_of_a_frame_is_skipped(self):
        s = self.ready()
        s.receive(b"noise\x00\xff" + recorded("sony", "off"))
        self.assertEqual(s.lines[-1]["mode"], "off")

    def test_commands_it_cannot_take_send_nothing(self):
        for s in (SonySession(name="WH-CH720N"), self.ready()):
            frames = list(s.frames)
            for line in ("", "set", "set talkthru", "set nonsense", "level nan",
                         "voice maybe", "latency on"):
                s.command(line)
            self.assertEqual(s.frames, frames)
        s = SonySession(name="WH-CH720N")
        for line in ("set anc", "level 5", "voice on"):
            s.command(line)
        self.assertEqual(s.frames, [], "nothing before the headset has answered")

    def test_a_setting_moves_only_when_the_headset_reports_it(self):
        s = self.ready()
        s.command("level 5")
        s.ack()
        self.assertEqual(s.lines[-1]["level"], 14)
        s.receive(recorded("sony", "level5"))
        self.assertEqual(s.lines[-1]["level"], 5)
        s.command("set off")
        self.assertEqual(s.sent[-1], "68 17 01 00 00 00 05")

    def test_a_lost_ack_frees_the_queue_after_its_deadline(self):
        s = self.ready()
        s.command("set off")
        s.command("set ambient")
        self.assertEqual(s.sent[-1], "68 17 01 00 00 00 0e", "the second waits its turn")
        s.advance(3.0)
        self.assertEqual(s.sent[-1], "68 17 01 01 01 00 0e")
        self.assertTrue(s.headset.waiting)
        self.assertIsNone(s.exit_code)

    def test_an_unanswered_handshake_is_a_link_problem_not_silence(self):
        s = SonySession(name="WH-CH720N")
        s.do_open()
        for _ in range(sony.GREET_ATTEMPTS):
            s.fire()
        self.assertEqual(s.sent, ["00 00"] * sony.GREET_ATTEMPTS)
        self.assertEqual(s.exit_code, TRANSIENT)

    def test_a_dropped_channel_ends_the_run_once(self):
        s = self.ready()
        s.headset.closed("main", None)
        self.assertEqual(s.exit_code, TRANSIENT)
        self.assertEqual(s.lines[-1], {"modes": False, "error": "the Sony channel closed"})
        count = len(s.lines)
        s.headset.fail(CLEAN, "a second ending")
        self.assertEqual((s.exit_code, len(s.lines)), (TRANSIENT, count))


class Jbl(unittest.TestCase):
    MODEL = "0x1234"

    def notify(self, s, mode):
        data = recorded("jbl", mode).hex(" ")
        s.device("Handle Value Not/Ind: 0x000c - (10 data bytes): " + data)

    def test_all_four_recorded_answers_and_changes_made_on_the_buds(self):
        s = JblSession()
        for mode in ("off", "anc", "ambient", "talkthru"):
            self.notify(s, mode)
            self.assertEqual(s.lines[-1], {"modes": True, "mode": mode})
        self.assertEqual(s.support, [("answered", self.MODEL)])

    def test_a_set_is_not_a_report(self):
        s = JblSession()
        self.notify(s, "off")
        s.command("set anc")
        self.assertEqual(s.lines, [{"modes": True, "mode": "off"}])
        self.notify(s, "anc")
        self.assertEqual(s.lines[-1]["mode"], "anc")

    def test_short_or_foreign_lines_invent_no_mode(self):
        s = JblSession()
        self.notify(s, "ambient")
        for line in ("", "random log", "aa 91 07 12", "aa 91 07 12 01 00 02 00",
                     "aa 92 07 12 01 00 02 00 03 00"):
            s.device(line)
        self.assertEqual(s.lines, [{"modes": True, "mode": "ambient"}])
        self.notify(s, "anc")
        self.assertEqual(s.lines[-1]["mode"], "anc")

    def test_commands_it_cannot_take_send_nothing(self):
        s = JblSession()
        self.notify(s, "off")
        written = list(s.sent)
        for line in ("", "set", "set unknown", "level 5", "voice on", "latency on"):
            s.command(line)
        self.assertEqual(s.sent, written)

    def test_a_link_failure_never_retires_the_model(self):
        for line in ("GATT discovery procedures failed", "Failed to register notify handler",
                     "GATT client not initialized", "Device disconnected"):
            with self.subTest(line=line):
                s = JblSession()
                s.device(line)
                self.assertEqual(s.exit_code, TRANSIENT)
                s.headset.answer_overdue()
                self.assertEqual(s.support, [])
                self.assertEqual(s.exit_code, TRANSIENT)

    def test_a_report_before_the_deadline_is_never_a_miss(self):
        s = JblSession()
        self.notify(s, "anc")
        s.headset.answer_overdue()
        self.assertEqual(s.support, [("answered", self.MODEL)])
        self.assertIsNone(s.exit_code)

    def test_the_first_ending_stands_and_nothing_follows_it(self):
        s = JblSession()
        s.headset.stop()
        s.headset.fail(TRANSIENT, "a late end of file")
        self.assertEqual((s.exit_code, s.lines), (CLEAN, []))
        written = list(s.sent)
        s.advance(60)
        self.assertEqual(s.sent, written)


if __name__ == "__main__":
    unittest.main()
