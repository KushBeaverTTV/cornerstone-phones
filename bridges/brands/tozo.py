"""TOZO NC9 Pro: battery (buds and case) and six listening modes, over BLE GATT.

    cornerstone-bridge tozo <classic-address> <reported-name>

Only its owner's model has a row. The earbuds serve GATT on their public
address, which in the owner's session equalled the Classic one; BlueZ owns
the connection and every notification is one whole datagram. See PROTOCOL.md,
"TOZO NC9 Pro", and docs/TOZO-NC9-PRO-TESTS.md.

    service  0000b610-...   write B611 (without response)   notify B612
    value    group  command  length  payload  checksum
             checksum: the payload's sum, low byte. A query has no payload.

    buds (group 00)
      00 02 00 00   battery         -> 00 02 02 <left> <right> <sum>
      00 30 00 00   mode            -> 00 30 01 <mode> <sum>
      00 20 00 00   case address    -> 00 20 06 <6 bytes, MSB first> <sum>
      10 <c> 01 ..  set a mode      -> 10 <c> 01 00 00, an ACK that names no mode
      20 0d 02 01 ..                   a change the earbuds announce

    case (service 000001ff-3c17-d293-8e48-14fe2e4da212, same B611/B612, group 01)
      01 09 00 00   its earbuds     -> 01 09 06 <6 bytes> <sum>
      01 02 00 00   battery         -> 01 02 01 <case> <sum>

Right after a set the earbuds were seen to report a passing mode, so the mode
shown is never the one asked for, nor the ACK: 1.5 s after the ACK (or after
an announced change) the mode is asked again, and that answer is the mode.
Sets run one at a time. The mode is polled every 10 s, the battery every 30 s.
Writes are spaced 150 ms apart.

The case is a second, optional link. Its address comes from the earbuds and is
believed only once the case names these earbuds back. Losing it never costs
the modes or the earbud battery: a case reading kept after the case went quiet
carries "caseStale": true, and the case is tried again a minute later. No
charging encoding was observed, so none is reported.
"""
from collections import deque

from core.headset import SILENT, TRANSIENT, Headset, SetupError, address, choice
from core.links import Gatt

SERVICE = "0000b610-0000-1000-8000-00805f9b34fb"
CASE_SERVICE = "000001ff-3c17-d293-8e48-14fe2e4da212"
NOTIFY = "0000b612-0000-1000-8000-00805f9b34fb"
WRITE = "0000b611-0000-1000-8000-00805f9b34fb"

BUDS, CASE = "buds", "case"      # the two links' names

MODE_GET = bytes.fromhex("00300000")
BATTERY_GET = bytes.fromhex("00020000")
CASE_ADDRESS_GET = bytes.fromhex("00200000")
CASE_BATTERY_GET = bytes.fromhex("01020000")
CASE_PEER_GET = bytes.fromhex("01090000")

BUDS_GROUP, CASE_GROUP, SET_GROUP, EVENT_GROUP = 0x00, 0x01, 0x10, 0x20
BATTERY, CASE_ADDRESS, MODE, PEER, CHANGED = 0x02, 0x20, 0x30, 0x09, 0x0D
NO_ADDRESS = ("00:00:00:00:00:00", "FF:FF:FF:FF:FF:FF")

# Keyed by the exact reported name.
MODELS = {
    "TOZO NC9 Pro": {
        "modes": {0: "off", 1: "anc", 2: "ambient", 3: "wind", 4: "leisure", 6: "adaptive"},
        # In drawing order: this is also the line's "available".
        "sets": {
            "off": bytes.fromhex("1004010000"),
            "anc": bytes.fromhex("1004010101"),
            "ambient": bytes.fromhex("1005010101"),
            "wind": bytes.fromhex("1007010101"),
            "leisure": bytes.fromhex("1008010101"),
            "adaptive": bytes.fromhex("1011010101"),
        },
    },
}

SETTLE_MS = 1500          # after an ACK or an announced change, before asking
REPLY_MS = 8000           # a mode query, or the case, unanswered this long
GAP_MS = 150              # between two writes
MODE_POLL_MS = 10000
BATTERY_POLL_MS = 30000
CASE_RETRY_MS = 60000
QUEUE_LIMIT = 16


