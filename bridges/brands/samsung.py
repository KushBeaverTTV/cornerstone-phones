"""Samsung Galaxy Buds2 (SPPNew): battery and Off / ANC / Ambient.

    cornerstone-bridge samsung <classic-address>

The Buds2 and later advertise SPPNew, 2e73a4ad-332d-41fc-90e2-16bef06523f2,
and frame everything on it the same way:

    FD  header u16 LE  id u8  payload  crc16 LE  DD

    header  low 10 bits: id + payload + crc length; 0x1000: a request
    crc     CRC-16/XMODEM over id and payload

    0x88    manager info; asked with 01 01 22, answered with the extended status
    0x61    extended status: battery at [2] left, [3] right, [7] case; the mode
            at [12] (0 off, 1 ANC, 2 ambient); charging bits at [36]
    0x78    set noise control, one byte, the mode
    0x77    noise control changed, first byte the mode

Only what a Buds2 was seen to send is read (tests/pins/samsung). The plain
status 0x60 has never been captured and is ignored rather than read at the
0x61 offsets. The charging bits have only ever been seen clear (0x40); their
layout is GalaxyBudsClient's, not confirmed here.
"""
import struct

from core import codec
from core.headset import SILENT, Headset, SetupError, address, choice
from core.links import Profile

UUID = "2e73a4ad-332d-41fc-90e2-16bef06523f2"

START, END = 0xFD, 0xDD
REQUEST_BIT = 0x1000
SIZE_MASK = 0x3FF

MANAGER_INFO = 0x88
STATUS = 0x60
EXTENDED_STATUS = 0x61
NOISE_SET = 0x78
NOISE_CHANGED = 0x77

AVAILABLE = ["off", "anc", "ambient"]
MODE_NAMES = {0: "off", 1: "anc", 2: "ambient"}
MODE_BYTES = {name: byte for byte, name in MODE_NAMES.items()}
CHARGING_BITS = (("left", 0x10), ("right", 0x04), ("case", 0x01))

ANSWER_MS = 5000


def frame(message, payload=b"", request=True):
    size = 1 + len(payload) + 2
    header = (size & SIZE_MASK) | (REQUEST_BIT if request else 0)
    check = codec.crc16_xmodem(bytes([message]) + payload)
    return (bytes([START]) + struct.pack("<H", header) + bytes([message]) + payload
            + struct.pack("<H", check) + bytes([END]))


def unframe(buffer):
    """Whole frames off the front of a bytearray, as [(id, payload)].

    A frame whose end byte or CRC is wrong is dropped; the search resumes
    after its start byte so a bad length cannot swallow the next frame.
    """
    out = []
    while True:
        start = buffer.find(START)
        if start < 0:
            buffer.clear()
            return out
        del buffer[:start]
        if len(buffer) < 7:
            return out
        size = struct.unpack_from("<H", buffer, 1)[0] & SIZE_MASK
        total = 3 + size + 1
        if len(buffer) < total:
            return out
        raw = bytes(buffer[:total])
        message, payload = raw[3], raw[4:-3]
        if raw[-1] != END or raw[-3:-1] != struct.pack("<H", codec.crc16_xmodem(raw[3:-3])):
            del buffer[:1]
            continue
        del buffer[:total]
        out.append((message, payload))


def level(payload, index):
    if index >= len(payload) or payload[index] > 100:
        return -1
    return payload[index]


def status_line(payload):
    """The panel's line from an extended status, or None for one too short."""
    if len(payload) < 13 or payload[12] not in MODE_NAMES:
        return None
    charging = []
    if len(payload) > 36:
        charging = [part for part, bit in CHARGING_BITS if payload[36] & bit]
    return {
        "modes": True,
        "mode": MODE_NAMES[payload[12]],
        "available": list(AVAILABLE),
        "battery": {"left": level(payload, 2), "right": level(payload, 3),
                    "case": level(payload, 7), "charging": charging},
    }


class Samsung(Headset):
    brand = "samsung"
    label = "Samsung"
    usage = "<classic-address>"

    @classmethod
    def parse(cls, args):
        if len(args) != 1:
            raise SetupError("usage: cornerstone-bridge samsung " + cls.usage)
        return {"address": address(args[0])}

    def __init__(self, host, address):
        super().__init__(host)
        self.address = address
        self.buffer = bytearray()
        self.state = None
        self.asked = False

    def links(self):
        return [Profile(self.address, UUID)]

    def opened(self, link):
        self.ask()
        self.later(ANSWER_MS, self.give_up_if_silent)

    def ask(self):
        if not self.asked:
            self.asked = True
            self.send(frame(MANAGER_INFO, bytes([1, 1, 34])))

    def give_up_if_silent(self):
        if self.state is None:
            self.fail(SILENT, "the Samsung headset did not answer")

    def received(self, data, link):
        self.buffer += data
        for message, payload in unframe(self.buffer):
            if self.finished:
                return
            if message == EXTENDED_STATUS:
                self.show(status_line(payload))
            elif message == NOISE_CHANGED and payload and payload[0] in MODE_NAMES:
                state = dict(self.state or {
                    "modes": True, "mode": "off", "available": list(AVAILABLE),
                    "battery": {"left": -1, "right": -1, "case": -1, "charging": []},
                })
                state["mode"] = MODE_NAMES[payload[0]]
                self.show(state)

    def show(self, state):
        if state is not None:
            self.state = state
            self.report(state)

    def command(self, verb, arg):
        mode = choice(arg, MODE_BYTES) if verb == "set" else None
        if mode is not None and self.state is not None:
            self.send(frame(NOISE_SET, bytes([MODE_BYTES[mode]])))


HEADSET = Samsung
