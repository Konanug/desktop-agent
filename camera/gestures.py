"""Turning a LEVEL into an EDGE, and publishing it as an event.

hands.json answers "what is the hand doing RIGHT NOW". That is a level, and a
level is the wrong shape to wire an action to: at HANDS_HZ it says PEACE ten
times a second for as long as you hold it, so a client that acts on it fires
ten times a second. The fix is not "poll slower" -- that only makes the number
of spurious actions smaller and adds latency. What an action needs is an EDGE:
*this gesture just started*, delivered exactly once.

That translation belongs HERE and not in the client, for the same reason the
staleness verdict lives in status.json rather than being inferred from a
stalled <img>: it is a fact this process is in the best position to know, and
duplicating it into every client is how it ends up subtly different in each one
and done properly in none.

THE THREE THINGS THAT MAKE IT AN EDGE

  1. DEBOUNCE. A single mis-detected frame must not break a held gesture, and a
     hand passing through a shape on its way to another must not fire it. So a
     value commits only when it holds MAJORITY of the last WINDOW observations
     -- 3 of 5, i.e. ~300 ms at 10 Hz -- rather than on a run of consecutive
     agreements, which one bad frame would reset forever.

  2. LATCH. Once committed, a gesture cannot fire again until the slot commits
     to something else (including "no hand"). Holding your hand up for a minute
     is one event, not six hundred.

  3. A SLIDING RATE LIMIT. Trap 19 in CLAUDE.md is the camera tool's per-turn
     counter that never reset and wedged the camera permanently. A limit that
     can reach a state it never leaves is worse than no limit, because it fails
     closed and silently. Both bounds here are sliding windows over wall time
     and cannot wedge.

A RATE-LIMITED GESTURE IS DROPPED, NOT DEFERRED. It still latches, so it is
never re-emitted once the limit lifts. Firing an action a second and a half
after the hand that meant it has moved on would be worse than not firing it.

TIMESTAMPS ARE MONOTONIC-DERIVED ON THE WIRE
`at` is wall clock and is for humans. Freshness travels as `age_s`, computed
from time.monotonic() at the moment of serialisation, because this Pi has no
battery-backed RTC (trap 6) and a client comparing two machines' wall clocks
would reject every event for the first ~34 s after a boot.

WHAT THIS DOES *NOT* DO
It does not act, and it does not reach Hermes. Nothing here can run a tool. The
deliberate non-build recorded in CLAUDE.md is the path from "someone waves in
the room" to "the agent runs a tool", and that path is still not built: a
camera authenticates nobody, so it cannot be an input to something holding a
shell. This module publishes edges to a token-gated socket, and the decision
about what any edge MEANS is taken by the subscriber, on the subscriber's own
machine, under the subscriber's own config. See docs/GESTURES.md.
"""

from __future__ import annotations

import collections
import threading
import time

# Debounce. 3 of the last 5 observations, which at HANDS_HZ = 10 is a ~300 ms
# hold before anything commits. MAJORITY > WINDOW/2 is load-bearing: it makes a
# tie impossible, so "the most common value" is never an arbitrary pick between
# two equals.
WINDOW = 5
MAJORITY = 3

# Sliding bounds. Deliberately looser than the >=1.5 s / <=6 per minute the
# plan file reserved for a hypothetical Hermes lane -- that lane would be
# driving an agent with a shell, this one drives the owner's own media keys.
# If a Hermes lane is ever built it needs its own, tighter, numbers.
#
# MIN_GAP is PER HAND and MAX_PER_MIN is GLOBAL, which is not an accident. A
# global minimum gap would silently drop one half of any two-handed gesture,
# because both hands commit on the same frame and the second would arrive
# nought seconds after the first. The thing worth rate limiting per hand is one
# hand's jitter; the thing worth bounding globally is the total number of
# actions the room can cause.
MIN_GAP = 0.8
MAX_PER_MIN = 30

# Events kept for a reconnecting client to ask about. Small on purpose: this is
# a replay buffer for a dropped connection, not a history anyone should mine.
CAPACITY = 64

# MediaPipe labels handedness as if looking at the person, and can return
# neither label; "?" is a real slot rather than an error so an unlabelled hand
# still debounces against itself instead of being discarded.
SLOTS = ("LEFT", "RIGHT", "?")


