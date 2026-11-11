"""TOZO NC9 Pro: the frozen session, the set-and-read-back cycle, and the case.

    python3 -m unittest tests.test_tozo

Values marked synthetic are fault injection; every other one is in the
owner's captures (Evidence checks that).
"""
import os
import unittest

from tests import harness
from brands import tozo
from core.headset import CLEAN, SILENT, TRANSIENT, SetupError

ADDRESS = "94:4B:F8:C1:5F:98"
CASE = "41:42:B1:F6:F8:4F"
MODEL = tozo.MODELS["TOZO NC9 Pro"]
RX = {
    "off": "00 30 01 00 00", "anc": "00 30 01 01 01",
    "ambient": "00 30 01 02 02", "wind": "00 30 01 03 03",
    "leisure": "00 30 01 04 04", "adaptive": "00 30 01 06 06",
    "buds": "00 02 02 64 64 c8", "case": "01 02 01 64 64",
    "case_address": "00 20 06 41 42 b1 f6 f8 4f 71",
    "case_peer": "01 09 06 94 4b f8 c1 5f 98 8f",
    "event": "20 0d 02 01 03 04",
}
MODE_GET = "buds 00 30 00 00"


class Session(harness.Session):
    """A TOZO session with two links, "buds" and "case". "device" in a pin is
    one whole GATT value as hex, from the buds; or {"role": "case", "wire":
    hex} for one from the case. "sent" is every value written, as
    "<link> <hex>". "connected" is a link coming up (its notifications
    subscribed), "disconnected" one going away, and "advance" moves the clock
    by that many seconds, running timers as they fall due."""

    def __init__(self, address=ADDRESS):
        super().__init__()
        self.start(tozo.Tozo, address=address, model=MODEL)

    def device(self, spec):
        if isinstance(spec, str):
            self.receive(harness.hexbytes(spec), tozo.BUDS)
        else:
            self.receive(harness.hexbytes(spec["wire"]), spec["role"])

    def do_connected(self, link):
        self.headset.opened(link)

    def do_disconnected(self, link):
        self.headset.closed(link, None)

    def do_advance(self, seconds):
        self.host.advance(seconds)

    def do_ready(self):
        self.do_connected(tozo.BUDS)
        self.do_advance(0.5)
        self.device(RX["anc"])

    @property
    def sent(self):
        return ["%s %s" % (link, harness.hexstr(data)) for link, data in self.host.writes]

    @property
    def cases(self):
        """Every case link opened, by address."""
        return [spec.address for spec in self.host.opened if spec.name == tozo.CASE]


harness.pin_tests(globals(), "tozo", Session)


def ready(address=ADDRESS):
    s = Session(address)
    s.do_ready()
    return s


def case_frame(name):
    return {"role": "case", "wire": RX[name]}


class Evidence(unittest.TestCase):
    def test_every_value_used_here_is_in_the_owner_captures(self):
        text = ""
        for name in ("tozo-nc9-pro-live.txt", "tozo-nc9-pro-phone.txt"):
            with open(os.path.join(harness.ROOT, "docs", "captures", name)) as handle:
                text += handle.read()
        for frame in RX.values():
            self.assertIn(frame, text)
        for frame in MODEL["sets"].values():
            self.assertIn(harness.hexstr(frame), text)


class Datagrams(unittest.TestCase):
    def test_partial_damaged_or_combined_values_are_ignored_then_recovered_from(self):
        raw = harness.hexbytes(RX["ambient"])
        for split in range(1, len(raw)):
            s = ready()
            s.receive(raw[:split], tozo.BUDS)
            s.receive(raw[split:], tozo.BUDS)
            self.assertEqual(s.headset.mode, "anc")
            s.receive(raw, tozo.BUDS)
            self.assertEqual(s.headset.mode, "ambient")
        for bad in (raw + raw, raw[:-1] + b"\xff", b"noise" + raw,
                    harness.hexbytes("00 30 01 05 05"),      # synthetic: no such mode
                    harness.hexbytes("00 30 00 00")):        # our own query
            s = ready()
            s.receive(bad, tozo.BUDS)
            self.assertEqual(s.headset.mode, "anc")
            s.receive(raw, tozo.BUDS)
            self.assertEqual(s.headset.mode, "ambient")

    def test_the_checksum_is_the_payloads_low_byte(self):
        self.assertEqual(tozo.datagram(harness.hexbytes(RX["buds"])), (0, 2, b"\x64\x64"))
        self.assertIsNone(tozo.datagram(harness.hexbytes("00 02 02 64 64 c9")))


