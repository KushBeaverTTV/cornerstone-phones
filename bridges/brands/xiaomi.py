"""Xiaomi Buds 5 Pro: Off / ANC / Ambient over Compact GAIA on SPP.

    cornerstone-bridge xiaomi <classic-address>

Confirmed on a Xiaomi Buds 5 Pro (Qualcomm QCC-7228), PROTOCOL.md "Xiaomi Buds
5 Pro". The shell picks this bridge from the CSR GAIA UUID in the SDP record
(00001100-d102-...), but that channel refuses ConnectProfile; the one that
answers is standard SPP, 00001101-0000-1000-8000-00805f9b34fb, so that is the
UUID the Profile1 registers.

Every frame, both ways:

    FF  version u8  flags u8 (0)  payload length u8
        vendor u16 BE  command u16 BE  payload

    The buds answer with version 3, and a frame's version is adopted for
    everything sent after it. Flags have only ever been 0 (no checksum, no
    long length); a frame with other flags is not trusted.

    vendor 0x000A  0x0300 handshake, answered 0x8300
    vendor 0x001D  0x1003 listening mode GET, answered 0x1103 with 5 bytes,
                   the first being the mode byte
                   0x1004 listening mode SET, one byte, acked by 0x1104 empty

    mode byte  0x00 off  0x01 anc  0x02 ambient
               0x03 accepted by the buds, not offered (reported as mode "")
               0x04 REBOOTS the buds: never sent, whatever the tables say

The buds announce nothing when a touch control changes the mode, so the mode
is asked for every 2.5 s once the first answer is in.
"""
import struct

from core.headset import SILENT, TRANSIENT, Headset, SetupError, address
from core.links import Profile

UUID = "00001101-0000-1000-8000-00805f9b34fb"

START = 0xFF
HEADER = 8
GAIA = 0x000A
DEVICE = 0x001D
HANDSHAKE, HANDSHAKE_ANSWER = 0x0300, 0x8300
MODE_GET, MODE_ANSWER = 0x1003, 0x1103
MODE_SET, MODE_ACK = 0x1004, 0x1104

FIRST_VERSION = 3
KNOWN_VERSIONS = range(1, 5)

AVAILABLE = ["off", "anc", "ambient"]
MODE_NAMES = {0x00: "off", 0x01: "anc", 0x02: "ambient"}
MODE_BYTES = {name: byte for byte, name in MODE_NAMES.items()}
NEVER_SEND = frozenset({0x04})

HANDSHAKE_TRIES = 3
HANDSHAKE_MS = 2000     # between handshakes, and after the last one
ANSWER_MS = 3000        # for the first listening-mode answer
POLL_MS = 2500


def frame(version, vendor, command, payload=b""):
    return (bytes([START, version & 0xFF, 0x00, len(payload)])
            + struct.pack(">HH", vendor, command) + bytes(payload))


def unframe(buffer):
    """Whole frames off the front of a bytearray, as [(version, vendor, command, payload)].

    Bytes before an FF go. An FF whose flags are not 0 is passed over one
    byte at a time rather than trusted; a frame not yet whole stays put.
    """
    out = []
    while True:
        start = buffer.find(START)
        if start < 0:
            buffer.clear()
            return out
        del buffer[:start]
        if len(buffer) < HEADER:
            return out
        if buffer[2] != 0:
            del buffer[:1]
            continue
        total = HEADER + buffer[3]
        if len(buffer) < total:
            return out
        vendor, command = struct.unpack_from(">HH", buffer, 4)
        out.append((buffer[1], vendor, command, bytes(buffer[HEADER:total])))
        del buffer[:total]


class Xiaomi(Headset):
    brand = "xiaomi"
    label = "Xiaomi"
    usage = "<classic-address>"

    @classmethod
    def parse(cls, args):
        if len(args) != 1:
            raise SetupError("usage: cornerstone-bridge xiaomi " + cls.usage)
        return {"address": address(args[0].strip())}

    def __init__(self, host, address):
        super().__init__(host)
        self.address = address
        self.buffer = bytearray()
        self.version = FIRST_VERSION
        self.greeted = False
        self.mode = None        # None until the first answer; "" for an unnamed byte

    def links(self):
        return [Profile(self.address, UUID)]

    def opened(self, link):
        self.handshake(1)

    def handshake(self, attempt):
        # Each try sets up the next; the timer that outlives an answer finds
        # `greeted` set and does nothing.
        if self.greeted:
            return
        if attempt > HANDSHAKE_TRIES:
            self.fail(TRANSIENT, "the Xiaomi buds did not answer the handshake")
            return
        self.send(frame(self.version, GAIA, HANDSHAKE))
        self.later(HANDSHAKE_MS, self.handshake, attempt + 1)

    def ask(self):
        self.send(frame(self.version, DEVICE, MODE_GET))

    def give_up_if_silent(self):
        if self.mode is None:
            self.fail(SILENT, "the buds did not answer the listening-mode query")

    def received(self, data, link):
        self.buffer += data
        for version, vendor, command, payload in unframe(self.buffer):
            if self.finished:
                return
            self.frame(version, vendor, command, payload)

    def frame(self, version, vendor, command, payload):
        if version in KNOWN_VERSIONS:
            self.version = version
        if vendor == GAIA and command == HANDSHAKE_ANSWER:
            if not self.greeted:
                self.greeted = True
                self.ask()
                self.later(ANSWER_MS, self.give_up_if_silent)
        elif vendor == DEVICE and command == MODE_ANSWER:
            self.mode_answer(payload)
        elif vendor == DEVICE and command == MODE_ACK:
            # The ack names nothing; the next GET says what the buds now are.
            self.ask()

    def mode_answer(self, payload):
        if not payload:
            return
        first = self.mode is None
        self.mode = MODE_NAMES.get(payload[0], "")
        self.report({"modes": True, "mode": self.mode, "available": list(AVAILABLE)})
        if first:
            self.every(POLL_MS, self.ask)

    def command(self, verb, arg):
        if verb != "set" or self.mode is None:
            return
        byte = MODE_BYTES.get(arg)
        if byte is None or byte in NEVER_SEND:
            return
        self.send(frame(self.version, DEVICE, MODE_SET, bytes([byte])))


HEADSET = Xiaomi
