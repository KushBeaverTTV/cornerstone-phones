"""Nothing and CMF (NT Link): the frozen sessions, the framing, the captures
replayed, timing, channel choice and the case memory.

    python3 -m unittest tests.test_nothing

Frames the device sends come from docs/captures wherever a capture has them,
never from the encoder under test. Damaged bytes, refused connections and a
moving clock are synthetic fault injection, not hardware observations.
"""
import json
import os
import pathlib
import re
import shutil
import tempfile
import unittest
from unittest import mock

from tests import harness
from brands import nothing
from core.headset import MAIN, SETUP, SILENT, TRANSIENT, SetupError
from core.links import Rfcomm

ADDRESS = "3C:B0:ED:AF:7C:30"


class Session(harness.Session):
    """A Nothing session. "device" in a pin is {"cmd": "1e", "dir": "40",
    "payload": hex}: the command, the direction byte and the payload (empty
    when absent), framed with a CRC the way the device frames them and read
    through the brand's own framer. "sent" is every whole frame written, as
    hex. "open" is the channel coming up, which asks for device info first;
    "poll" is one tick of the mode poll.

    The case memory lives in a temporary directory of the session's own (or
    one handed in, to play a second run against the first one's file), on a
    wall clock the test can move.
    """

    def __init__(self, name="", directory=None, address=ADDRESS):
        super().__init__()
        self.owned = directory is None
        self.directory = directory or tempfile.mkdtemp(prefix="cornerstone-nothing-")
        self.wall = [1_800_000_000.0]
        self.memory = nothing.CaseMemory(os.path.join(self.directory, "nothing-case.json"),
                                         clock=lambda: self.wall[0])
        self.start(nothing.Nothing, address=address, channels=nothing.channels_for(name),
                   case_memory=self.memory)

    def close(self):
        if self.owned:
            shutil.rmtree(self.directory, ignore_errors=True)

    def device(self, spec):
        self.receive(nothing.frame(int(spec["cmd"], 16), int(spec["dir"], 16),
                                   harness.hexbytes(spec.get("payload", ""))))

    def unlocked(self):
        """Open, and answer device info, as every capture starts."""
        self.do_open()
        self.device({"cmd": "06", "dir": "40", "payload": "00"})
        return self


def session(test, *args, **kwargs):
    s = Session(*args, **kwargs)
    test.addCleanup(s.close)
    return s


harness.pin_tests(globals(), "nothing", Session)


# ---- The captures, read the way the probe printed them

def captured(name, skip_device_info):
    rows = []
    path = pathlib.Path(harness.ROOT) / "docs" / "captures" / name
    for number, line in enumerate(path.read_text().splitlines(), 1):
        found = re.search(r"\b55(?: [0-9a-fA-F]{2})+", line)
        if found and not (skip_device_info and "device-info    answer" in line):
            rows.append((number, "<-" if "<-" in line else "->", bytes.fromhex(found.group())))
    return rows


# The Headphone Pro capture masks the device-info serial, which breaks that
# one frame's CRC, so it is left out rather than reconstructed.
PRO = captured("nothing-headphone-pro.txt", skip_device_info=True)
BUDS2 = captured("nothing-cmf-buds-2.txt", skip_device_info=False)


def from_capture(rows, command, direction, payload):
    """A whole captured reply, its own header and CRC included."""
    want = bytes.fromhex(payload)
    for _, flow, raw in rows:
        size = int.from_bytes(raw[5:7], "little")
        if flow == "<-" and raw[3:5] == bytes((command, direction)) and raw[8:8 + size] == want:
            return raw
    raise AssertionError("no %02x %02x %s in the capture" % (command, direction, payload))


def replies(rows):
    return [raw for _, flow, raw in rows if flow == "<-"]


def writes(rows):
    return [raw for _, flow, raw in rows if flow == "->"]


ANC_GET = "55 60 01 1e c0 00 00 01 b1 1d"
LATENCY_GET = "55 60 01 41 c0 00 00 01 25 10"


