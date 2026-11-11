"""JBL over btgatt-client: the frozen sessions, the opening, and mode-support.json.

    python3 -m unittest tests.test_jbl
"""
import json
import os
import shutil
import tempfile
import unittest
from unittest import mock

from tests import harness
from brands import jbl
from core.headset import CLEAN, SILENT, TRANSIENT, SetupError

ADDRESS = "48:B4:41:00:00:01"
DISCOVERED = "GATT discovery procedures complete"
REGISTERED = "Registered notify handler"
CONNECTED = "Connecting to device... Done"


def notification(mode, handle="0x000c"):
    slots = {"off": "00 02 00 03 00", "anc": "01 02 00 03 00",
             "ambient": "00 02 01 03 00", "talkthru": "00 02 00 03 01"}[mode]
    return "Handle Value Not/Ind: %s - (10 data bytes): aa 91 07 12 01 %s" % (handle, slots)


class Support:
    """mode-support.json as calls, so a test sees exactly what was recorded."""

    def __init__(self, log):
        self.log = log

    def answered(self, model_id):
        self.log.append(("answered", model_id))

    def missed(self, model_id):
        self.log.append(("miss", model_id))


class Session(harness.Session):
    """A JBL session. The child, btgatt-client, is running from the start (the
    runtime calls opened() as it spawns it). "device" in a pin is one output
    line of btgatt-client; "sent" is every line written to its stdin.
    "start" marks the opening conversation as under way (it already is: it
    runs on timers); "wait_sent" lets time pass until that many lines are
    out; "wait_start" lets it pass until the opening is over, after which
    state "start_code" is null to carry on, or the exit code. `support` is
    every write to mode-support.json, as ("answered" | "miss", model id)."""

    TIME_LIMIT_S = 120

    def __init__(self, model_id="0x1234"):
        super().__init__()
        self.support = []
        self.started = False
        self.start(jbl.Jbl, address=ADDRESS, model_id=model_id,
                   support=Support(self.support))
        self.headset.opened(harness.MAIN)

    def device(self, line):
        self.receive(line)

    def do_start(self):
        self.started = True

    def run_until(self, done):
        """Run timers in due order until done() or nothing is left to run."""
        limit = self.host.clock + self.TIME_LIMIT_S
        while not done() and self.host.timers and self.host.clock < limit:
            due = min(t[5] for t in self.host.timers)
            self.host.advance(max(0.0, due - self.host.clock))
        return done()

    def wait_sent(self, count):
        self.run_until(lambda: len(self.sent) >= count or self.headset.finished)
        super().wait_sent(count)

    def do_wait_start(self):
        if not self.run_until(lambda: self.headset.settled or self.headset.finished):
            raise AssertionError("the opening conversation did not end")

    @property
    def sent(self):
        return list(self.frames)

    def state(self, name):
        if name == "start_code":
            return self.headset.exit_code if self.headset.finished else None
        return super().state(name)


harness.pin_tests(globals(), "jbl", Session)


def through_registration(model_id="0x1234"):
    s = Session(model_id)
    s.device(CONNECTED)
    s.device(DISCOVERED)
    s.wait_sent(1)
    s.device(REGISTERED)
    s.wait_sent(3)
    return s


