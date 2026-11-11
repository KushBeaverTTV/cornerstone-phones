"""EarFun Air Pro 4: Off / ANC / Ambient.

    cornerstone-bridge earfun <classic-address> <reported-name>

Confirmed on an EarFun Air Pro 4 (firmware TW600-3091_20250919_v0.4.1),
docs/captures/earfun-air-pro-4.txt. The command numbers are the ones
Gadgetbridge's EarFun support uses; only those these earbuds answered are
sent. The battery is not read here: the earbuds give left, right and case on
the Fast Pair Message Stream.

Standard SPP, 00001101-0000-1000-8000-00805f9b34fb, connected through a
Profile1. GAIA-shaped frames with no checksum:

    FF  version  flags  length u8  vendor u16 BE  command u16 BE  payload

    sent        version 04, flags 00, vendor 000A
    received    version 03, flags 00, vendor 000A; an answer carries the
                command with 0x8000 set and the payload <status 00> <value>

    0315        get the noise control: 00 off, 01 noise cancelling, 02 transparency
    0314        set it, payload the value. Nothing answers a set, and the get
                goes on answering the old value until the change is done (0.7
                to 2.1 s after the set in the capture)

The earbuds also send frames of their own (034D, 0350, 0358, no answer bit),
often several in one read; they are framed and skipped. A change made on the
earbuds was never seen announced.

What is sent: the 0315 get when the channel opens, once more 2 s later if it
went unanswered, and every 10 s; on `set`, one 0314 set, then the get every
second, at most five times, until the earbuds report the mode that was set.
"""
from core.headset import SILENT, Headset, SetupError, address, choice
from core.links import Profile

UUID = "00001101-0000-1000-8000-00805f9b34fb"

START = 0xFF
VERSION_OURS = 0x04
VERSION_THEIRS = 0x03
FLAGS = 0x00
VENDOR = 0x000A
HEADER = 8                 # start byte through the command
ANSWER_BIT = 0x8000
NOISE_GET = 0x0315
NOISE_SET = 0x0314
OK = 0x00

MODE_NAMES = {0x00: "off", 0x01: "anc", 0x02: "ambient"}
VALUES = {name: value for value, name in MODE_NAMES.items()}

# Keyed by the reported name exactly as the BACKENDS row passes it. A model
# nobody has held has no row: its values may not mean the same.
MODELS = {
    "EarFun Air Pro 4": {"available": ["off", "anc", "ambient"]},
}

ASK_AGAIN_MS = 2000    # the get was answered in 20-50 ms; one more before giving up
SILENCE_MS = 5000      # still no mode: exit 3
POLL_MS = 10000        # changes on the earbuds are not announced
READBACK_MS = 1000     # after a set, until the earbuds agree
READBACKS = 5


def frame(command, payload=b""):
    return (bytes([START, VERSION_OURS, FLAGS, len(payload)])
            + VENDOR.to_bytes(2, "big") + command.to_bytes(2, "big") + bytes(payload))


def mode_get():
    return frame(NOISE_GET)


def mode_set(mode):
    return frame(NOISE_SET, bytes([VALUES[mode]]))


def unframe(buffer):
    """Take whole frames the earbuds could have sent off the front of a bytearray.

    A start byte not followed by the earbuds' version and flags is not a frame
    start, and the search moves on one byte. The length is one byte, so no
    length is absurd; a frame cut short waits for the rest.
    """
    out = []
    while True:
        start = buffer.find(START)
        if start < 0:
            buffer.clear()
            return out
        del buffer[:start]
        if (len(buffer) >= 2 and buffer[1] != VERSION_THEIRS) or \
                (len(buffer) >= 3 and buffer[2] != FLAGS):
            del buffer[:1]
            continue
        if len(buffer) < HEADER:
            return out
        total = HEADER + buffer[3]
        if len(buffer) < total:
            return out
        out.append(bytes(buffer[:total]))
        del buffer[:total]


def mode_of(raw):
    """The mode an answer to the 0315 get carries, or None for any other frame.

    Our own frames echoed back have version 04 and no answer bit; the
    earbuds' own frames have no answer bit; other answers are other commands.
    """
    if len(raw) < HEADER or raw[0] != START or raw[1] != VERSION_THEIRS:
        return None
    vendor = int.from_bytes(raw[4:6], "big")
    command = int.from_bytes(raw[6:8], "big")
    payload = raw[HEADER:]
    if vendor != VENDOR or command != NOISE_GET | ANSWER_BIT:
        return None
    if len(payload) != 2 or payload[0] != OK:
        return None
    return MODE_NAMES.get(payload[1])


class EarFun(Headset):
    brand = "earfun"
    label = "EarFun"
    usage = "<classic-address> <reported-name>"

    @classmethod
    def parse(cls, args):
        if len(args) != 2:
            raise SetupError("usage: cornerstone-bridge earfun " + cls.usage)
        model = MODELS.get(args[1])
        if model is None:
            raise SetupError("no EarFun model row for %r" % args[1])
        return {"address": address(args[0]), "model": model}

    def __init__(self, host, address, model):
        super().__init__(host)
        self.address = address
        self.model = model
        self.buffer = bytearray()
        self.mode = None
        self.wanted = None            # the mode the last set asked for
        self.readbacks_left = 0
        self.readback_timer = None

    def links(self):
        return [Profile(self.address, UUID)]

    def opened(self, link):
        self.ask()
        self.later(ASK_AGAIN_MS, self.ask_if_silent)
        self.later(SILENCE_MS, self.give_up_if_silent)
        self.every(POLL_MS, self.ask)

    def ask(self):
        self.send(mode_get())

    def ask_if_silent(self):
        if self.mode is None:
            self.ask()

    def give_up_if_silent(self):
        if self.mode is None:
            self.fail(SILENT, "the EarFun earbuds did not answer")

    def received(self, data, link):
        self.buffer += data
        for raw in unframe(self.buffer):
            if self.finished:
                return
            mode = mode_of(raw)
            if mode in self.model["available"]:
                self.mode = mode
                if mode == self.wanted:
                    self.stop_readback()
                self.report({"modes": True, "mode": mode,
                             "available": list(self.model["available"])})

    def command(self, verb, arg):
        # Nothing is sent before the earbuds have said what they are set to.
        if verb != "set" or self.mode is None:
            return
        mode = choice(arg, self.model["available"])
        if mode is None:
            return
        self.send(mode_set(mode))
        self.wanted = mode
        # A set during a readback refills its count; one timer at a time.
        self.readbacks_left = READBACKS
        if self.readback_timer is None:
            self.readback_timer = self.every(READBACK_MS, self.read_back)

    def read_back(self):
        if self.mode == self.wanted or self.readbacks_left <= 0:
            self.stop_readback()
            return
        self.readbacks_left -= 1
        self.ask()

    def stop_readback(self):
        self.readbacks_left = 0
        self.cancel(self.readback_timer)
        self.readback_timer = None


HEADSET = EarFun