class Framing(unittest.TestCase):
    ANC_ADAPTIVE = harness.hexbytes("55 60 01 1e 40 06 00 01 01 04 00 02 04 00 88 ba")
    BATTERY_15 = harness.hexbytes("55 60 01 07 40 03 00 01 01 06 0f 0b 8e")

    def test_the_noise_control_get_matches_the_worked_example(self):
        # PROTOCOL.md: 55 60 01 1E C0 00 00 01 B1 1D asks for the state.
        self.assertEqual(harness.hexstr(nothing.frame(nothing.NOISE_GET, nothing.GET)), ANC_GET)

    def test_a_frame_with_a_bad_crc_is_dropped(self):
        damaged = bytearray(self.ANC_ADAPTIVE)
        damaged[-1] ^= 0xFF
        self.assertEqual(nothing.unframe(damaged), [])
        self.assertEqual(damaged, bytearray())

    def test_a_real_answer_split_at_every_byte_reads_once(self):
        for cut in range(1, len(self.ANC_ADAPTIVE)):
            with self.subTest(cut=cut):
                buffer = bytearray(self.ANC_ADAPTIVE[:cut])
                self.assertEqual(nothing.unframe(buffer), [])
                buffer += self.ANC_ADAPTIVE[cut:]
                ((command, direction, payload),) = nothing.unframe(buffer)
                self.assertEqual((command, direction), (0x1E, 0x40))
                self.assertEqual(nothing.noise_state(payload), ("anc", "adaptive"))

    def test_two_answers_in_one_read_are_both_taken(self):
        buffer = bytearray(self.BATTERY_15 + self.ANC_ADAPTIVE)
        frames = nothing.unframe(buffer)
        self.assertEqual([(c, d) for c, d, _ in frames], [(0x07, 0x40), (0x1E, 0x40)])
        self.assertEqual(buffer, bytearray())

    def test_noise_before_a_frame_is_skipped(self):
        buffer = bytearray(b"\x00\x13" + self.BATTERY_15)
        self.assertEqual(nothing.unframe(buffer), [(0x07, 0x40, b"\x01\x06\x0f")])

    def test_the_event_form_without_a_crc_parses(self):
        # ctrl 0x0300: bit 0x20 clear, so no trailer (the CMF sends these).
        buffer = bytearray(harness.hexbytes("55 00 03 03 e0 06 00 00 01 04 00 02 04 00"))
        ((command, direction, payload),) = nothing.unframe(buffer)
        self.assertEqual((command, direction), (0x03, 0xE0))
        self.assertEqual(nothing.noise_state(payload), ("anc", "adaptive"))


class Reading(unittest.TestCase):
    def test_every_mode_and_strength_the_cmf_reported(self):
        seen = {
            "01 05 00 02 04 00": ("off", "adaptive"),
            "01 07 00 02 04 00": ("ambient", "adaptive"),
            "01 04 00 02 04 00": ("anc", "adaptive"),
            "01 03 00 02 03 00": ("anc", "low"),
            "01 02 00 02 02 00": ("anc", "mid"),
            "01 01 00 02 01 00": ("anc", "high"),
        }
        for payload, expected in seen.items():
            self.assertEqual(nothing.noise_state(harness.hexbytes(payload)), expected, payload)

    def test_the_three_byte_form_carries_the_strength_in_the_mode_byte(self):
        self.assertEqual(nothing.noise_state(harness.hexbytes("01 01 00")), ("anc", "high"))
        self.assertEqual(nothing.noise_state(harness.hexbytes("01 07 00")), ("ambient", None))
        self.assertEqual(nothing.noise_state(harness.hexbytes("01 05 00")), ("off", None))

    def test_a_truncated_noise_payload_yields_nothing(self):
        self.assertEqual(nothing.noise_state(b""), (None, None))
        self.assertEqual(nothing.noise_state(b"\x01"), (None, None))

    def test_an_unknown_mode_byte_is_not_forced_into_a_mode(self):
        self.assertIsNone(nothing.noise_state(harness.hexbytes("01 6f 00"))[0])

    def test_the_battery_example_in_protocol_md(self):
        self.assertEqual(nothing.battery_state(harness.hexbytes("03 02 55 03 0f 04 55")),
                         ({"left": 85, "right": 15, "case": 85}, []))

    def test_an_unknown_component_is_skipped(self):
        self.assertEqual(nothing.battery_state(harness.hexbytes("02 09 40 06 0f")),
                         ({"headset": 15}, []))

    def test_a_level_over_100_is_dropped_not_clamped(self):
        self.assertEqual(nothing.battery_state(harness.hexbytes("01 06 65")), ({}, []))

    def test_the_charging_bit_is_read_off_the_level(self):
        # Synthetic: no capture has seen a part charging.
        self.assertEqual(nothing.battery_state(harness.hexbytes("01 06 8f")),
                         ({"headset": 15}, ["headset"]))

    def test_an_empty_or_short_battery_payload(self):
        self.assertEqual(nothing.battery_state(b""), ({}, []))
        self.assertEqual(nothing.battery_state(harness.hexbytes("03 02 55 03")), ({"left": 85}, []))


