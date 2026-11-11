"""OPPO HeyMelody (Enco Air3 Pro): Off / ANC / Ambient, and a battery fallback.

    cornerstone-bridge oppo <classic-address>

OPPO, OnePlus and Realme earbuds serve the HeyMelody SPP UUID,
0000079a-d102-11e1-9b23-00025b00a5a5, which BlueZ connects through a
Profile1. No handshake: the buds answer a query at once. Confirmed on an
Enco Air3 Pro (HeyMelody product id 065C10); PROTOCOL.md has the frames as
they were read off the wire, and tools/oppo_probe.py is how.

Every frame, both ways:

    AA  length varint  00 00  command u16 LE  sequence u8  payload length u16 LE
        payload

    length   bytes after itself: 7 + the payload length (one byte on every
             frame seen)
    sequence ours count 01..FE and wrap; an answer echoes its query's

    0x010C  mode query, payload 01 01 -> 0x810C: status 00, then the window
            01 01 v1 [v2]: 08 00 off, 10 00 ANC, 00 01 ambient
    0x0404  mode set, payload 01 01 then 01 off, 02 ANC, 04 ambient
            -> 0x8404 payload 00, an ack; the query after it names the state
    0x0106  battery query, empty -> 0x8106: status 00, a count, then
            (index, value) pairs. Index 1 left, 2 right, 3 case; bit 7 of
            the value is charging, the low seven the level

The query answers use the child bitmap positions and the sets the parent
ones; both were seen to stick on this hardware, and only those three sets are
ever sent. Window values the brand's app knows but these buds never answered
(other NC levels, adaptive) are neither sent nor named.

The buds say nothing unasked, so the mode is polled every three seconds and,
on every tenth poll, the battery in its place. Fast Pair carries the battery
too and wins in the shell while it is up; this one is the fallback.
"""
import struct

from core.headset import SILENT, Headset, SetupError, address, choice
from core.links import Profile

UUID = "0000079a-d102-11e1-9b23-00025b00a5a5"

START = 0xAA
AFTER_LENGTH = 7    # 00 00, command, sequence, payload length

MODE_QUERY, MODE_ANSWER = 0x010C, 0x810C
MODE_SET, MODE_ACK = 0x0404, 0x8404
BATTERY_QUERY, BATTERY_ANSWER = 0x0106, 0x8106

MODE_QUERY_PAYLOAD = b"\x01\x01"
WINDOW = b"\x01\x01"

AVAILABLE = ["off", "anc", "ambient"]
# The three query windows these buds answered.
MODE_WINDOWS = {(0x08, 0x00): "off", (0x10, 0x00): "anc", (0x00, 0x01): "ambient"}
# The three sets that were sent and read back.
MODE_SETS = {"off": b"\x01\x01\x01", "anc": b"\x01\x01\x02", "ambient": b"\x01\x01\x04"}
BATTERY_PARTS = {1: "left", 2: "right", 3: "case"}
CHARGING_BIT = 0x80
LEVEL_BITS = 0x7F

FIRST_SEQUENCE, LAST_SEQUENCE = 0x01, 0xFE

ANSWER_MS = 3000        # no mode by now: these buds do not speak this
POLL_MS = 3000          # slower than a person tapping, cheap on RFCOMM
BATTERY_EVERY = 10      # polls, half a minute


def varint(value):
    out = bytearray()
    while True:
        low, value = value & 0x7F, value >> 7
        out.append(low | (0x80 if value else 0))
        if not value:
            return bytes(out)


def read_varint(data, at):
    """(value, the index after it), or None while it is incomplete."""
    value = shift = 0
    while at < len(data):
        byte = data[at]
        value |= (byte & 0x7F) << shift
        shift += 7
        at += 1
        if not byte & 0x80:
            return value, at
    return None


def frame(command, sequence, payload=b""):
    rest = (b"\x00\x00" + struct.pack("<HBH", command, sequence & 0xFF, len(payload))
            + bytes(payload))
    return bytes([START]) + varint(len(rest)) + rest


