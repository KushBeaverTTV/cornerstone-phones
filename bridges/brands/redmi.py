"""Redmi Buds 5 and Redmi Buds 3 Pro: battery and Off / ANC / Ambient.

    cornerstone-bridge redmi <classic-address> [<reported-name>]

Without a name this is the Redmi Buds 5 (their BACKENDS row passes none).
"Redmi Buds 3 Pro" picks that model's row; any other name is exit 4.

Confirmed on a Redmi Buds 5 (docs/captures/redmi-buds-5.txt) and a Redmi Buds
3 Pro (docs/captures/redmi-buds-3-pro.txt). Xiaomi's RCSP, the Jieli framing,
on the 16-bit service 0000fd2d, which BlueZ connects through a Profile1:

    FE DC BA  flags  opcode  length u16 BE  body  EF

    flags   0x80 a command, 0x40 it wants a response, 0x04 the app's field
            (the earbuds' own announcements carry C7)
    body    a command's:  sn  param
            a response's: status  sn  payload      (status 0 is success)

    0x02    GetTargetInfo, param FF FF FF FF. The payload is <length> <type>
            <value> attributes, length counting the type; type 7 is one byte
            each for left, right, case: the level in the low seven bits, bit 7
            charging, FF no reading
    0xF3    GetDeviceConfig, param the config id u16 BE; answered with the
            config. A config is <length> <id u16 BE> <value>, length counting
            the id and the value
    0xF2    SetDeviceConfig, param the config; acknowledged with status 0, and
            then announced by the earbuds as an 0xF4 command
    0x0E    the earbuds' status report, attributes as GetTargetInfo's

    config 11, the noise control: <state> <level>, state 0 off, 1 noise
    cancelling, 2 transparency; level is the strength inside a state, and a
    set sends the one that state was last seen at, or 0

The Buds 3 Pro answer nothing until a mutual authentication is done (first
described by Gadgetbridge's Redmi Buds support):

    ours     ->  C4 50  sn 01 <16-byte challenge>
             <-  04 50  status sn 01 <their response>   checked against ours
             ->  C4 51  sn 01 00
    theirs   <-  C0 50  sn 01 <16-byte challenge>
             ->  04 50  00 sn 01 <our response>
             <-  C0 51  sn 01 00                        their confirmation
             ->  04 51  00 sn 01

    response SAFER+ in Bluetooth's Ar' form (the E1 block cipher), keyed by
             the challenge with its last byte XOR 06, over the fixed block
             11 22 33 33 22 11 repeated to 16 bytes

They also repeat each announcement (0x0E, 0xF4) every two seconds until it
is answered with an empty response carrying its sn, and their status reports
carry the battery as attribute 0, in GetTargetInfo's three-byte layout. The
Buds 5 were seen to need neither, and their reports carried no battery.

What is sent: the handshake where the row asks for it, the config 11 get and
the battery ask (again every 30 s), one config 11 set per `set`, a config 11
get 800 ms after it, and the answers to announcements where the row asks.
"""
import os
from dataclasses import dataclass
from typing import NamedTuple, Optional

from core.headset import SILENT, Headset, SetupError, address, choice
from core.links import Profile

UUID = "0000fd2d-0000-1000-8000-00805f9b34fb"

HEADER = b"\xFE\xDC\xBA"
TAIL = 0xEF
OVERHEAD = 8               # header, flags, opcode, length, tail
LARGEST_BODY = 1024        # anything longer is not a frame of this protocol

COMMAND = 0x80
WANTS_REPLY = 0x40
APP = 0x04

TARGET_INFO = 0x02
STATUS_REPORT = 0x0E
AUTH_CHALLENGE = 0x50
AUTH_CONFIRM = 0x51
SET_CONFIG = 0xF2
GET_CONFIG = 0xF3
CONFIG_CHANGED = 0xF4
ANNOUNCEMENTS = (STATUS_REPORT, CONFIG_CHANGED)

NOISE_CONFIG = 11
BATTERY_ATTRIBUTE = 7          # in GetTargetInfo
REPORT_BATTERY_ATTRIBUTE = 0   # in a status report (Buds 3 Pro)
NO_READING = 0xFF
CHARGING_BIT = 0x80
PARTS = ("left", "right", "case")

MODE_NAMES = {0: "off", 1: "anc", 2: "ambient"}
STATES = {name: state for state, name in MODE_NAMES.items()}
AVAILABLE = ["off", "anc", "ambient"]