class HeadphoneProCapture(unittest.TestCase):
    """docs/captures/nothing-headphone-pro.txt, replayed byte for byte."""

    def setUp(self):
        self.s = session(self, "CMF Headphone Pro")

    def test_every_unredacted_frame_frames_whole(self):
        self.assertEqual(len(PRO), 64)
        for number, _, raw in PRO:
            with self.subTest(line=number):
                buffer = bytearray(raw)
                self.assertEqual(len(nothing.unframe(buffer)), 1)
                self.assertEqual(buffer, bytearray())

    def test_every_reply_survives_every_split(self):
        for raw in replies(PRO):
            expected = nothing.unframe(bytearray(raw))
            for cut in range(1, len(raw)):
                with self.subTest(frame=raw.hex(" "), cut=cut):
                    buffer = bytearray(raw[:cut])
                    self.assertEqual(nothing.unframe(buffer), [])
                    buffer += raw[cut:]
                    self.assertEqual(nothing.unframe(buffer), expected)
                    self.assertEqual(buffer, bytearray())

    def test_every_control_writes_the_captured_frame_and_reads_back(self):
        s = self.s
        s.receive(from_capture(PRO, 0x1E, 0x40, "01 04 00 02 04 00"))
        s.receive(from_capture(PRO, 0x07, 0x40, "01 06 0f"))
        s.receive(from_capture(PRO, 0x41, 0x40, "02"))
        cases = [
            ("set off", "01 05 00", "01 05 00 02 04 00", "off", "adaptive"),
            ("set ambient", "01 07 00", "01 07 00 02 04 00", "ambient", "adaptive"),
            ("set anc", "01 04 00", "01 04 00 02 04 00", "anc", "adaptive"),
            ("level low", "01 03 00", "01 03 00 02 03 00", "anc", "low"),
            ("level mid", "01 02 00", "01 02 00 02 02 00", "anc", "mid"),
            ("level high", "01 01 00", "01 01 00 02 01 00", "anc", "high"),
            ("level adaptive", "01 04 00", "01 04 00 02 04 00", "anc", "adaptive"),
            ("latency on", "01", "01", "anc", "adaptive"),
            ("latency off", "02", "02", "anc", "adaptive"),
        ]
        for line, outgoing, answer, mode, level in cases:
            with self.subTest(command=line):
                latency = line.startswith("latency")
                command = nothing.LATENCY_SET if latency else nothing.NOISE_SET
                before = len(s.lines)
                s.command(line)
                sent = s.frames[-1]
                self.assertIn(sent, writes(PRO))
                self.assertEqual(sent[3:5], bytes((command, 0xF0)))
                self.assertEqual(sent[8:-2], bytes.fromhex(outgoing))
                s.receive(from_capture(PRO, command, 0x70, ""))
                self.assertEqual(len(s.lines), before, "a command and its ack print nothing")
                count = len(s.sent)
                s.advance(nothing.AFTER_ACK_MS / 1000)
                self.assertEqual(s.sent[count:], [ANC_GET, LATENCY_GET])
                s.receive(from_capture(PRO, 0x41 if latency else 0x1E, 0x40, answer))
                self.assertEqual(s.lines[-1]["mode"], mode)
                self.assertEqual(s.lines[-1]["ancLevel"], level)
                if latency:
                    self.assertEqual(s.lines[-1]["latency"], line.endswith("on"))

    def test_announcements_deduplicate_and_damage_does_not_hide_the_next_frame(self):
        s = self.s
        mode = from_capture(PRO, 0x03, 0xE0, "01 04 00 02 04 00")
        battery = from_capture(PRO, 0x01, 0xE0, "01 06 0f")
        s.receive(mode + battery)
        self.assertEqual(s.lines[-1]["battery"]["headset"], 15)
        count = len(s.lines)
        s.receive(mode + battery)
        self.assertEqual(len(s.lines), count)
        damaged = bytearray(from_capture(PRO, 0x1E, 0x40, "01 01 00 02 01 00"))
        damaged[-1] ^= 0xFF
        s.receive(bytes(damaged) + from_capture(PRO, 0x03, 0xE0, "01 02 00 02 02 00"))
        self.assertEqual(s.lines[-1]["ancLevel"], "mid")
        self.assertEqual(len(s.lines), count + 1)


