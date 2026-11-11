"""HUAWEI FreeBuds SE 2: battery only.

    cornerstone-bridge huawei <classic-address> <reported-name>

Owner's capture: docs/captures/huawei-freebuds-se-2.txt (PROTOCOL.md has the
long version). The FreeBuds SE 2 has no noise control and no Fast Pair
stream, and BlueZ alone sees one figure for the pair, so this brand exists
for the per-bud battery. Every line is `"modes": true, "available": []`,
which tells the panel to draw no mode row, with the battery; every command
is ignored without a frame.

The channel is plain RFCOMM, dialled by the number the model's row names,
behind the Serial Port UUID; nothing in the SDP record is Huawei's own. That
is why the reported name must match a row exactly: a name with no row is
exit 4 rather than a guess at somebody else's serial port. The earbuds serve
the channel to one client. While a phone's AI Life app holds it they refuse
everyone else, and the error line says so; the shell keeps retrying, and the
channel comes free when the phone lets go.

Every frame, both ways:

    5A  length u16 BE  00  service  command  TLVs  crc u16 BE

    length  from the 00 to the last TLV byte
    TLV     tag u8, length u8, value
    crc     CRC-16/XMODEM over every byte before it

    01 08   battery. Asked with empty tags 1, 2 and 3; answered with tag 1 the
            overall level, tag 2 left, right, case, tag 3 a byte for each that
            looks like charging and is not read: the case's is relayed by a
            docked bud and goes stale without one, and the buds' never changed
    01 27   the same tags, sent by the earbuds on their own on every change
    01 06   empty, once, as the channel opens; not a battery answer
    2B 5F   the lid closing on a bud (01) and opening again (00); not read

A bud in a closed case reads 0, and is left out of the line rather than drawn
empty. No part is ever reported charging, for the tag 3 reason above.
"""
import errno
import os

from core import codec
from core.headset import SILENT, TRANSIENT, Headset, SetupError, address
from core.links import Rfcomm

# Keyed by the name the earbuds report, exactly as bluetoothctl prints it.
MODELS = {
    "HUAWEI FreeBuds SE 2": {"channel": 1},
}

SOF = 0x5A
SERVICE = 0x01
BATTERY = 0x08
BATTERY_EVENT = 0x27
PARTS = ("left", "right", "case")
LEVELS_TAG = 2

SILENCE_MS = 5000       # the battery query gets this long
POLL_MS = 60000         # a backstop: the earbuds announce changes themselves

REFUSED = "the earbuds refused the battery channel: another device (Huawei AI Life) may hold it"


def frame(service, command, tags=()):
    body = bytes([service, command])
    for tag, value in tags:
        body += bytes([tag, len(value)]) + bytes(value)
    head = bytes([SOF]) + (len(body) + 1).to_bytes(2, "big") + b"\x00"
    return head + body + codec.crc16_xmodem(head + body).to_bytes(2, "big")


BATTERY_QUERY = frame(SERVICE, BATTERY, [(1, b""), (2, b""), (3, b"")])


def unframe(buffer):
    """Whole frames off the front of a bytearray, as [(service, command, tlv bytes)].

    Bytes before a 5A are noise and go. A frame too short to hold a service
    and a command, without its 00, or with a CRC that fails is dropped whole.
    """
    out = []
    while True:
        start = buffer.find(SOF)
        if start < 0:
            buffer.clear()
            return out
        del buffer[:start]
        if len(buffer) < 3:
            return out
        size = int.from_bytes(buffer[1:3], "big")
        total = 3 + size + 2
        if len(buffer) < total:
            return out
        raw = bytes(buffer[:total])
        del buffer[:total]
        if size < 3 or raw[3] != 0x00:
            continue
        if int.from_bytes(raw[-2:], "big") != codec.crc16_xmodem(raw[:-2]):
            continue
        out.append((raw[4], raw[5], raw[6:-2]))


def tlvs(data):
    """{tag: value} for each whole TLV, or None when a length runs off the end."""
    out = {}
    at = 0
    while at < len(data):
        if at + 2 > len(data):
            return None
        tag, size = data[at], data[at + 1]
        if at + 2 + size > len(data):
            return None
        out[tag] = bytes(data[at + 2:at + 2 + size])
        at += 2 + size
    return out


def battery_levels(data):
    """{"left": 95, "right": 96, "case": 64} from a battery answer, or None.

    Tag 2 must be exactly three levels, each 0-100, or the frame says nothing.
    A 0 is a part not reporting (a bud under a closed lid), not an empty one,
    and is left out. Tag 3 is not read.
    """
    tags = tlvs(data)
    if tags is None:
        return None
    raw = tags.get(LEVELS_TAG)
    if raw is None or len(raw) != len(PARTS) or any(level > 100 for level in raw):
        return None
    return {part: level for part, level in zip(PARTS, raw) if level}


class Huawei(Headset):
    brand = "huawei"
    label = "Huawei"
    usage = "<classic-address> <reported-name>"

    @classmethod
    def parse(cls, args):
        if not 1 <= len(args) <= 2:
            raise SetupError("usage: cornerstone-bridge huawei " + cls.usage)
        where = address(args[0].strip())
        name = args[1] if len(args) == 2 else ""
        model = MODELS.get(name)
        if model is None:
            raise SetupError("usage: cornerstone-bridge huawei %s (no row for the model %r)"
                             % (cls.usage, name))
        return {"address": where, "model": model}

    def __init__(self, host, address, model):
        super().__init__(host)
        self.address = address
        self.model = model
        self.buffer = bytearray()
        self.levels = None
        self.silence = None

    def links(self):
        return [Rfcomm(self.address, [self.model["channel"]],
                       attempts=6, retry_ms=1500, timeout_s=5.0)]

    def unavailable(self, link, message, code):
        # The runtime words a failed dial "cannot open the Huawei channel:
        # <strerror>". Refused is what these earbuds answer while another
        # device holds the channel, so that one is said plainly.
        refused = os.strerror(errno.ECONNREFUSED)
        if code == TRANSIENT and message and message.endswith(": " + refused):
            message = REFUSED
        self.fail(code, message)

    def opened(self, link):
        self.ask()
        self.silence = self.later(SILENCE_MS, self.give_up_if_silent)
        self.every(POLL_MS, self.ask)

    def ask(self):
        self.send(BATTERY_QUERY)

    def give_up_if_silent(self):
        if self.levels is None:
            self.fail(SILENT, "the earbuds did not answer the battery query")

    def received(self, data, link):
        self.buffer += data
        for service, command, body in unframe(self.buffer):
            if self.finished:
                return
            if service != SERVICE or command not in (BATTERY, BATTERY_EVENT):
                continue
            levels = battery_levels(body)
            if levels is None:
                continue
            if self.levels is None:
                self.cancel(self.silence)
            self.levels = levels
            battery = dict(levels, caseStale=False)
            self.report({"modes": True, "available": [], "battery": battery})

    def command(self, verb, arg):
        """No listening modes, nothing to set: every line is ignored."""


HEADSET = Huawei
