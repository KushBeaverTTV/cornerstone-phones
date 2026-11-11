"""Motorola Moto Buds+: battery and Off / ANC / Ambient.

    cornerstone-bridge moto <classic-address> <reported-name>

Confirmed on Kush's Moto Buds+ (XT2441-1, reported name "moto buds+"),
docs/captures/moto-buds-plus.txt. The buds serve their control channel on a
vendor RFCOMM UUID; BlueZ connects it through a Profile1.

Every packet, both ways:

    "HEAD"  length u16 LE (= 8 + payload)
            opcode u16 BE  type u8  result u8
            payload length u16 LE  sequence u16 LE
            payload  crc32 u32 LE  "TAIL"

    type    0x80 request · 0x20 response · 0x40 notification
    0x0000  profile hello             0x000B  feature query (fc ff 05 00)
    0x0005  battery get               0x0009  battery changed
            payload L, R, case; 0xFF is a part that is not reporting
    0x0200  noise control get         0x0201  noise control set
    0x0204  noise control changed
            payload mode, sub-mode; mode 0 off, 1 ANC, 2 ambient

The buds answer with 0 in the CRC slot, so a received CRC is not checked;
ours is computed the standard way. The sequence number stays 0, which is
what the buds were tested with.
"""
import struct

from core import codec
from core.headset import SILENT, Headset, SetupError, address, choice
from core.links import Profile

UUID = "fc9d9fe0-4899-11ee-be56-0242ac120002"

HEAD = b"HEAD"
TAIL = b"TAIL"
HEADER = 14        # "HEAD" through the sequence number
TRAILER = 8        # crc32 and "TAIL"
REQUEST, RESPONSE, NOTIFY = 0x80, 0x20, 0x40

HELLO = 0x0000
BATTERY = 0x0005
BATTERY_CHANGED = 0x0009
FEATURES = 0x000B
MODE_GET = 0x0200
MODE_SET = 0x0201
MODE_CHANGED = 0x0204

NOT_REPORTING = 0xFF
PARTS = ("left", "right", "case")
MODE_NAMES = {0x00: "off", 0x01: "anc", 0x02: "ambient"}

# Per model, keyed by the reported name, case-insensitively. Only models
# somebody has held go here.
MODELS = {
    "moto buds+": {
        "available": ["off", "anc", "ambient"],
        # (mode, sub-mode) as this model accepted them
        "set": {"off": b"\x00\x00", "anc": b"\x01\x01", "ambient": b"\x02\x00"},
    },
}

ASK_AGAIN_MS = 2000     # a second ask if the first went unanswered
SILENCE_MS = 6000       # no mode by now: these buds do not speak this
POLL_MS = 15000         # the buds announce changes, but not every one


def packet(opcode, payload=b"", kind=REQUEST, sequence=0):
    body = (HEAD + struct.pack("<H", 8 + len(payload)) + struct.pack(">H", opcode)
            + bytes([kind, 0]) + struct.pack("<HH", len(payload), sequence) + payload)
    return body + struct.pack("<I", codec.crc32(body)) + TAIL


def unpack(buffer):
    """Take whole packets off the front of a bytearray.

    Returns [(opcode, kind, payload)]. Bytes before a "HEAD" are noise and go;
    a packet whose "TAIL" is not where its length says is not a packet, and
    the search starts again one byte further on rather than trusting it.
    """
    out = []
    while True:
        start = buffer.find(HEAD)
        if start < 0:
            # Keep a possible partial "HEA" at the end for the next read.
            del buffer[:max(0, len(buffer) - (len(HEAD) - 1))]
            return out
        del buffer[:start]
        if len(buffer) < HEADER:
            return out
        size = struct.unpack_from("<H", buffer, 10)[0]
        total = HEADER + size + TRAILER
        if len(buffer) < total:
            return out
        if bytes(buffer[total - len(TAIL):total]) != TAIL:
            del buffer[:1]
            continue
        opcode = struct.unpack_from(">H", buffer, 6)[0]
        out.append((opcode, buffer[8], bytes(buffer[HEADER:HEADER + size])))
        del buffer[:total]


def battery_levels(payload):
    """{"left": 92, "right": 90} from L, R, case; parts not reporting are left out."""
    levels = {}
    for part, value in zip(PARTS, payload):
        if value != NOT_REPORTING and value <= 100:
            levels[part] = value
    return levels


class Moto(Headset):
    brand = "moto"
    label = "Moto"
    usage = "<classic-address> <reported-name>"

    @classmethod
    def parse(cls, args):
        if len(args) != 2:
            raise SetupError("usage: cornerstone-bridge moto " + cls.usage)
        model = MODELS.get(args[1].strip().casefold())
        if model is None:
            raise SetupError("no Moto model row for %r" % args[1])
        return {"address": address(args[0]), "model": model}

    def __init__(self, host, address, model):
        super().__init__(host)
        self.address = address
        self.model = model
        self.buffer = bytearray()
        self.mode = None
        self.battery = {}

    def links(self):
        return [Profile(self.address, UUID)]

    def opened(self, link):
        # The Android app's opening, shortened to what the buds need.
        self.send(packet(HELLO))
        self.send(packet(FEATURES, bytes.fromhex("fcff0500")))
        self.ask()
        self.later(ASK_AGAIN_MS, self.ask_if_silent)
        self.later(SILENCE_MS, self.give_up_if_silent)
        self.every(POLL_MS, self.ask)

    def ask(self):
        self.send(packet(BATTERY))
        self.send(packet(MODE_GET))

    def ask_if_silent(self):
        if self.mode is None:
            self.ask()

    def give_up_if_silent(self):
        if self.mode is None:
            self.fail(SILENT, "the Moto buds did not answer the listening-mode query")

    def received(self, data, link):
        self.buffer += data
        for opcode, kind, payload in unpack(self.buffer):
            if self.finished:
                return
            self.packet(opcode, kind, payload)

    def packet(self, opcode, kind, payload):
        if opcode in (MODE_GET, MODE_CHANGED) or (opcode == MODE_SET and kind == NOTIFY):
            mode = MODE_NAMES.get(payload[0]) if payload else None
            if mode in self.model["available"]:
                self.mode = mode
                self.publish()
        elif opcode == MODE_SET and kind == RESPONSE:
            # An empty acknowledgement. What the buds now are is what they
            # say next, so ask rather than assume.
            self.send(packet(MODE_GET))
        elif opcode in (BATTERY, BATTERY_CHANGED):
            levels = battery_levels(payload)
            if levels:
                self.battery = levels
                self.publish()

    def publish(self):
        if self.mode is None:
            return
        line = {"modes": True, "mode": self.mode, "available": list(self.model["available"])}
        if self.battery:
            line["battery"] = dict(self.battery)
        self.report(line)

    def command(self, verb, arg):
        if verb != "set" or self.mode is None:
            return
        mode = choice(arg, self.model["set"])
        if mode is not None:
            self.send(packet(MODE_SET, self.model["set"][mode]))


HEADSET = Moto