class Buds2Capture(unittest.TestCase):
    """docs/captures/nothing-cmf-buds-2.txt, replayed byte for byte, the
    device-info answer included (its CRC is intact)."""

    def setUp(self):
        self.s = session(self, "CMF Buds 2", address="3C:B0:ED:D0:AC:0B")

    def test_every_frame_frames_whole(self):
        self.assertEqual(len(BUDS2), 63)
        for number, _, raw in BUDS2:
            with self.subTest(line=number):
                buffer = bytearray(raw)
                self.assertEqual(len(nothing.unframe(buffer)), 1)
                self.assertEqual(buffer, bytearray())

    def test_the_real_info_answer_unlocks_the_captured_gets(self):
        s = self.s
        s.do_open()
        self.assertEqual(s.frames, writes(BUDS2)[:1])
        info = next(raw for raw in replies(BUDS2) if raw[3:5] == bytes((0x06, 0x40)))
        s.receive(info)
        self.assertEqual(s.frames, writes(BUDS2)[:4])
        self.assertEqual(s.lines, [])

    def test_every_reply_survives_every_split_and_the_joined_stream(self):
        stream = []
        for raw in replies(BUDS2):
            expected = nothing.unframe(bytearray(raw))
            stream += expected
            for cut in range(1, len(raw)):
                with self.subTest(frame=raw.hex(" "), cut=cut):
                    buffer = bytearray(raw[:cut])
                    self.assertEqual(nothing.unframe(buffer), [])
                    buffer += raw[cut:]
                    self.assertEqual(nothing.unframe(buffer), expected)
        buffer = bytearray(b"".join(replies(BUDS2)))
        self.assertEqual(nothing.unframe(buffer), stream)
        self.assertEqual(buffer, bytearray())

    def test_announcements_deduplicate_and_a_bad_crc_is_skipped(self):
        s = self.s
        mode = from_capture(BUDS2, 0x03, 0xE0, "01 07 00 02 01 00")
        latency = next(raw for raw in replies(BUDS2)
                       if raw[:5] == bytes.fromhex("55 20 01 41 40") and raw[8] == 1)
        s.receive(mode + latency)
        self.assertEqual(s.lines[-1]["mode"], "ambient")
        self.assertTrue(s.lines[-1]["latency"])
        count = len(s.lines)
        s.receive(mode + latency)
        self.assertEqual(len(s.lines), count)
        damaged = bytearray(from_capture(BUDS2, 0x03, 0xE0, "01 03 00 02 03 00"))
        damaged[-1] ^= 0xFF
        s.receive(bytes(damaged))
        self.assertEqual(len(s.lines), count)
        s.receive(from_capture(BUDS2, 0x03, 0xE0, "01 02 00 02 02 00"))
        self.assertEqual(s.lines[-1]["ancLevel"], "mid")
        self.assertEqual(len(s.lines), count + 1)

    def test_controls_and_the_case_battery(self):
        s = self.s
        s.receive(from_capture(BUDS2, 0x1E, 0x40, "01 05 00 02 01 00"))
        s.receive(from_capture(BUDS2, 0x07, 0x40, "02 02 64 03 64"))
        s.receive(from_capture(BUDS2, 0x41, 0x40, "02"))
        self.assertEqual((s.lines[-1]["mode"], s.lines[-1]["ancLevel"]), ("off", "high"))
        self.assertEqual(s.lines[-1]["battery"], {"left": 100, "right": 100,
                                                  "charging": [], "caseStale": False})

        s.receive(from_capture(BUDS2, 0x01, 0xE0, "03 02 64 03 64 04 55"))     # lid open
        self.assertEqual(s.lines[-1]["battery"]["case"], 85)
        self.assertFalse(s.lines[-1]["battery"]["caseStale"])
        s.receive(from_capture(BUDS2, 0x07, 0x40, "02 02 64 03 64"))           # lid shut
        self.assertEqual(s.lines[-1]["battery"]["case"], 85)
        self.assertTrue(s.lines[-1]["battery"]["caseStale"])

        cases = [
            ("set ambient", "01 07 00", "01 07 00 02 01 00", "ambient", "high"),
            ("set anc", "01 01 00", "01 01 00 02 01 00", "anc", "high"),
            ("level adaptive", "01 04 00", "01 04 00 02 04 00", "anc", "adaptive"),
            ("level low", "01 03 00", "01 03 00 02 03 00", "anc", "low"),
            ("level mid", "01 02 00", "01 02 00 02 02 00", "anc", "mid"),
            ("level high", "01 01 00", "01 01 00 02 01 00", "anc", "high"),
            ("set off", "01 05 00", "01 05 00 02 01 00", "off", "high"),
            ("latency on", "01", "01", "off", "high"),
            ("latency off", "02", "02", "off", "high"),
        ]
        for line, outgoing, answer, mode, level in cases:
            with self.subTest(command=line):
                latency = line.startswith("latency")
                command = nothing.LATENCY_SET if latency else nothing.NOISE_SET
                before = len(s.lines)
                s.command(line)
                sent = s.frames[-1]
                self.assertIn(sent, writes(BUDS2))
                self.assertEqual(sent[3:5], bytes((command, 0xF0)))
                self.assertEqual(sent[8:-2], bytes.fromhex(outgoing))
                ack = "00" if not latency else ("01" if line.endswith("on") else "02")
                s.receive(from_capture(BUDS2, command, 0x70, ack))
                self.assertEqual(len(s.lines), before)
                s.advance(nothing.AFTER_ACK_MS / 1000)
                s.receive(from_capture(BUDS2, 0x41 if latency else 0x1E, 0x40, answer))
                self.assertEqual(s.lines[-1]["mode"], mode)
                self.assertEqual(s.lines[-1]["ancLevel"], level)
                if latency:
                    self.assertEqual(s.lines[-1]["latency"], line.endswith("on"))