SILENCE_MS = 5000      # nothing read by now (handshake included): exit 3
GRACE_MS = 2000        # how long a battery waits for the mode to go out with it
POLL_MS = 30000        # the earbuds were not heard to announce a battery change
READBACK_MS = 800      # the config get after a set


@dataclass(frozen=True)
class Model:
    """What one model needs beyond the Buds 5's conversation."""
    authenticate: bool = False     # the handshake first, nothing else before it
    acknowledge: bool = False      # answer the earbuds' announcements
    report_battery: bool = False   # read the battery from their status reports


# Keyed by the reported name, exactly as the BACKENDS row passes it. No name
# is the Redmi Buds 5, which have no row of their own.
BUDS_5 = Model()
MODELS = {
    "Redmi Buds 3 Pro": Model(authenticate=True, acknowledge=True, report_battery=True),
}


# ---- frames

def command(opcode, sn, param=b"", flags=COMMAND | WANTS_REPLY):
    body = bytes([sn & 0xFF]) + bytes(param)
    return HEADER + bytes([flags, opcode]) + len(body).to_bytes(2, "big") + body + bytes([TAIL])


def response(opcode, sn, payload=b""):
    """Our answer to a command of the earbuds': status 0 and their sn."""
    body = bytes([0, sn & 0xFF]) + bytes(payload)
    return HEADER + bytes([APP, opcode]) + len(body).to_bytes(2, "big") + body + bytes([TAIL])


def target_info_get(sn):
    return command(TARGET_INFO, sn, b"\xFF\xFF\xFF\xFF")


def noise_get(sn):
    return command(GET_CONFIG, sn, NOISE_CONFIG.to_bytes(2, "big"))


def noise_set(sn, state, level):
    config = bytes([4]) + NOISE_CONFIG.to_bytes(2, "big") + bytes([state, level])
    return command(SET_CONFIG, sn, config)


def challenge_frame(sn, challenge):
    return command(AUTH_CHALLENGE, sn, b"\x01" + bytes(challenge), COMMAND | WANTS_REPLY | APP)


def confirm_frame(sn):
    return command(AUTH_CONFIRM, sn, b"\x01\x00", COMMAND | WANTS_REPLY | APP)


class Frame(NamedTuple):
    is_command: bool
    wants_reply: bool
    opcode: int
    sn: Optional[int]
    status: Optional[int]      # None for a command
    payload: bytes


def read_frame(raw):
    """A whole frame, header to tail, as a Frame."""
    flags, opcode, body = raw[3], raw[4], bytes(raw[7:-1])
    if flags & COMMAND:
        return Frame(True, bool(flags & WANTS_REPLY), opcode,
                     body[0] if body else None, None, body[1:])
    if len(body) < 2:
        return Frame(False, False, opcode, None, None, b"")
    return Frame(False, False, opcode, body[1], body[0], body[2:])


def unframe(buffer):
    """Take whole frames off the front of a bytearray, as raw bytes.

    Bytes before a header are noise. A length past LARGEST_BODY, or a tail
    that is not where the length says, drops the header's first byte and the
    search goes on, so garbage cannot stall the stream or swallow the frame
    after it. A partial header at the end is kept for the next read.
    """
    out = []
    while True:
        start = buffer.find(HEADER)
        if start < 0:
            keep = next((n for n in (2, 1) if buffer.endswith(HEADER[:n])), 0)
            del buffer[:len(buffer) - keep]
            return out
        del buffer[:start]
        if len(buffer) < 7:
            return out
        size = int.from_bytes(buffer[5:7], "big")
        if size > LARGEST_BODY:
            del buffer[:1]
            continue
        total = OVERHEAD + size
        if len(buffer) < total:
            return out
        if buffer[total - 1] != TAIL:
            del buffer[:1]
            continue
        out.append(bytes(buffer[:total]))
        del buffer[:total]


def attributes(data):
    """<length> <type> <value> entries as {type: value}; stops at damage."""
    out, index = {}, 0
    while index + 2 <= len(data):
        length = data[index]
        if length == 0 or index + 1 + length > len(data):
            break
        out[data[index + 1]] = bytes(data[index + 2:index + 1 + length])
        index += 1 + length
    return out


def configs(data):
    """<length> <id u16 BE> <value> entries as {id: value}; stops at damage."""
    out, index = {}, 0
    while index + 3 <= len(data):
        length = data[index]
        if length < 2 or index + 1 + length > len(data):
            break
        out[int.from_bytes(data[index + 1:index + 3], "big")] = bytes(data[index + 3:index + 1 + length])
        index += 1 + length
    return out


