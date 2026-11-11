"""JBL TUNE230NC TWS and Wave Buds 2: Off / ANC / Ambient Aware / TalkThru.

    cornerstone-bridge jbl <ble-address> [<fast-pair-model-id>]

The listening mode lives on the earbuds' BLE side, on a vendor service whose
UUID spells its maker, 65786365-6c70-6f69-6e74-2e636f6d0000 ("excelpoint.com").
The address is the resolvable private one the Fast Pair stream announces, so
it rotates; BlueZ has no device object for it until something scans, which is
why the link is btgatt-client dialling the address directly rather than
BlueZ's GATT API. Its stdout lines are what we read, its stdin takes
register-notify and write-value commands. See PROTOCOL.md, "The control
protocol lives on BLE".

    frame   AA <cmd> <payload length> <payload...>   no sequence, no checksum
    aa 9b 02 01 01          notifications on
    aa 91 01 11             get the mode
    aa 91 07 10 <slots>     set the mode
    aa 91 07 12 <slots>     the mode, as a report (also unprompted: touch control)
    slots   01 <anc> 02 <ambient> 03 <talkthru>, each 00 or 01

A set is answered by a report of its own, so nothing is read back. The value
handles differ per model and the Fast Pair model id is the one thing a
connect names for free, so MODELS is keyed by it; a model not listed keeps
the TUNE230NC handles, where the protocol was first confirmed.

The conversation, each step waiting on the one before:

    btgatt-client says "GATT discovery procedures complete"   (15 s deadline)
    register-notify <notify handle>                           (5 s, then on anyway)
    2 s, aa 9b 02 01 01; 2 s, aa 91 01 11   (the earbuds drop a frame sent too soon)
    the first report                                          (12 s deadline)

Whether a model answers at all is remembered per Fast Pair model id in
$XDG_STATE_HOME/cornerstone-phones/mode-support.json, which the shell reads
(Model.supportVerdict): {"<id>": {"supported": true}} once one answered,
{"<id>": {"misses": n}} after silent runs, and "supported": false from the
third miss on. Only a run that connected, discovered, subscribed, asked and
heard nothing is a miss; a link that failed on the way says nothing about the
model and is not written down.
"""
import json
import os
import re

from core.codec import unhex
from core.headset import (SILENT, TRANSIENT, Headset, SetupError, address,
                          choice)
from core.links import Child

DEFAULT_HANDLES = {"notify": "0x000c", "write": "0x0010"}    # TUNE230NC TWS

# Fast Pair model id -> value handles on the excelpoint service.
MODELS = {
    "ea59a0": {"notify": "0xa205", "write": "0xa202"},       # Wave Buds 2
}

NOTIFY_ON = unhex("aa 9b 02 01 01")
GET_MODE = unhex("aa 91 01 11")
SET_MODE = {
    "off":      unhex("aa 91 07 10 01 00 02 00 03 00"),
    "anc":      unhex("aa 91 07 10 01 01 02 00 03 00"),
    "ambient":  unhex("aa 91 07 10 01 00 02 01 03 00"),
    "talkthru": unhex("aa 91 07 10 01 00 02 00 03 01"),
}

# A report, wherever btgatt-client puts it on its line.
REPORT = re.compile(r"aa 91 07 12 01 ([0-9a-f]{2}) 02 ([0-9a-f]{2}) 03 ([0-9a-f]{2})")

DISCOVERY_MS = 15000     # a deadline on a stale address, not a guess at discovery
REGISTER_MS = 5000       # a refusal comes as a line; silence is just slowness
FRAME_GAP_MS = 2000      # what tools/jbl-anc was tuned to on the earbuds
ANSWER_MS = 12000        # discovery is done by now: one round trip and thinking time

MISSES_BEFORE_GIVING_UP = 3


def mode_from_slots(anc, ambient, talkthru):
    """Only 00 and 01 have been seen; anything but 01 is "not this slot"."""
    if anc == "01":
        return "anc"
    if ambient == "01":
        return "ambient"
    if talkthru == "01":
        return "talkthru"
    return "off"


def write_value(handle, frame):
    """The btgatt-client command for one frame: each byte spelled 0x.."""
    return "write-value %s %s" % (handle, " ".join("0x%02x" % b for b in frame))


class SupportFile:
    """mode-support.json: which Fast Pair models answered, which never did.

    Small enough to be hand-edited, and an early version wrote bare booleans,
    so every entry is normalised to a dict on reading. A file that cannot be
    read is an empty one; one that cannot be written costs a probe next time,
    not correctness.
    """

    def __init__(self, path=None):
        if path is None:
            state = os.environ.get("XDG_STATE_HOME") or os.path.expanduser("~/.local/state")
            path = os.path.join(state, "cornerstone-phones", "mode-support.json")
        self.path = path

    def read(self):
        try:
            with open(self.path) as handle:
                known = json.load(handle)
        except (OSError, ValueError):
            return {}
        if not isinstance(known, dict):
            return {}
        entries = {}
        for model_id, entry in known.items():
            if isinstance(entry, dict):
                entries[model_id] = entry
            elif isinstance(entry, bool):
                entries[model_id] = {"supported": entry}
            else:
                entries[model_id] = {}
        return entries

    def write(self, known):
        try:
            os.makedirs(os.path.dirname(self.path), exist_ok=True)
            # One temporary name per process: two bridges racing on one name
            # would rename each other's half-written file into place.
            temporary = "%s.%d.tmp" % (self.path, os.getpid())
            with open(temporary, "w") as handle:
                json.dump(known, handle, separators=(",", ":"), sort_keys=True)
            os.replace(temporary, self.path)
        except OSError:
            pass

    def answered(self, model_id):
        known = self.read()
        if known.get(model_id, {}).get("supported") is True:
            return
        known[model_id] = {"supported": True}
        self.write(known)

    def missed(self, model_id):
        known = self.read()
        entry = known.get(model_id, {})
        if entry.get("supported") is True:
            # It answered once, so this was a bad connection, not a silent model.
            return
        try:
            misses = int(entry.get("misses", 0)) + 1
        except (TypeError, ValueError):
            misses = 1
        entry["misses"] = misses
        if misses >= MISSES_BEFORE_GIVING_UP:
            entry["supported"] = False
        known[model_id] = entry
        self.write(known)