class Lifecycle(unittest.TestCase):
    def test_a_late_info_answer_does_not_hold_the_gets_back(self):
        s = session(self, "CMF Headphone Pro")
        s.do_open()
        s.advance(1.4)
        self.assertEqual(len(s.sent), 1)
        s.advance(0.1)
        self.assertEqual([frame[3] for frame in s.frames], [0x06, 0x07, 0x1E, 0x41])

    def test_silence_parks_the_address(self):
        s = session(self, "CMF Headphone Pro")
        s.do_open()
        s.advance(6.4)
        self.assertIsNone(s.exit_code)
        s.advance(0.1)
        self.assertEqual(s.exit_code, SILENT)
        self.assertEqual(s.lines, [{"modes": False,
                                    "error": "the device did not answer the noise-control query"}])

    def test_a_battery_answer_alone_is_still_silence(self):
        s = session(self).unlocked()
        s.device({"cmd": "07", "dir": "40", "payload": "01 06 0f"})
        self.assertEqual(s.lines, [])
        s.advance(6.5)
        self.assertEqual(s.exit_code, SILENT)

    def test_a_mode_answer_keeps_the_run_going(self):
        s = session(self).unlocked()
        s.device({"cmd": "1e", "dir": "40", "payload": "01 01 00"})
        s.advance(30)
        self.assertIsNone(s.exit_code)

    def test_nothing_is_written_before_the_mode_is_known(self):
        s = session(self)
        s.do_open()
        s.command("set anc")
        s.command("level low")
        s.command("latency on")
        self.assertEqual(len(s.sent), 1)

    def test_latency_is_not_set_before_the_device_has_answered_for_it(self):
        s = session(self).unlocked()
        s.device({"cmd": "1e", "dir": "40", "payload": "01 01 00"})
        before = list(s.sent)
        s.command("latency on")
        self.assertEqual(s.sent, before)

    def test_words_outside_the_vocabulary_send_nothing(self):
        s = session(self).unlocked()
        s.device({"cmd": "1e", "dir": "40", "payload": "01 04 00 02 04 00"})
        before = list(s.sent)
        s.device({"cmd": "41", "dir": "40", "payload": "01"})
        for line in ("set purple", "level eleven", "latency maybe", "set", "volume 3"):
            s.command(line)
        self.assertEqual(s.sent, before)
        self.assertEqual(s.timers, [nothing.POLL_MS])

    def test_frames_never_asked_for_are_ignored(self):
        s = session(self).unlocked()
        s.device({"cmd": "1e", "dir": "40", "payload": "01 04 00 02 04 00"})
        settled = list(s.lines)
        s.device({"cmd": "19", "dir": "e0", "payload": "01 00"})
        s.device({"cmd": "09", "dir": "7c", "payload": "00 04 00"})
        s.device({"cmd": "1e", "dir": "c0", "payload": "01 07 00"})   # a get, not an answer
        self.assertEqual(s.lines, settled)
        self.assertIsNone(s.exit_code)

    def test_the_poll_asks_the_mode_and_every_fifth_time_the_battery(self):
        s = session(self).unlocked()
        s.device({"cmd": "1e", "dir": "40", "payload": "01 01 00"})
        before = len(s.sent)
        s.advance(4 * nothing.POLL_MS / 1000)
        self.assertEqual(s.sent[before:], [ANC_GET] * 4)
        s.advance(nothing.POLL_MS / 1000)
        self.assertEqual(s.sent[before + 4:], [ANC_GET, "55 60 01 07 c0 00 00 01 ac df"])

    def test_one_read_back_per_set_sooner_once_acked(self):
        s = session(self).unlocked()
        s.device({"cmd": "1e", "dir": "40", "payload": "01 01 00"})
        s.device({"cmd": "41", "dir": "40", "payload": "02"})
        s.command("set off")
        count = len(s.sent)
        s.advance(0.3)
        s.device({"cmd": "0f", "dir": "70"})
        s.advance(0.39)
        self.assertEqual(len(s.sent), count)
        s.advance(0.01)
        self.assertEqual(s.sent[count:], [ANC_GET, LATENCY_GET])
        s.advance(0.5)
        self.assertEqual(len(s.sent), count + 2, "the unacked read-back was replaced, not added")

    def test_an_unacked_set_is_read_back_anyway(self):
        s = session(self).unlocked()
        s.device({"cmd": "1e", "dir": "40", "payload": "01 01 00"})
        s.command("set ambient")
        count = len(s.sent)
        s.advance(nothing.AFTER_SET_MS / 1000)
        self.assertEqual(s.sent[count:], [ANC_GET, LATENCY_GET])

    def test_a_closed_channel_is_transient(self):
        s = session(self).unlocked()
        s.device({"cmd": "1e", "dir": "40", "payload": "01 01 00 02 01 00"})
        s.headset.closed(MAIN, None)
        self.assertEqual(s.exit_code, TRANSIENT)
        self.assertEqual(s.lines[-1], {"modes": False, "error": "the Nothing channel closed"})

    def test_a_channel_that_never_opens_is_transient_and_says_why(self):
        s = session(self)
        s.headset.unavailable(MAIN, "cannot open the Nothing channel: Connection refused", TRANSIENT)
        self.assertEqual(s.exit_code, TRANSIENT)
        self.assertEqual(s.lines[-1]["modes"], False)

    def test_two_headsets_do_not_share_state(self):
        first = session(self, "CMF Headphone Pro").unlocked()
        first.device({"cmd": "1e", "dir": "40", "payload": "01 01 00 02 01 00"})
        first_lines = list(first.lines)
        second = session(self, "CMF Headphone Pro", address="2C:BE:EE:3C:6F:FE").unlocked()
        second.device({"cmd": "1e", "dir": "40", "payload": "01 07 00 02 04 00"})
        second.device({"cmd": "07", "dir": "40", "payload": "01 06 63"})
        self.assertEqual((first.headset.mode, first.headset.level), ("anc", "high"))
        self.assertEqual(first.lines, first_lines)
        self.assertEqual(second.headset.mode, "ambient")
        self.assertEqual(second.headset.levels, {"headset": 99})

    def test_a_fresh_run_after_a_drop_starts_from_what_it_hears(self):
        first = session(self, "CMF Headphone Pro").unlocked()
        first.device({"cmd": "1e", "dir": "40", "payload": "01 01 00 02 01 00"})
        first.headset.closed(MAIN, None)
        second = session(self, "CMF Headphone Pro").unlocked()
        second.device({"cmd": "07", "dir": "40", "payload": "01 06 0f"})
        second.device({"cmd": "1e", "dir": "40", "payload": "01 07 00 02 04 00"})
        self.assertEqual(second.lines[0]["mode"], "ambient")
        self.assertEqual(second.lines[0]["battery"]["headset"], 15)
        self.assertEqual(second.headset.level, "adaptive")
        self.assertEqual(first.headset.level, "high")