class Control(unittest.TestCase):
    def test_the_opening_asks_mode_battery_and_case_spaced_apart(self):
        s = Session()
        s.do_connected(tozo.BUDS)
        self.assertEqual(s.sent, [MODE_GET])
        s.do_advance(0.15)
        self.assertEqual(s.sent[-1], "buds 00 02 00 00")
        s.do_advance(0.15)
        self.assertEqual(s.sent[-1], "buds 00 20 00 00")

    def test_sets_run_one_at_a_time_and_read_back_after_the_ack(self):
        s = ready()
        before = list(s.lines)
        s.command("set ambient")
        s.command("set wind")
        s.do_advance(0.2)
        self.assertEqual(s.sent[-1], "buds 10 05 01 01 01")
        s.device("10 05 01 00 00")
        s.do_advance(1)
        self.assertEqual(s.lines, before)
        self.assertEqual(s.sent[-1], "buds 10 05 01 01 01")
        # An answer to a query from before the set does not release the next one.
        s.device(RX["anc"])
        self.assertEqual(s.headset.pending, "ambient")
        s.do_advance(0.6)
        self.assertEqual(s.sent[-1], MODE_GET)
        s.device(RX["ambient"])
        s.do_advance(0.2)
        self.assertEqual(s.lines[-1]["mode"], "ambient")
        self.assertEqual(s.sent[-1], "buds 10 07 01 01 01")

    def test_an_echo_is_not_an_ack_and_an_unanswered_read_back_is_transient(self):
        s = ready()
        s.command("set ambient")
        s.do_advance(0.2)
        before = list(s.lines)
        s.device("10 05 01 01 01")          # synthetic: an echo, not success
        self.assertEqual(s.lines, before)
        s.do_advance(2)
        self.assertEqual(s.sent[-1], MODE_GET)
        s.do_advance(8)
        self.assertEqual(s.exit_code, TRANSIENT)
        self.assertEqual(s.lines[-1]["error"], "TOZO mode readback timed out")

    def test_an_announced_change_is_read_back_and_repeats_say_nothing(self):
        s = ready()
        s.device(RX["event"])
        s.do_advance(1)
        self.assertEqual(s.headset.mode, "anc")
        s.do_advance(0.6)
        self.assertEqual(s.sent[-1], MODE_GET)
        s.device(RX["wind"])
        count = len(s.lines)
        s.device(RX["wind"])
        self.assertEqual(len(s.lines), count)
        self.assertEqual(s.headset.mode, "wind")

    def test_unknown_commands_and_commands_before_the_mode_send_nothing(self):
        for s in (Session(), ready()):
            before = list(s.sent)
            for line in ("set talkthru", "set bad", "level high", "voice on", "latency on", ""):
                s.command(line)
            s.do_advance(0.1)
            self.assertEqual(s.sent, before)
        s = Session()
        s.command("set anc")
        s.do_advance(1)
        self.assertEqual(s.sent, [])

    def test_the_polls_ask_only_the_observed_queries(self):
        s = ready()
        for _ in range(20):
            s.do_advance(2)
            s.device(RX["anc"])
        self.assertEqual(set(s.sent), {MODE_GET, "buds 00 02 00 00", "buds 00 20 00 00"})
        self.assertGreater(s.sent.count("buds 00 02 00 00"), 1)
        self.assertIsNone(s.exit_code)


class Lifecycle(unittest.TestCase):
    def test_silence_parks_the_address(self):
        s = Session()
        s.do_connected(tozo.BUDS)
        s.do_advance(9)
        self.assertEqual(s.exit_code, SILENT)
        self.assertEqual(s.lines, [])

    def test_losing_the_buds_is_transient(self):
        s = ready()
        s.do_disconnected(tozo.BUDS)
        self.assertEqual((s.exit_code, s.lines[-1]["error"]),
                         (TRANSIENT, "TOZO control connection closed"))

    def test_buds_that_never_come_up_end_the_run_with_the_links_reason(self):
        s = Session()
        s.headset.unavailable(tozo.BUDS, "TOZO device is unavailable in BlueZ", TRANSIENT)
        self.assertEqual((s.exit_code, s.lines[-1]["error"]),
                         (TRANSIENT, "TOZO device is unavailable in BlueZ"))

    def test_nothing_is_written_after_the_run_ends(self):
        s = ready()
        s.command("set ambient")
        s.headset.stop()
        before = list(s.sent)
        s.do_advance(60)
        s.do_connected(tozo.BUDS)
        s.command("set anc")
        self.assertEqual(s.sent, before)
        self.assertEqual(s.exit_code, CLEAN)


class Battery(unittest.TestCase):
    def test_values_out_of_range_or_of_the_wrong_length_are_not_read(self):
        s = ready()
        s.device(RX["buds"])
        original = dict(s.headset.battery)
        for raw in ("00 02 02 ff 64 63", "00 02 02 e4 64 48", "00 02 01 64 64"):   # synthetic
            s.device(raw)
            self.assertEqual(s.headset.battery, original)
        self.assertNotIn("charging", s.headset.battery)
        s.device("00 02 02 00 64 64")                                              # synthetic
        self.assertEqual(s.headset.battery, {"left": 0, "right": 100})

    def test_a_battery_before_the_mode_is_reported_without_modes(self):
        s = Session()
        s.do_connected(tozo.BUDS)
        s.device(RX["buds"])
        self.assertEqual(s.lines, [{"modes": False, "battery": {"left": 100, "right": 100}}])