class Opening(unittest.TestCase):
    def test_the_conversation_waits_on_each_step(self):
        s = Session()
        self.assertEqual(s.timers, [jbl.DISCOVERY_MS])
        s.device(CONNECTED)
        self.assertEqual(s.sent, [])
        s.device(DISCOVERED)
        self.assertEqual(s.sent, ["register-notify 0x000c"])
        self.assertEqual(s.timers, [jbl.REGISTER_MS])
        s.device(REGISTERED)
        self.assertEqual(s.timers, [jbl.FRAME_GAP_MS])
        s.advance(2.0)
        self.assertEqual(s.sent[-1], "write-value 0x0010 0xaa 0x9b 0x02 0x01 0x01")
        s.advance(2.0)
        self.assertEqual(s.sent[-1], "write-value 0x0010 0xaa 0x91 0x01 0x11")
        self.assertEqual(s.timers, [jbl.ANSWER_MS])

    def test_an_unconfirmed_registration_still_asks(self):
        # A slow device is still worth asking; a refusal comes as a line.
        s = Session()
        s.device(DISCOVERED)
        s.advance(5.0 + 4.0)
        self.assertEqual(s.sent[1:], ["write-value 0x0010 0xaa 0x9b 0x02 0x01 0x01",
                                      "write-value 0x0010 0xaa 0x91 0x01 0x11"])
        self.assertIsNone(s.exit_code)

    def test_the_wave_buds_2_use_their_own_handles(self):
        s = through_registration("ea59a0")
        self.assertEqual(s.sent, ["register-notify 0xa205",
                                  "write-value 0xa202 0xaa 0x9b 0x02 0x01 0x01",
                                  "write-value 0xa202 0xaa 0x91 0x01 0x11"])

    def test_an_unknown_model_keeps_the_tune230nc_handles(self):
        self.assertEqual(Session("ffffff").headset.handles, jbl.DEFAULT_HANDLES)
        self.assertEqual(Session("").headset.handles, jbl.DEFAULT_HANDLES)

    def test_discovery_that_never_ends_is_transient_and_says_how_far_it_got(self):
        s = Session()
        s.fire()
        self.assertEqual((s.exit_code, s.lines[-1]["error"]),
                         (TRANSIENT, "could not open the BLE link"))
        s = Session()
        s.device(CONNECTED)
        s.fire()
        self.assertEqual(s.lines[-1]["error"], "the BLE link never finished discovery")
        self.assertEqual(s.support, [])

    def test_a_report_before_the_query_still_ends_the_opening_without_a_deadline(self):
        s = Session()
        s.device(DISCOVERED)
        s.device(REGISTERED)
        s.device(notification("anc"))
        s.call("wait_start")
        self.assertIsNone(s.state("start_code"))
        self.assertEqual(s.timers, [])
        self.assertEqual(s.support, [("answered", "0x1234")])


class Silent(unittest.TestCase):
    def test_no_report_records_a_miss_and_parks_the_model(self):
        s = through_registration()
        s.call("wait_start")
        self.assertEqual(s.state("start_code"), SILENT)
        self.assertEqual(s.support, [("miss", "0x1234")])
        self.assertEqual(s.lines, [{"modes": False,
                                    "error": "the device did not answer the listening-mode query"}])

    def test_without_a_model_id_nothing_is_recorded(self):
        s = through_registration("")
        s.call("wait_start")
        self.assertEqual(s.exit_code, SILENT)
        self.assertEqual(s.support, [])

    def test_a_report_that_beats_the_deadline_is_not_a_miss(self):
        s = through_registration()
        s.device(notification("anc"))
        s.headset.answer_overdue()
        self.assertIsNone(s.exit_code)
        self.assertEqual(s.support, [("answered", "0x1234")])


class Failures(unittest.TestCase):
    def test_a_refused_link_is_transient_and_records_nothing(self):
        s = Session()
        s.device("Failed to connect: Connection refused (111)")
        self.assertEqual(s.exit_code, TRANSIENT)
        self.assertEqual(s.lines[-1]["error"],
                         "cannot reach the earbuds over BLE: Connection refused (111)")
        self.assertEqual(s.support, [])

    def test_link_and_subscription_failures_never_mark_the_model(self):
        for line in ("GATT discovery procedures failed", "Failed to register notify handler",
                     "GATT client not initialized", "Device disconnected"):
            with self.subTest(line=line):
                s = Session()
                s.device(DISCOVERED)
                s.device(line)
                self.assertEqual(s.exit_code, TRANSIENT)
                s.headset.answer_overdue()
                s.fire()
                self.assertEqual(s.exit_code, TRANSIENT)
                self.assertEqual(s.support, [])
                self.assertEqual(len(s.lines), 1)

    def test_the_child_going_away_is_transient(self):
        s = through_registration()
        s.device(notification("off"))
        s.headset.closed(harness.MAIN, None)
        self.assertEqual((s.exit_code, s.lines[-1]),
                         (TRANSIENT, {"modes": False, "error": "the BLE link closed"}))

    def test_the_first_ending_wins_and_nothing_runs_after_it(self):
        s = Session()
        s.device(DISCOVERED)
        s.headset.stop()
        s.headset.fail(TRANSIENT, "late EOF")
        self.assertEqual(s.exit_code, CLEAN)
        self.assertEqual(s.lines, [])
        sent = list(s.sent)
        s.advance(60)
        self.assertEqual(s.sent, sent)
        self.assertEqual(s.state("start_code"), CLEAN)