class CaseMemory(unittest.TestCase):
    def test_the_last_case_reading_outlives_the_run_and_shows_stale(self):
        first = session(self)
        first.unlocked()
        first.device({"cmd": "1e", "dir": "40", "payload": "01 01 00"})
        first.device({"cmd": "07", "dir": "40", "payload": "03 02 55 03 0f 04 46"})
        second = session(self, directory=first.directory)
        second.unlocked()
        second.device({"cmd": "1e", "dir": "40", "payload": "01 01 00"})
        self.assertEqual(second.lines[-1]["battery"],
                         {"case": 70, "charging": [], "caseStale": True})
        second.device({"cmd": "07", "dir": "40", "payload": "03 02 55 03 0f 04 47"})
        self.assertEqual(second.lines[-1]["battery"]["case"], 71)
        self.assertFalse(second.lines[-1]["battery"]["caseStale"])

    def test_a_reading_older_than_six_hours_is_not_recalled(self):
        directory = tempfile.mkdtemp(prefix="cornerstone-nothing-")
        self.addCleanup(shutil.rmtree, directory, ignore_errors=True)
        wall = [1_800_000_000.0]
        memory = nothing.CaseMemory(os.path.join(directory, "nothing-case.json"), clock=lambda: wall[0])
        memory.remember(ADDRESS.lower(), 40)
        wall[0] += 6 * 3600
        self.assertEqual(memory.recall(ADDRESS), 40)
        wall[0] += 1
        self.assertIsNone(memory.recall(ADDRESS))

    def test_the_file_is_the_old_bridges_shape_keyed_by_address(self):
        s = session(self).unlocked()
        s.device({"cmd": "07", "dir": "40", "payload": "01 04 55"})
        with open(s.memory.path) as handle:
            self.assertEqual(json.load(handle), {ADDRESS: {"level": 85, "saved": 1_800_000_000}})
        self.assertEqual(os.listdir(s.directory), ["nothing-case.json"], "no temporary left behind")

    def test_an_existing_file_from_the_old_bridge_is_read(self):
        directory = tempfile.mkdtemp(prefix="cornerstone-nothing-")
        self.addCleanup(shutil.rmtree, directory, ignore_errors=True)
        path = os.path.join(directory, "nothing-case.json")
        with open(path, "w") as handle:
            handle.write('{"%s":{"level":62,"saved":1799999000},"AA:BB:CC:DD:EE:FF":{"level":5,"saved":1}}'
                         % ADDRESS)
        memory = nothing.CaseMemory(path, clock=lambda: 1_800_000_000.0)
        self.assertEqual(memory.recall(ADDRESS), 62)
        self.assertIsNone(memory.recall("AA:BB:CC:DD:EE:FF"))
        memory.remember("AA:BB:CC:DD:EE:FF", 9)
        with open(path) as handle:
            self.assertEqual(json.load(handle)[ADDRESS], {"level": 62, "saved": 1799999000})

    def test_a_damaged_or_missing_file_is_no_reading(self):
        directory = tempfile.mkdtemp(prefix="cornerstone-nothing-")
        self.addCleanup(shutil.rmtree, directory, ignore_errors=True)
        path = os.path.join(directory, "nothing-case.json")
        memory = nothing.CaseMemory(path, clock=lambda: 1_800_000_000.0)
        self.assertIsNone(memory.recall(ADDRESS))
        for text in ("not json", "[1, 2]", '{"%s": 5}' % ADDRESS,
                     '{"%s": {"level": 150, "saved": 1800000000}}' % ADDRESS,
                     '{"%s": {"level": "85", "saved": 1800000000}}' % ADDRESS,
                     '{"%s": {"level": true, "saved": 1800000000}}' % ADDRESS):
            with self.subTest(text=text):
                with open(path, "w") as handle:
                    handle.write(text)
                self.assertIsNone(memory.recall(ADDRESS))
        memory.remember(ADDRESS, 50)
        self.assertEqual(memory.recall(ADDRESS), 50)

    def test_a_file_that_cannot_be_written_costs_nothing_but_the_memory(self):
        directory = tempfile.mkdtemp(prefix="cornerstone-nothing-")
        self.addCleanup(shutil.rmtree, directory, ignore_errors=True)
        blocker = os.path.join(directory, "not-a-directory")
        open(blocker, "w").close()
        memory = nothing.CaseMemory(os.path.join(blocker, "nothing-case.json"))
        memory.remember(ADDRESS, 50)
        self.assertIsNone(memory.recall(ADDRESS))

    def test_a_repeated_level_is_not_rewritten_until_the_stamp_needs_refreshing(self):
        s = session(self).unlocked()
        s.device({"cmd": "07", "dir": "40", "payload": "01 04 55"})
        with mock.patch.object(nothing.os, "replace", wraps=os.replace) as replace:
            s.wall[0] += 5
            s.device({"cmd": "01", "dir": "e0", "payload": "01 04 55"})
            self.assertEqual(replace.call_count, 0)
            s.device({"cmd": "01", "dir": "e0", "payload": "01 04 54"})
            self.assertEqual(replace.call_count, 1)
            s.wall[0] += nothing.CaseMemory.REFRESH_S
            s.device({"cmd": "01", "dir": "e0", "payload": "01 04 54"})
            self.assertEqual(replace.call_count, 2)

    def test_the_default_file_is_under_xdg_state_home(self):
        with mock.patch.dict(os.environ, {"XDG_STATE_HOME": "/somewhere/state"}):
            self.assertEqual(nothing.CaseMemory().path,
                             "/somewhere/state/cornerstone-phones/nothing-case.json")
        with mock.patch.dict(os.environ, {"XDG_STATE_HOME": "", "HOME": "/home/someone"}):
            self.assertEqual(nothing.CaseMemory().path,
                             "/home/someone/.local/state/cornerstone-phones/nothing-case.json")