def datagram(raw):
    """(group, command, payload) from one whole value, or None.

    The length byte must account for every byte and the checksum must hold:
    a partial, glued or damaged value is not read at all.
    """
    raw = bytes(raw)
    if len(raw) < 5 or len(raw) != raw[2] + 4:
        return None
    payload = raw[3:-1]
    if sum(payload) & 0xFF != raw[-1]:
        return None
    return raw[0], raw[1], payload


def bluetooth_address(payload):
    return ":".join("%02X" % byte for byte in payload)


class Tozo(Headset):
    brand = "tozo"
    label = "TOZO"
    usage = "<classic-address> <reported-name>"

    @classmethod
    def parse(cls, args):
        if len(args) != 2:
            raise SetupError("usage: cornerstone-bridge tozo " + cls.usage)
        model = MODELS.get(args[1])
        if model is None:
            raise SetupError("no TOZO model row for %r" % args[1])
        return {"address": address(args[0]), "model": model}

    def __init__(self, host, address, model):
        super().__init__(host)
        self.address = address
        self.model = model
        self.mode = None
        self.battery = {}
        self.case_address = None
        self.case_verified = False
        self.pending = None            # the mode a set is under way for
        self.pending_asked = False     # its read-back query has gone out
        self.queue = deque()           # sets waiting their turn
        self.outbox = deque()          # (link, frame) waiting for the gap
        self.gap = None
        self.readback = None           # timer: ask the mode again
        self.reply = None              # timer: a mode answer is overdue
        self.case_wait = None          # timer: the case answer is overdue
        self.case_retry = None         # timer: try the case again

    def links(self):
        return [Gatt(self.address, SERVICE, NOTIFY, WRITE, name=BUDS)]

    # ---- links

    def opened(self, link):
        if self.finished:
            return
        if link == BUDS:
            self.expect_reply(REPLY_MS)
            self.every(MODE_POLL_MS, self.poll_mode)
            self.every(BATTERY_POLL_MS, self.poll_battery)
            for frame in (MODE_GET, BATTERY_GET, CASE_ADDRESS_GET):
                self.write(BUDS, frame)
        elif link == CASE:
            self.case_verified = False
            self.expect_case()
            self.write(CASE, CASE_PEER_GET)

    def closed(self, link, reason):
        if link == BUDS:
            self.fail(TRANSIENT, "TOZO control connection closed")
        else:
            self.case_lost()

    def unavailable(self, link, message, code):
        if link == BUDS:
            self.fail(code, message)
        else:
            self.case_lost()

    # ---- writing, one every GAP_MS

    def write(self, link, frame):
        if not self.finished:
            self.outbox.append((link, frame))
            self.pump()

    def pump(self):
        if self.gap is not None or not self.outbox or self.finished:
            return
        link, frame = self.outbox.popleft()
        if link == CASE:
            self.expect_case()
        elif frame == MODE_GET:
            self.expect_reply(REPLY_MS)
            if self.pending is not None:
                self.pending_asked = True
        self.send(frame, link)
        self.gap = self.later(GAP_MS, self.gap_over)

    def gap_over(self):
        self.gap = None
        self.pump()

    def drop_queued(self, keep):
        self.outbox = deque(item for item in self.outbox if keep(*item))

    # ---- timers

    def expect_reply(self, ms):
        self.cancel(self.reply)
        self.reply = self.later(ms, self.reply_overdue)

    def reply_overdue(self):
        self.reply = None
        if self.mode is None:
            self.fail(SILENT)
        else:
            self.fail(TRANSIENT, "TOZO mode readback timed out")

    def read_back_in(self, ms):
        self.cancel(self.readback)
        self.readback = self.later(ms, self.read_back)

    def read_back(self):
        self.readback = None
        self.write(BUDS, MODE_GET)

    def poll_mode(self):
        if self.pending is None and self.readback is None:
            self.write(BUDS, MODE_GET)

    def poll_battery(self):
        self.write(BUDS, BATTERY_GET)
        if self.case_verified:
            self.write(CASE, CASE_BATTERY_GET)

    def expect_case(self):
        self.cancel(self.case_wait)
        self.case_wait = self.later(REPLY_MS, self.case_silent)

    def case_silent(self):
        self.case_wait = None
        self.close(CASE)
        self.case_lost()

    def case_lost(self):
        """The case went away or went quiet. The earbuds carry on."""
        self.case_verified = False
        self.cancel(self.case_wait)
        self.case_wait = None
        self.drop_queued(lambda link, _frame: link != CASE)
        if "case" in self.battery:
            self.battery["caseStale"] = True
            self.publish()
        self.cancel(self.case_retry)
        self.case_retry = self.later(CASE_RETRY_MS, self.retry_case)

    def retry_case(self):
        self.case_retry = None
        if self.case_address:
            self.open_case()

    def open_case(self):
        self.open(Gatt(self.case_address, CASE_SERVICE, NOTIFY, WRITE, name=CASE, scan=True))

    # ---- what arrives

    def received(self, data, link):
        if self.finished:
            return
        parsed = datagram(data)
        if parsed is None:
            return
        if link == CASE:
            self.from_case(*parsed)
        else:
            self.from_buds(*parsed)

    def from_buds(self, group, command, payload):
        if group == BUDS_GROUP and command == MODE and len(payload) == 1:
            self.mode_answer(payload[0])
        elif group == BUDS_GROUP and command == BATTERY and len(payload) == 2:
            # No charging bit or "unknown" value has been seen on this model,
            # so anything over 100 is refused rather than guessed at.
            if all(level <= 100 for level in payload):
                self.battery.update(left=payload[0], right=payload[1])
                self.publish()
        elif group == BUDS_GROUP and command == CASE_ADDRESS and len(payload) == 6:
            found = bluetooth_address(payload)
            if self.case_address is None and found not in NO_ADDRESS + (self.address,):
                self.case_address = found
                self.open_case()
        elif group == SET_GROUP and self.pending is not None:
            if command == self.model["sets"][self.pending][1] and payload == b"\x00":
                # Success, not the new mode: the earbuds change after it.
                self.read_back_in(SETTLE_MS)
        elif group == EVENT_GROUP and command == CHANGED and len(payload) == 2 and payload[0] == 1:
            # Ask, rather than read the event's second byte as a mode.
            self.read_back_in(SETTLE_MS)

    def mode_answer(self, value):
        mode = self.model["modes"].get(value)
        if mode is None:
            return
        if self.pending is not None and not self.pending_asked:
            # An answer to a query from before the set: not its outcome.
            return
        self.mode = mode
        self.cancel(self.reply)
        self.reply = None
        self.pending = None
        self.publish()
        self.next_set()

    def from_case(self, group, command, payload):
        if group != CASE_GROUP:
            return
        if command == PEER and len(payload) == 6:
            self.case_verified = bluetooth_address(payload) == self.address
            if self.case_verified:
                self.write(CASE, CASE_BATTERY_GET)
            else:
                # Some other pair's case: not ours to read.
                self.close(CASE)
                self.case_lost()
        elif command == BATTERY and len(payload) == 1 and self.case_verified and payload[0] <= 100:
            self.cancel(self.case_wait)
            self.case_wait = None
            self.battery.update(case=payload[0], caseStale=False)
            self.publish()

    def publish(self):
        line = {"modes": self.mode is not None}
        if self.mode is not None:
            line.update(mode=self.mode, available=list(self.model["sets"]))
        if self.battery:
            line["battery"] = dict(self.battery)
        self.report(line)

    # ---- the panel

    def command(self, verb, arg):
        mode = choice(arg, self.model["sets"]) if verb == "set" else None
        if mode is None or self.mode is None:
            return
        if len(self.queue) < QUEUE_LIMIT:
            self.queue.append(mode)
        self.next_set()

    def next_set(self):
        if self.finished or self.pending is not None or not self.queue:
            return
        self.pending = self.queue.popleft()
        self.pending_asked = False
        # A query already waiting would be answered with the old mode.
        self.drop_queued(lambda link, frame: not (link == BUDS and frame == MODE_GET))
        self.write(BUDS, self.model["sets"][self.pending])
        self.read_back_in(SETTLE_MS)
        self.expect_reply(SETTLE_MS + REPLY_MS)


HEADSET = Tozo