def unframe(buffer):
    """Whole frames off the front of a bytearray, as [(command, sequence, payload)].

    Bytes before an AA are noise. An AA whose next bytes are not 00 00, or
    whose two lengths disagree, did not start a frame, and the search goes
    on from the byte after it, so a stray AA cannot hold up what follows.
    """
    out = []
    while True:
        start = buffer.find(bytes([START]))
        if start < 0:
            buffer.clear()
            return out
        del buffer[:start]
        length = read_varint(buffer, 1)
        if length is None:
            return out
        size, head = length
        if size < AFTER_LENGTH:
            del buffer[:1]
            continue
        if len(buffer) < head + AFTER_LENGTH:
            if buffer[head:head + 2] not in (b"", b"\x00", b"\x00\x00"):
                del buffer[:1]
                continue
            return out
        command, sequence, count = struct.unpack_from("<HBH", buffer, head + 2)
        if buffer[head:head + 2] != b"\x00\x00" or size != AFTER_LENGTH + count:
            del buffer[:1]
            continue
        end = head + AFTER_LENGTH + count
        if len(buffer) < end:
            return out
        out.append((command, sequence, bytes(buffer[head + AFTER_LENGTH:end])))
        del buffer[:end]


def mode_of(payload):
    """The panel's name for a mode answer, or None for a window never seen.

    The window is the first 01 01 after the status byte; a missing second
    value byte reads as 00.
    """
    at = bytes(payload).find(WINDOW, 1)
    if at < 0 or at + 2 >= len(payload):
        return None
    second = payload[at + 3] if at + 3 < len(payload) else 0x00
    return MODE_WINDOWS.get((payload[at + 2], second))


def battery_of(payload):
    """({part: level}, [charging parts]) from a battery answer, or None.

    The whole answer is refused if any listed level is over 100, or if it
    names no part this bridge knows.
    """
    if len(payload) < 2:
        return None
    count = payload[1]
    pairs = payload[2:2 + 2 * count]
    if len(pairs) < 2 * count:
        return None
    levels, charging = {}, []
    for index, value in zip(pairs[0::2], pairs[1::2]):
        part = BATTERY_PARTS.get(index)
        if part is None:
            continue
        if value & LEVEL_BITS > 100:
            return None
        levels[part] = value & LEVEL_BITS
        if value & CHARGING_BIT:
            charging.append(part)
    return (levels, charging) if levels else None


class Oppo(Headset):
    brand = "oppo"
    label = "Oppo"
    usage = "<classic-address>"

    @classmethod
    def parse(cls, args):
        if len(args) != 1:
            raise SetupError("usage: cornerstone-bridge oppo " + cls.usage)
        return {"address": address(args[0])}

    def __init__(self, host, address):
        super().__init__(host)
        self.address = address
        self.buffer = bytearray()
        self.sequence = FIRST_SEQUENCE
        self.mode = None
        self.battery = None     # (levels, charging)
        self.polls = 0
        self.polling = None

    def links(self):
        return [Profile(self.address, UUID)]

    def opened(self, link):
        self.ask_mode()
        self.later(ANSWER_MS, self.give_up_if_silent)

    def next_sequence(self):
        sequence = self.sequence
        self.sequence = FIRST_SEQUENCE if sequence >= LAST_SEQUENCE else sequence + 1
        return sequence

    def query(self, command, payload=b""):
        self.send(frame(command, self.next_sequence(), payload))

    def ask_mode(self):
        self.query(MODE_QUERY, MODE_QUERY_PAYLOAD)

    def ask_battery(self):
        self.query(BATTERY_QUERY)

    def poll(self):
        self.polls += 1
        if self.polls % BATTERY_EVERY == 0:
            self.ask_battery()
        else:
            self.ask_mode()

    def give_up_if_silent(self):
        if self.mode is None:
            self.fail(SILENT, "the buds did not answer the listening-mode query")

    def received(self, data, link):
        self.buffer += data
        for command, _sequence, payload in unframe(self.buffer):
            if self.finished:
                return
            self.answer(command, payload)

    def answer(self, command, payload):
        if command == MODE_ANSWER:
            mode = mode_of(payload)
            if mode is None:
                return          # a state never seen here leaves the mode as it was
            first = self.mode is None
            self.mode = mode
            self.publish()
            if first:
                self.ask_battery()
                self.polling = self.every(POLL_MS, self.poll)
        elif command == BATTERY_ANSWER:
            battery = battery_of(payload)
            if battery is not None:
                self.battery = battery
                self.publish()
        elif command == MODE_ACK:
            self.ask_mode()     # the set landed; the query says what it did

    def publish(self):
        if self.mode is None:
            return
        line = {"modes": True, "mode": self.mode, "available": list(AVAILABLE)}
        if self.battery is not None:
            levels, charging = self.battery
            line["battery"] = dict(levels, charging=list(charging))
        self.report(line)

    def command(self, verb, arg):
        mode = choice(arg, MODE_SETS) if verb == "set" else None
        if mode is not None and self.mode is not None:
            self.query(MODE_SET, MODE_SETS[mode])


HEADSET = Oppo
