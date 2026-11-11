"""Shared test plumbing: a fake host for brand modules, and pin playback.

A brand module (bridges/brands/<brand>.py) touches the world only through its
host: send, emit, timers, a clock, opening links. FakeHost records all of it
and lets a test decide when time passes, so no test needs Bluetooth, D-Bus or
GLib, and none of them sleep.

Pins
----
tests/pins/<brand>/<model>.json is one model's frozen session: what the device
said, what the bridge wrote back and printed, step by step. A pin belongs to
the person whose headphones it describes, and is never edited to suit a code
change; tools/check refuses a branch that edits or removes one. The step
vocabulary (one key per step, plus an optional "note"):

    actions      device, command, ack, fire, call, wait_sent
    assertions   sent, sent_last, lines, line, line_has, line_without,
                 line_count, timers, exit, state

What "device" holds and what "sent" means is each brand's Session's business
(its docstring says); everything else means the same for every brand:

    command      one stdin line, e.g. "set anc"
    fire         run every pending timer once, in the order they were set
    call         a Session action by name: "open", or [name, args...]
    sent         every frame written so far, as the Session renders them
    lines        every stdout line so far
    timers       the intervals of every timer still pending, in order
    exit         the exit code so far (null while running)
    state        Session.state(name) for each name, compared
"""
import glob
import json
import os
import sys
import unittest

sys.dont_write_bytecode = True
HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.normpath(os.path.join(HERE, ".."))
BRIDGES = os.path.join(ROOT, "bridges")
PINS = os.path.join(HERE, "pins")
if BRIDGES not in sys.path:
    sys.path.insert(0, BRIDGES)

from core import codec  # noqa: E402
from core.headset import MAIN  # noqa: E402

hexbytes = codec.unhex
hexstr = codec.hexstr


class FakeHost:
    """A host whose clock moves only when a test says so."""

    def __init__(self):
        self.lines = []
        self.writes = []          # (link name, data), in order
        self.timers = []          # [ms, callback, args, repeat, serial, due]
        self.opened = []          # link specs opened while running
        self.closed = []          # link names closed
        self.finished = None
        self.clock = 1000.0
        self.serial = 0
        self.headset = None

    def emit(self, line):
        self.lines.append(json.loads(json.dumps(line)))

    def send(self, link, data):
        self.writes.append((link, data))

    def later(self, ms, callback, args, repeat):
        self.serial += 1
        self.timers.append([ms, callback, args, repeat, self.serial, self.clock + ms / 1000])
        return self.serial

    def cancel(self, handle):
        self.timers = [t for t in self.timers if t[4] != handle]

    def now(self):
        return self.clock

    def open(self, spec):
        self.opened.append(spec)

    def close(self, link):
        self.closed.append(link)

    def finish(self, code):
        self.finished = code

    # -- driving time

    def _run(self, timer):
        ms, callback, args, repeat, serial, _due = timer
        if repeat:
            timer[5] = self.clock + ms / 1000
            self.timers.append(timer)
        if self.headset is None or not self.headset.finished:
            callback(*args)

    def fire(self):
        """Every pending timer once, in the order they were set."""
        pending, self.timers = self.timers, []
        for timer in pending:
            self._run(timer)

    def advance(self, seconds):
        """Move the clock, running timers as they come due, earliest first."""
        target = self.clock + seconds
        while True:
            due = [t for t in self.timers if t[5] <= target]
            if not due:
                break
            timer = min(due, key=lambda t: (t[5], t[4]))
            self.timers.remove(timer)
            self.clock = max(self.clock, timer[5])
            self._run(timer)
        self.clock = target