def battery_levels(values):
    """The panel's battery object from one byte per part, or None if no part reads."""
    battery, charging = {}, []
    for part, byte in zip(PARTS, values):
        level = byte & 0x7F
        if byte == NO_READING or level > 100:
            continue
        battery[part] = level
        if byte & CHARGING_BIT:
            charging.append(part)
    if not battery:
        return None
    battery["charging"] = charging
    return battery


def target_battery(frame):
    """The battery a GetTargetInfo answer carries, or None."""
    if frame.is_command or frame.opcode != TARGET_INFO or frame.status != 0:
        return None
    values = attributes(frame.payload).get(BATTERY_ATTRIBUTE)
    return battery_levels(values) if values else None


def report_battery(frame):
    """The battery an earbuds' status report carries, or None."""
    if not frame.is_command or frame.opcode != STATUS_REPORT:
        return None
    values = attributes(frame.payload).get(REPORT_BATTERY_ATTRIBUTE)
    return battery_levels(values) if values and len(values) == 3 else None


def noise_control(frame):
    """(state, level) from a config get's answer or an announcement, or None."""
    if frame.is_command:
        if frame.opcode != CONFIG_CHANGED:
            return None
    elif frame.opcode != GET_CONFIG or frame.status != 0:
        return None
    value = configs(frame.payload).get(NOISE_CONFIG)
    if value is None or len(value) < 2 or value[0] not in MODE_NAMES:
        return None
    return value[0], value[1]


# ---- the authentication: SAFER+ as Bluetooth's Ar' (Core spec, E1)

def _powers():
    exp = [pow(45, i, 257) % 256 for i in range(256)]     # 45^128 = 256 -> 0
    log = [0] * 256
    for i, value in enumerate(exp):
        log[value] = i
    return exp, log


EXP, LOG = _powers()
# Byte positions that combine with a round key by XOR; the rest by addition.
XOR_LANES = (0, 3, 4, 7, 8, 11, 12, 15)
# The Armenian shuffle, applied between the four pseudo-Hadamard layers.
SHUFFLE = (8, 11, 12, 15, 2, 1, 6, 5, 10, 9, 14, 13, 0, 7, 4, 3)
ROUNDS = 8
AUTH_INPUT = bytes([0x11, 0x22, 0x33, 0x33, 0x22, 0x11] * 3)[:16]


def _combine(block, key, xor_lanes):
    return [(b ^ k) if i in xor_lanes else (b + k) & 0xFF
            for i, (b, k) in enumerate(zip(block, key))]


def _diffuse(block):
    for layer in range(4):
        mixed = []
        for i in range(0, 16, 2):
            a, b = block[i], block[i + 1]
            mixed += [(2 * a + b) & 0xFF, (a + b) & 0xFF]
        block = mixed if layer == 3 else [mixed[i] for i in SHUFFLE]
    return block


def _schedule(key):
    """The 17 round keys of SAFER+ for a 128-bit key."""
    register = list(key)
    parity = 0
    for byte in key:
        parity ^= byte
    register.append(parity)
    keys = [list(key)]
    for k in range(2, 18):
        register = [((byte << 3) | (byte >> 5)) & 0xFF for byte in register]
        keys.append([(register[(k - 1 + j) % 17]
                      + EXP[EXP[(17 * k + j + 1) % 256]]) & 0xFF for j in range(16)])
    return keys


def auth_response(challenge):
    """The 16 bytes that answer a 16-byte challenge, either way round."""
    key = bytearray(challenge)
    key[15] ^= 0x06
    keys = _schedule(bytes(key))
    add_lanes = tuple(i for i in range(16) if i not in XOR_LANES)
    plain = list(AUTH_INPUT)
    block = list(plain)
    for r in range(ROUNDS):
        if r == 2:
            block = _combine(block, plain, XOR_LANES)   # Ar': the input again
        block = _combine(block, keys[2 * r], XOR_LANES)
        block = [EXP[b] if i in XOR_LANES else LOG[b] for i, b in enumerate(block)]
        block = _combine(block, keys[2 * r + 1], add_lanes)
        block = _diffuse(block)
    return bytes(_combine(block, keys[16], XOR_LANES))


def random_challenge():
    return os.urandom(16)


