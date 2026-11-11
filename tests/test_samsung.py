"""Samsung Galaxy Buds2: the frozen session, what is read, and silence.

    python3 -m unittest tests.test_samsung
"""
import unittest

from tests import harness
from brands import samsung
from core.headset import SILENT

ADDRESS = "84:5F:04:B5:D6:74"

STATUS_FRAME = (
    "fd 2a 00 61 0b 03 12 12 01 01 11 00 00 00 bf 22 01 00 40 01 40 01 03 00 03 66"
    " 00 02 00 10 00 00 00 00 11 02 00 01 00 00 40 00 00 99 3d dd"
)
STATUS_PAYLOAD = (
    "0b 03 12 12 01 01 11 00 00 00 bf 22 01 00 40 01 40 01 03 00 03 66 00 02 00 10"
    " 00 00 00 00 11 02 00 01 00 00 40 00 00"
)


class Session(harness.Session):
    """A Galaxy Buds2 session. "device" in a pin is a whole frame as hex, FD
    to DD. "sent" is every frame written, as hex. "open" is the channel
    coming up, which sends the manager-info request."""

    def __init__(self):
        super().__init__()
        self.start(samsung.Samsung, address=ADDRESS)

    def device(self, spec):
        self.receive(harness.hexbytes(spec))


harness.pin_tests(globals(), "samsung", Session)


class Framing(unittest.TestCase):
    def test_the_captured_status_frame_decodes(self):
        buffer = bytearray(harness.hexbytes(STATUS_FRAME))
        self.assertEqual(samsung.unframe(buffer),
                         [(samsung.EXTENDED_STATUS, harness.hexbytes(STATUS_PAYLOAD))])

    def test_our_frames_carry_the_request_bit_and_a_valid_crc(self):
        wire = samsung.frame(samsung.NOISE_SET, b"\x01")
        self.assertEqual(harness.hexstr(wire), "fd 04 10 78 01 d1 91 dd")
        self.assertEqual(samsung.unframe(bytearray(wire)), [(samsung.NOISE_SET, b"\x01")])

    def test_a_bad_crc_is_dropped_and_the_next_frame_still_reads(self):
        bad = bytearray(harness.hexbytes(STATUS_FRAME))
        bad[-2] ^= 0xFF
        good = samsung.frame(samsung.NOISE_CHANGED, b"\x02", request=False)
        self.assertEqual(samsung.unframe(bytearray(bytes(bad) + good)),
                         [(samsung.NOISE_CHANGED, b"\x02")])

    def test_a_frame_split_anywhere_reads_once(self):
        wire = harness.hexbytes(STATUS_FRAME)
        for cut in range(1, len(wire)):
            with self.subTest(cut=cut):
                buffer = bytearray(wire[:cut])
                self.assertEqual(samsung.unframe(buffer), [])
                buffer += wire[cut:]
                self.assertEqual(len(samsung.unframe(buffer)), 1)


class WhatIsRead(unittest.TestCase):
    def test_the_status_uses_the_observed_offsets(self):
        self.assertEqual(samsung.status_line(harness.hexbytes(STATUS_PAYLOAD)), {
            "modes": True,
            "mode": "anc",
            "available": ["off", "anc", "ambient"],
            "battery": {"left": 18, "right": 18, "case": 0, "charging": []},
        })

    def test_a_plain_status_frame_is_not_read(self):
        # 0x60 has not been captured; the same bytes under that id say nothing.
        s = Session()
        s.do_open()
        s.receive(samsung.frame(samsung.STATUS, harness.hexbytes(STATUS_PAYLOAD), request=False))
        self.assertEqual(s.lines, [])

    def test_a_noise_update_changes_only_the_mode(self):
        s = Session()
        s.do_open()
        s.device(STATUS_FRAME)
        s.receive(samsung.frame(samsung.NOISE_CHANGED, b"\x02", request=False))
        self.assertEqual(s.lines[-1]["mode"], "ambient")
        self.assertEqual(s.lines[-1]["battery"]["left"], 18)

    def test_charging_bits_are_read_where_the_status_is_long_enough(self):
        payload = bytearray(harness.hexbytes(STATUS_PAYLOAD))
        payload[36] = 0x40 | 0x10 | 0x01
        self.assertEqual(samsung.status_line(bytes(payload))["battery"]["charging"],
                         ["left", "case"])


class Silent(unittest.TestCase):
    def test_silence_after_the_request_parks_the_address(self):
        s = Session()
        s.do_open()
        s.fire()
        self.assertEqual(s.exit_code, SILENT)

    def test_nothing_is_set_before_the_first_status(self):
        s = Session()
        s.do_open()
        s.command("set anc")
        self.assertEqual(len(s.sent), 1)


if __name__ == "__main__":
    unittest.main()