class Case(unittest.TestCase):
    def test_the_case_is_believed_only_once_it_names_these_earbuds(self):
        s = ready()
        s.device(RX["case_address"])
        self.assertEqual(s.cases, [CASE])
        spec = s.host.opened[-1]
        self.assertEqual((spec.service, spec.scan), (tozo.CASE_SERVICE, True))
        s.do_connected(tozo.CASE)
        s.do_advance(0.2)
        self.assertEqual(s.sent[-1], "case 01 09 00 00")
        s.device(case_frame("case"))
        self.assertNotIn("case", s.headset.battery)
        s.device(case_frame("case_peer"))
        s.do_advance(0.2)
        self.assertEqual(s.sent[-1], "case 01 02 00 00")
        s.device(case_frame("case"))
        self.assertEqual(s.headset.battery, {"case": 100, "caseStale": False})

    def test_a_lost_case_goes_stale_and_is_tried_again_a_minute_later(self):
        s = ready()
        s.device(RX["case_address"])
        s.do_connected(tozo.CASE)
        s.device(case_frame("case_peer"))
        s.device(case_frame("case"))
        s.do_disconnected(tozo.CASE)
        self.assertTrue(s.headset.battery["caseStale"])
        self.assertEqual(s.lines[-1]["mode"], "anc")
        for _ in range(31):
            s.do_advance(2)
            s.device(RX["anc"])
        self.assertEqual(s.cases, [CASE, CASE])
        s.do_connected(tozo.CASE)
        self.assertFalse(s.headset.case_verified)
        s.device(case_frame("case_peer"))
        s.device(case_frame("case"))
        self.assertFalse(s.headset.battery["caseStale"])

    def test_a_silent_case_is_closed_alone_and_retried(self):
        s = ready()
        s.device(RX["case_address"])
        s.do_connected(tozo.CASE)
        s.do_advance(9)
        self.assertEqual(s.host.closed, [tozo.CASE])
        self.assertIsNone(s.exit_code)
        self.assertFalse(s.headset.case_verified)
        self.assertIsNotNone(s.headset.case_retry)

    def test_the_case_battery_is_polled_with_the_buds_once_verified(self):
        s = ready()
        s.device(RX["case_address"])
        s.do_connected(tozo.CASE)
        s.device(case_frame("case_peer"))
        s.device(case_frame("case"))
        for _ in range(15):
            s.do_advance(2)
            s.device(RX["anc"])
            if s.sent[-1] == "case 01 02 00 00":
                s.device(case_frame("case"))
        self.assertEqual(s.sent.count("case 01 02 00 00"), 2)
        self.assertEqual(s.host.closed, [])

    def test_no_case_is_dialled_for_an_empty_or_our_own_address(self):
        s = ready()
        for raw in ("00 20 06 00 00 00 00 00 00 00",              # synthetic
                    "00 20 06 ff ff ff ff ff ff fa",              # synthetic
                    "00 20 06 94 4b f8 c1 5f 98 8f"):             # synthetic: the buds
            s.device(raw)
        self.assertEqual(s.cases, [])

    def test_two_pairs_do_not_share_case_battery_or_mode(self):
        a = ready()
        b = ready("AA:BB:CC:DD:EE:FF")
        for s in (a, b):
            s.device(case_frame("case_peer"))
            s.device(case_frame("case"))
        self.assertEqual(a.headset.battery["case"], 100)
        self.assertNotIn("case", b.headset.battery)
        a.device(RX["ambient"])
        self.assertEqual(b.headset.mode, "anc")
        a.headset.stop()
        self.assertIsNone(b.exit_code)


class Arguments(unittest.TestCase):
    def test_only_its_own_model_row_is_accepted(self):
        self.assertEqual(tozo.Tozo.parse([ADDRESS.lower(), "TOZO NC9 Pro"]),
                         {"address": ADDRESS, "model": MODEL})
        for args in (["bad", "TOZO NC9 Pro"], [ADDRESS, "TOZO other"],
                     [ADDRESS, "tozo nc9 pro"], [ADDRESS], []):
            with self.subTest(args=args):
                with self.assertRaises(SetupError):
                    tozo.Tozo.parse(args)

    def test_the_buds_link_is_the_b610_service_on_the_classic_address(self):
        (spec,) = Session().headset.links()
        self.assertEqual((spec.address, spec.service, spec.notify, spec.write, spec.name),
                         (ADDRESS, tozo.SERVICE, tozo.NOTIFY, tozo.WRITE, tozo.BUDS))
        self.assertFalse(spec.scan)


if __name__ == "__main__":
    unittest.main()