class Session:
    """One headset on a FakeHost. Brand tests subclass this.

    A subclass builds self.headset in __init__ (through self.start), and
    defines device() for the shape its pins use. `sent` is every frame on the
    main link as hex; a subclass whose frames are text, or that has two links,
    overrides it.
    """

    def __init__(self):
        self.host = FakeHost()
        self.headset = None

    def start(self, headset_class, **options):
        self.headset = headset_class(self.host, **options)
        self.host.headset = self.headset
        return self.headset

    @classmethod
    def from_args(cls, headset_class, args):
        """A session started the way the runtime starts one: from argv."""
        session = cls.__new__(cls)
        Session.__init__(session)
        session.start(headset_class, **headset_class.parse(list(args)))
        return session

    # ---- actions

    def device(self, spec):
        raise NotImplementedError

    def receive(self, data, link=MAIN):
        self.headset.received(bytes(data) if not isinstance(data, str) else data, link)

    def command(self, line):
        self.headset.handle_line(line)

    def ack(self):
        raise NotImplementedError("this brand has no ACK")

    def fire(self):
        self.host.fire()

    def advance(self, seconds):
        self.host.advance(seconds)

    def call(self, name, *args):
        action = getattr(self, "do_" + name, None)
        if action is None:
            action = getattr(self.headset, name)
        return action(*args)

    def do_open(self):
        self.headset.opened(MAIN)

    def wait_sent(self, count):
        if len(self.sent) < count:
            raise AssertionError("only %d frames sent, wanted %d" % (len(self.sent), count))

    # ---- what happened

    @property
    def lines(self):
        return self.host.lines

    @property
    def frames(self):
        return [data for link, data in self.host.writes if link == MAIN]

    @property
    def sent(self):
        return [hexstr(data) for data in self.frames]

    @property
    def timers(self):
        return [t[0] for t in self.host.timers]

    @property
    def exit_code(self):
        return self.headset.exit_code

    def state(self, name):
        return getattr(self.headset, name)


def _check(test, session, key, value, where):
    if key == "sent":
        test.assertEqual(session.sent, value, where)
    elif key == "sent_last":
        test.assertTrue(session.sent, where + ": nothing sent")
        test.assertEqual(session.sent[-1], value, where)
    elif key == "lines":
        test.assertEqual(session.lines, value, where)
    elif key == "line":
        test.assertTrue(session.lines, where + ": nothing printed")
        test.assertEqual(session.lines[-1], value, where)
    elif key == "line_has":
        test.assertTrue(session.lines, where + ": nothing printed")
        for name, expected in value.items():
            test.assertIn(name, session.lines[-1], where)
            test.assertEqual(session.lines[-1][name], expected, "%s key %s" % (where, name))
    elif key == "line_without":
        test.assertTrue(session.lines, where + ": nothing printed")
        for name in value:
            test.assertNotIn(name, session.lines[-1], where)
    elif key == "line_count":
        test.assertEqual(len(session.lines), value, where)
    elif key == "timers":
        test.assertEqual(session.timers, value, where)
    elif key == "exit":
        test.assertEqual(session.exit_code, value, where)
    elif key == "state":
        for name, expected in value.items():
            test.assertEqual(session.state(name), expected, "%s attribute %s" % (where, name))
    else:
        raise AssertionError("%s: unknown step key %r" % (where, key))


def _act(session, key, value):
    if key == "device":
        session.device(value)
    elif key == "command":
        session.command(value)
    elif key == "ack":
        session.ack()
    elif key == "fire":
        session.fire()
    elif key == "call":
        if isinstance(value, list):
            session.call(value[0], *value[1:])
        else:
            session.call(value)
    elif key == "wait_sent":
        session.wait_sent(value)
    else:
        return False
    return True


def run_pin(test, session, pin, path):
    for index, step in enumerate(pin["steps"]):
        keys = [key for key in step if key != "note"]
        if len(keys) != 1:
            raise AssertionError("%s step %d: one key per step, got %r" % (path, index, keys))
        key = keys[0]
        where = "%s step %d (%s)%s" % (os.path.relpath(path, ROOT), index, key,
                                       ": " + step["note"] if "note" in step else "")
        if not _act(session, key, step[key]):
            _check(test, session, key, step[key], where)


def pins_for(brand):
    out = []
    for path in sorted(glob.glob(os.path.join(PINS, brand, "*.json"))):
        with open(path) as handle:
            out.append((path, json.load(handle)))
    return out


def pin_tests(namespace, brand, session_factory):
    """One TestCase per pin of a brand, added to a test module's namespace.

    session_factory(**pin["session"]) returns the Session to play the pin on.
    """
    pins = pins_for(brand)
    if not pins:
        raise AssertionError("no pins for %s under %s" % (brand, PINS))
    for path, pin in pins:
        stem = os.path.splitext(os.path.basename(path))[0]

        def test_frozen_session(self, pin=pin, path=path):
            session = session_factory(**(pin.get("session") or {}))
            if hasattr(session, "close"):
                self.addCleanup(session.close)
            run_pin(self, session, pin, path)

        name = "Pin_" + stem.replace("-", "_").replace(".", "_")
        namespace[name] = type(name, (unittest.TestCase,), {
            "test_frozen_session": test_frozen_session,
            "__doc__": "%s (%s). Frozen." % (pin["model"], pin["owner"]),
            "__module__": namespace["__name__"],
        })