class Reports(unittest.TestCase):
    def test_all_four_reports_and_spontaneous_changes(self):
        s = Session()
        for mode in ("off", "anc", "ambient", "talkthru"):
            s.device(notification(mode))
            self.assertEqual(s.lines[-1], {"modes": True, "mode": mode})
        self.assertEqual(s.support, [("answered", "0x1234")])
        self.assertEqual(s.sent, [])

    def test_lines_that_are_not_a_whole_report_say_nothing(self):
        s = Session()
        s.device(notification("ambient"))
        for line in ("", "random log", "aa 91 07 12", "aa 91 07 12 01 00 02 00",
                     "aa 92 07 12 01 00 02 00 03 00"):
            s.device(line)
        self.assertEqual(s.lines, [{"modes": True, "mode": "ambient"}])

    def test_a_slot_value_other_than_01_reads_as_off(self):
        self.assertEqual(jbl.mode_from_slots("02", "00", "00"), "off")
        self.assertEqual(jbl.mode_from_slots("00", "00", "01"), "talkthru")


class Commands(unittest.TestCase):
    def test_a_set_is_not_a_report(self):
        s = Session()
        s.device(notification("off"))
        s.command("set anc")
        self.assertEqual(s.sent, ["write-value 0x0010 0xaa 0x91 0x07 0x10 0x01 0x01 0x02 0x00 0x03 0x00"])
        self.assertEqual(s.lines, [{"modes": True, "mode": "off"}])

    def test_unrecognised_commands_send_nothing(self):
        s = Session()
        s.device(notification("off"))
        for line in ("", "set", "set unknown", "set anc now", "level 5", "voice on", "latency on"):
            s.command(line)
        self.assertEqual(s.sent, [])

    def test_nothing_is_set_before_the_first_report(self):
        s = Session()
        s.command("set anc")
        self.assertEqual(s.sent, [])


class Arguments(unittest.TestCase):
    def test_the_model_id_is_optional(self):
        self.assertEqual(jbl.Jbl.parse([ADDRESS.lower()]), {"address": ADDRESS, "model_id": ""})
        self.assertEqual(jbl.Jbl.parse([ADDRESS, "ea59a0"])["model_id"], "ea59a0")

    def test_a_bad_address_never_reaches_the_command_line(self):
        for args in ([], ["--d", "x"], ["48:B4:41:00:00:01; rm"], [ADDRESS, "a", "b"]):
            with self.subTest(args=args):
                with self.assertRaises(SetupError):
                    jbl.Jbl.parse(args)

    def test_the_link_is_btgatt_client_on_a_random_address(self):
        (spec,) = Session().headset.links()
        self.assertEqual(list(spec.argv), ["btgatt-client", "-d", ADDRESS, "-t", "random"])


class SupportFile(unittest.TestCase):
    def setUp(self):
        self.state = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.state)
        patcher = mock.patch.dict(os.environ, {"XDG_STATE_HOME": self.state})
        patcher.start()
        self.addCleanup(patcher.stop)
        self.file = jbl.SupportFile()

    def contents(self):
        with open(os.path.join(self.state, "cornerstone-phones", "mode-support.json")) as handle:
            return handle.read()

    def test_the_path_is_the_one_the_shell_reads(self):
        self.assertEqual(self.file.path,
                         os.path.join(self.state, "cornerstone-phones", "mode-support.json"))

    def test_an_answer_is_written_compactly(self):
        self.file.answered("ea59a0")
        self.assertEqual(self.contents(), '{"ea59a0":{"supported":true}}')

    def test_three_misses_are_a_verdict(self):
        self.file.missed("0x1234")
        self.assertEqual(json.loads(self.contents()), {"0x1234": {"misses": 1}})
        self.file.missed("0x1234")
        self.file.missed("0x1234")
        self.assertEqual(json.loads(self.contents()),
                         {"0x1234": {"misses": 3, "supported": False}})

    def test_a_model_that_answered_once_is_never_downgraded(self):
        self.file.answered("ea59a0")
        self.file.missed("ea59a0")
        self.assertEqual(json.loads(self.contents()), {"ea59a0": {"supported": True}})

    def test_old_and_damaged_entries_are_read_as_dicts(self):
        os.makedirs(os.path.dirname(self.file.path))
        with open(self.file.path, "w") as handle:
            json.dump({"a": True, "b": 7, "c": {"misses": "x"}}, handle)
        self.assertEqual(self.file.read(), {"a": {"supported": True}, "b": {}, "c": {"misses": "x"}})
        self.file.missed("c")
        self.assertEqual(self.file.read()["c"], {"misses": 1})
        with open(self.file.path, "w") as handle:
            handle.write("[1, 2")
        self.assertEqual(self.file.read(), {})

    def test_an_unwritable_place_costs_nothing_but_the_record(self):
        blocked = os.path.join(self.state, "file")
        open(blocked, "w").close()
        jbl.SupportFile(os.path.join(blocked, "mode-support.json")).answered("x")


if __name__ == "__main__":
    unittest.main()