class Event:
    """One gesture edge. Immutable once published."""

    __slots__ = ("seq", "at", "mono", "hand", "gesture", "fingers_up",
                 "bbox", "score")

    def __init__(self, seq: int, at: float, mono: float, hand: str,
                 gesture: str, fingers_up: int, bbox, score: float):
        self.seq = seq
        self.at = at
        self.mono = mono
        self.hand = hand
        self.gesture = gesture
        self.fingers_up = fingers_up
        self.bbox = bbox
        self.score = score

    def as_dict(self, now_mono: float | None = None) -> dict:
        """`age_s` is stamped HERE, at serialisation, not at publication.

        A replayed event is genuinely older than a live one and must say so, or
        a client reconnecting after a two minute outage would replay a burst of
        stale gestures as though they had just happened.
        """
        now_mono = time.monotonic() if now_mono is None else now_mono
        x0, y0, x1, y1 = self.bbox
        return {
            "seq": self.seq,
            "at": round(self.at, 3),
            "age_s": round(now_mono - self.mono, 3),
            "hand": self.hand,
            "gesture": self.gesture,
            "fingers_up": self.fingers_up,
            "score": round(self.score, 3),
            "bbox": [round(v, 4) for v in self.bbox],
            # Where in frame, for a client that wants to point at something.
            # Normalised 0..1 in the frame as DELIVERED, which is rotated --
            # see HERMES_CAMERA_ROTATE before mapping this to screen space.
            "center": [round((x0 + x1) / 2, 4), round((y0 + y1) / 2, 4)],
        }


class EventLog:
    """A small ring of published events, plus a wait that clients can block on.

    The wait has exactly the shape forced by trap 22 on the frame buffer: a
    WHILE keyed on the cursor, re-deriving the remaining timeout each pass.
    Condition.wait() returns spuriously, and the obvious `if nothing: wait()`
    would hand a subscriber an empty list and make it reconnect in a loop.
    """

    def __init__(self, capacity: int = CAPACITY):
        self._cond = threading.Condition()
        self._events: collections.deque = collections.deque(maxlen=capacity)
        self._seq = 0

    @property
    def cursor(self) -> int:
        with self._cond:
            return self._seq

    def publish(self, at: float, mono: float, hand: str, gesture: str,
                fingers_up: int, bbox, score: float) -> Event:
        with self._cond:
            self._seq += 1
            ev = Event(self._seq, at, mono, hand, gesture, fingers_up,
                       bbox, score)
            self._events.append(ev)
            self._cond.notify_all()
            return ev

    def since(self, cursor: int) -> list[Event]:
        """Everything newer than `cursor`, oldest first.

        A cursor older than the ring returns what survives; the caller sees a
        gap between its cursor and the first seq and knows it missed events,
        which is strictly better than pretending it did not.
        """
        with self._cond:
            return [e for e in self._events if e.seq > cursor]

    def wait_for(self, cursor: int, timeout: float) -> list[Event]:
        deadline = time.monotonic() + timeout
        with self._cond:
            while True:
                got = [e for e in self._events if e.seq > cursor]
                if got:
                    return got
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return []
                self._cond.wait(remaining)

    def clear(self) -> None:
        """Tracking stopped. Drop the ring so a client reconnecting later
        cannot be handed gestures from a previous session as if they were
        current -- the same rule as StreamBuffer.drop()."""
        with self._cond:
            self._events.clear()
            self._cond.notify_all()