class Redmi(Headset):
    brand = "redmi"
    label = "Redmi"
    usage = "<classic-address> [<reported-name>]"

    @classmethod
    def parse(cls, args):
        if len(args) not in (1, 2):
            raise SetupError("usage: cornerstone-bridge redmi " + cls.usage)
        if len(args) == 1:
            model = BUDS_5
        elif args[1] in MODELS:
            model = MODELS[args[1]]
        else:
            raise SetupError("no Redmi model row for %r" % args[1])
        return {"address": address(args[0]), "model": model}

    def __init__(self, host, address, model=BUDS_5, new_challenge=random_challenge):
        super().__init__(host)
        self.address = address
        self.model = model
        self.new_challenge = new_challenge
        self.buffer = bytearray()
        self.sn = 0
        self.mode = None
        self.levels = {}          # state -> the level it was last seen at
        self.battery = None
        self.answered = False
        self.grace_over = False
        # the handshake; a model without one starts authenticated
        self.authenticated = not model.authenticate
        self.challenge = None
        self.ours_checked = False
        self.theirs_confirmed = False

    def links(self):
        return [Profile(self.address, UUID)]

    def next_sn(self):
        self.sn = (self.sn + 1) & 0xFF
        return self.sn

    # ---- the conversation

    def opened(self, link):
        if self.authenticated:
            self.ask()
        else:
            self.challenge = bytes(self.new_challenge())
            self.send(challenge_frame(self.next_sn(), self.challenge))
        self.later(SILENCE_MS, self.give_up_if_silent)
        self.later(GRACE_MS, self.end_grace)
        self.every(POLL_MS, self.ask)

    def ask(self):
        if self.authenticated:
            self.send(noise_get(self.next_sn()))
            self.send(target_info_get(self.next_sn()))

    def read_back(self):
        if self.authenticated:
            self.send(noise_get(self.next_sn()))

    def give_up_if_silent(self):
        # Linked and asked (or challenged), and nothing came of it: the
        # channel is served but not this conversation.
        if not self.answered:
            self.fail(SILENT, "the Redmi Buds did not answer")

    def end_grace(self):
        # The mode had its chance to go out with the battery; if it has not
        # come, the battery goes out alone rather than not at all.
        self.grace_over = True
        self.publish()

    def received(self, data, link):
        self.buffer += data
        for raw in unframe(self.buffer):
            if self.finished:
                return
            frame = read_frame(raw)
            if self.authenticated:
                self.on_frame(frame)
            else:
                self.on_handshake(frame)

    def on_handshake(self, frame):
        if frame.is_command and frame.sn is not None:
            if (frame.opcode == AUTH_CHALLENGE and len(frame.payload) == 17
                    and frame.payload[0] == 1):
                self.send(response(AUTH_CHALLENGE, frame.sn,
                                   b"\x01" + auth_response(frame.payload[1:])))
            elif frame.opcode == AUTH_CONFIRM and self.ours_checked:
                # Taken only once ours has been checked: earlier, it would
                # authenticate a side that has proved nothing.
                self.theirs_confirmed = True
                self.send(response(AUTH_CONFIRM, frame.sn, b"\x01"))
        elif (frame.opcode == AUTH_CHALLENGE and frame.status is not None
              and self.challenge is not None and not self.ours_checked):
            if frame.status != 0 or frame.payload != b"\x01" + auth_response(self.challenge):
                self.fail(SILENT, "the Redmi Buds did not answer the authentication")
                return
            self.ours_checked = True
            self.send(confirm_frame(self.next_sn()))
        if self.ours_checked and self.theirs_confirmed:
            self.authenticated = True
            self.ask()

    def on_frame(self, frame):
        if (self.model.acknowledge and frame.is_command and frame.wants_reply
                and frame.opcode in ANNOUNCEMENTS and frame.sn is not None):
            self.send(response(frame.opcode, frame.sn))
        noise = noise_control(frame)
        if noise is not None:
            state, level = noise
            self.answered = True
            self.mode = MODE_NAMES[state]
            self.levels[state] = level
            self.publish()
            return
        battery = target_battery(frame)
        if battery is None and self.model.report_battery:
            battery = report_battery(frame)
        if battery is not None:
            self.answered = True
            self.battery = battery
            self.publish()

    def publish(self):
        if self.mode is None and (self.battery is None or not self.grace_over):
            return
        line = {"modes": True, "available": list(AVAILABLE) if self.mode else []}
        if self.mode:
            line["mode"] = self.mode
        if self.battery:
            line["battery"] = dict(self.battery, charging=list(self.battery["charging"]))
        self.report(line)

    def command(self, verb, arg):
        # Nothing is sent before the earbuds have said what they are set to.
        if verb != "set" or self.mode is None or not self.authenticated:
            return
        mode = choice(arg, STATES)
        if mode is None:
            return
        state = STATES[mode]
        self.send(noise_set(self.next_sn(), state, self.levels.get(state, 0)))
        self.later(READBACK_MS, self.read_back)


HEADSET = Redmi