class Channels(unittest.TestCase):
    def test_known_earbuds_stay_on_15(self):
        for name in ("Nothing Ear (a)", "Ear (a)", " NOTHING EAR (A) ", "Nothing Ear",
                     "Ear (2)", "Nothing Ear (stick)", "Nothing Headphone (1)"):
            with self.subTest(name=name):
                self.assertEqual(nothing.channels_for(name), (15,))

    def test_the_cmf_models_have_their_own(self):
        self.assertEqual(nothing.channels_for("CMF Headphone Pro"), (28,))
        for name in ("CMF Buds 2", "Nothing CMF Buds 2", "buds 2", " CMF BUDS 2 "):
            with self.subTest(name=name):
                self.assertEqual(nothing.channels_for(name), (16,))

    def test_an_unknown_name_tries_15_then_28_and_no_name_keeps_15(self):
        self.assertEqual(nothing.channels_for("Future NT Link"), (15, 28))
        self.assertEqual(nothing.channels_for(""), (15,))
        self.assertEqual(nothing.channels_for("   "), (15,))

    def test_the_link_is_dialled_with_the_old_retry_budget(self):
        options = nothing.Nothing.parse([ADDRESS.lower(), "CMF Headphone Pro"])
        s = session(self)
        s.headset.channels = options["channels"]
        (spec,) = s.headset.links()
        self.assertIsInstance(spec, Rfcomm)
        self.assertEqual((spec.address, tuple(spec.channels)), (ADDRESS, (28,)))
        self.assertEqual((spec.attempts, spec.retry_ms, spec.timeout_s), (6, 1500, 5.0))
        self.assertIsNone(spec.probe)


class Arguments(unittest.TestCase):
    def test_the_name_is_optional(self):
        self.assertEqual(nothing.Nothing.parse([ADDRESS]),
                         {"address": ADDRESS, "channels": (15,)})
        self.assertEqual(nothing.Nothing.parse([ADDRESS, "CMF Buds 2"])["channels"], (16,))

    def test_a_bad_address_or_too_many_arguments_is_setup(self):
        for args in ([], ["nope"], ["nope", "Ear (a)"], [ADDRESS, "Ear (a)", "extra"]):
            with self.subTest(args=args):
                with self.assertRaises(SetupError):
                    nothing.Nothing.parse(args)
        self.assertEqual(SETUP, 4)


if __name__ == "__main__":
    unittest.main()