class _Slot:
    """One hand's debounce state. LEFT and RIGHT are fully independent."""

    __slots__ = ("name", "_obs", "committed", "hand", "last_fire")

    def __init__(self, name: str):
        self.name = name
        self._obs: collections.deque = collections.deque(maxlen=WINDOW)
        self.committed: str | None = None
        self.hand = None            # the Hand behind the committed gesture
        # None, not 0.0. time.monotonic()'s origin is explicitly undefined, so
        # a sentinel of zero means "never fired" on a machine whose monotonic
        # clock starts large and "fired just now" on one where it starts near
        # zero -- and on the latter the very first gesture is swallowed.
        self.last_fire: float | None = None

    def reset(self) -> None:
        self._obs.clear()
        self.committed = None
        self.hand = None
        self.last_fire = None

    def observe(self, hand) -> tuple[bool, str | None]:
        """Feed one observation. Returns (changed, newly committed value).

        `hand` is a hands.Hand or None for "no hand in this slot". Absence is
        an observation like any other -- that is what clears the latch when you
        drop your hand, and it must debounce too, or a single frame where the
        tracker loses the hand would re-arm a gesture you are still holding.
        """
        gesture = hand.gesture if hand is not None else None
        self._obs.append(gesture)
        value, n = collections.Counter(self._obs).most_common(1)[0]
        if n < MAJORITY or value == self.committed:
            return False, self.committed
        self.committed = value
        # Keep the hand that carried the winning value, not whichever arrived
        # last -- the last frame may be the one stray reading in the window.
        self.hand = hand if gesture == value else None
        return True, value


class GestureGate:
    """Debounced gesture edges from a stream of hand-tracking results."""

    def __init__(self, log: EventLog | None = None, min_gap: float = MIN_GAP,
                 max_per_min: int = MAX_PER_MIN):
        self.log = log if log is not None else EventLog()
        self.min_gap = min_gap
        self.max_per_min = max_per_min
        self._slots = {n: _Slot(n) for n in SLOTS}
        self._recent: collections.deque = collections.deque()
        self.fired = 0
        self.suppressed = 0
        self.last_event: Event | None = None

    def reset(self) -> None:
        """Called when tracking stops. Everything about the previous session is
        wrong once the camera has been closed and reopened."""
        for s in self._slots.values():
            s.reset()
        self._recent.clear()
        self.last_event = None
        self.log.clear()

    # -- rate limiting ---------------------------------------------------
    def _allowed(self, slot: "_Slot", mono: float) -> bool:
        """Both bounds are SLIDING windows and therefore cannot wedge.

        Trap 19: hermes_camera's per-turn cap keyed on task_id only ever went
        up, so after N captures the camera refused permanently and reported its
        limit as exhausted until the gateway was restarted. Anything that gates
        an action here gets the same scrutiny.
        """
        while self._recent and mono - self._recent[0] > 60.0:
            self._recent.popleft()
        if slot.last_fire is not None and mono - slot.last_fire < self.min_gap:
            return False
        return len(self._recent) < self.max_per_min

    # -- the gate --------------------------------------------------------
    def observe(self, result, now: float | None = None,
                mono: float | None = None) -> list[Event]:
        """Feed ONE tracker result. Returns the events it produced.

        Call this once per NEW result, not once per stream frame. Feeding the
        same result twice would count as two observations and halve the real
        hold time the debounce demands, which is the difference between "a
        deliberate gesture" and "a hand that happened to pass through a shape".
        """
        if result is None:
            return []
        now = time.time() if now is None else now
        mono = time.monotonic() if mono is None else mono

        # Best-scoring hand per slot. Two hands can both be labelled RIGHT when
        # the tracker is unsure; collapsing to the more confident one is better
        # than letting them fight over one debounce window.
        seen: dict[str, object] = {}
        for h in result.hands:
            key = (h.handedness or "?").upper()
            if key not in self._slots:
                key = "?"
            if key not in seen or h.score > seen[key].score:
                seen[key] = h

        out = []
        for name, slot in self._slots.items():
            changed, value = slot.observe(seen.get(name))
            if not changed or value is None:
                # None is a commit too -- it is what clears the latch when the
                # hand leaves -- but "no hand" is not an event.
                continue
            if not self._allowed(slot, mono):
                # Latched above, so this is DROPPED and will not be re-emitted
                # when the limit lifts. See the module docstring.
                self.suppressed += 1
                continue
            hand = slot.hand
            ev = self.log.publish(
                at=result.at, mono=mono, hand=name, gesture=value,
                fingers_up=hand.count if hand else 0,
                bbox=hand.bbox if hand else (0.0, 0.0, 0.0, 0.0),
                score=hand.score if hand else 0.0)
            self._recent.append(mono)
            slot.last_fire = mono
            self.fired += 1
            self.last_event = ev
            out.append(ev)
        return out