class Jbl(Headset):
    brand = "jbl"
    label = "JBL"
    usage = "<ble-address> [<fast-pair-model-id>]"

    @classmethod
    def parse(cls, args):
        if len(args) not in (1, 2):
            raise SetupError("usage: cornerstone-bridge jbl " + cls.usage)
        return {"address": address(args[0].strip()),
                "model_id": args[1].strip() if len(args) > 1 else ""}

    def __init__(self, host, address, model_id="", support=None):
        super().__init__(host)
        self.address = address
        self.model_id = model_id
        self.handles = MODELS.get(model_id, DEFAULT_HANDLES)
        self.support = support if support is not None else SupportFile()
        self.mode = None
        self.linked = False         # "Connecting to device... Done" seen
        self.stage = "connecting"   # then registering, asking, asked
        self.deadline = None        # the one step-deadline timer pending

    def links(self):
        return [Child(["btgatt-client", "-d", self.address, "-t", "random"])]

    # ---- the opening conversation

    def opened(self, link):
        self.wait(DISCOVERY_MS, self.discovery_overdue)

    def wait(self, ms, callback):
        self.cancel(self.deadline)
        self.deadline = self.later(ms, callback)

    def discovery_overdue(self):
        self.deadline = None
        # Never connected is a rotated address, most likely; connected and
        # still walking the tree is the same story one step on. Neither says
        # anything about what the model can do.
        self.fail(TRANSIENT, "the BLE link never finished discovery" if self.linked
                  else "could not open the BLE link")

    def discovered(self):
        if self.stage != "connecting":
            return
        self.stage = "registering"
        self.tell("register-notify " + self.handles["notify"])
        self.wait(REGISTER_MS, self.subscribed)

    def subscribed(self):
        """The handler is registered, or REGISTER_MS passed without a word."""
        if self.stage != "registering":
            return
        self.stage = "asking"
        self.cancel(self.deadline)
        self.deadline = None
        self.later(FRAME_GAP_MS, self.notifications_on)

    def notifications_on(self):
        self.tell(write_value(self.handles["write"], NOTIFY_ON))
        self.later(FRAME_GAP_MS, self.ask)

    def ask(self):
        self.tell(write_value(self.handles["write"], GET_MODE))
        self.stage = "asked"
        if self.mode is None:
            self.wait(ANSWER_MS, self.answer_overdue)

    def answer_overdue(self):
        """Linked, discovered, subscribed, asked, and no report: a miss.

        Checked here, on the loop, so a report and this deadline cannot both
        win: whichever runs first decides, and a run something else already
        ended is not this model's silence either.
        """
        self.deadline = None
        if self.finished or self.mode is not None:
            return
        if self.model_id:
            self.support.missed(self.model_id)
        self.fail(SILENT, "the device did not answer the listening-mode query")

    @property
    def settled(self):
        """The opening is over: the query went out and was answered."""
        return self.stage == "asked" and self.mode is not None

    # ---- what btgatt-client says

    def received(self, line, link):
        if self.finished:
            return
        if "Connecting" in line and "Done" in line:
            self.linked = True
        if "Failed to connect" in line:
            reason = line.split("Failed to connect:")[-1].strip() or line
            self.fail(TRANSIENT, "cannot reach the earbuds over BLE: %s" % reason)
        elif "Device disconnected" in line:
            self.fail(TRANSIENT, "the BLE link closed")
        elif "GATT discovery procedures complete" in line:
            self.linked = True
            self.discovered()
        elif "GATT discovery procedures failed" in line:
            self.fail(TRANSIENT, "GATT discovery failed on this link")
        elif "Failed to register notify handler" in line or "GATT client not initialized" in line:
            # The link is up but will carry no notifications: about this
            # connection, not the model, so no miss.
            self.fail(TRANSIENT, line)
        elif "Registered notify handler" in line:
            self.subscribed()
        else:
            match = REPORT.search(line)
            if match:
                self.reported(mode_from_slots(*match.groups()))

    def reported(self, mode):
        if self.mode is None:
            if self.model_id:
                self.support.answered(self.model_id)
            if self.stage == "asked":
                self.cancel(self.deadline)
                self.deadline = None
        self.mode = mode
        # Every change, touch control included; the same mode twice is one line.
        self.report({"modes": True, "mode": mode})

    def closed(self, link, reason):
        # btgatt-client is gone, and the ATT link with it.
        self.fail(TRANSIENT, "the BLE link closed")

    # ---- the panel

    def command(self, verb, arg):
        mode = choice(arg, SET_MODE) if verb == "set" else None
        if mode is not None and self.mode is not None:
            # No read-back: a set is answered by a report of its own.
            self.tell(write_value(self.handles["write"], SET_MODE[mode]))

    def tell(self, text):
        self.send(text)


HEADSET = Jbl
