"""A gesture must fire ONCE, and a limit must never wedge.

Four failures are pinned here. Three of them are the obvious ways a
level-to-edge translation goes wrong, and the fourth has already happened in
this project in a different guise:

  * FIRING PER FRAME. hands.json says PEACE ten times a second for as long as
    you hold it. A client wired to the level pauses and unpauses your music
    five times a second. Holding a gesture must be ONE event.

  * BREAKING ON ONE BAD FRAME. Detection is not perfect; a blurred frame in
    the middle of a held PEACE reads as something else. A debounce built on
    consecutive agreement resets on that frame and never commits. It must
    tolerate flicker.

  * REPLAY. A client reconnecting must not be handed a burst of gestures the
    person made minutes ago and act on all of them.

  * A COUNTER THAT ONLY GOES UP. Trap 19: hermes_camera's per-turn capture cap
    keyed on task_id never reset, so after N captures the camera refused
    PERMANENTLY and said its limit was exhausted. Anything that gates an action
    gets a sliding window and a test that proves it recovers.

No camera and no mediapipe required: the gate is fed synthetic hands.

Run:  python3 tests/test_gestures.py
"""

import json
import os
import sys
import threading
import time
import urllib.error
import urllib.request

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from camera import gestures, stream                          # noqa: E402


# -- synthetic hands ------------------------------------------------------
class _Hand:
    """Just enough of hands.Hand for the gate. Using the real class would drag
    in mediapipe-shaped landmark data for no benefit -- the gate only ever
    reads these six attributes."""

    def __init__(self, gesture, handedness="Right", score=0.9, count=None):
        # gesture=None is a REAL case, not a missing hand: it is what
        # hands.classify() returns for a shape outside the closed vocabulary.
        self.gesture = gesture
        self.handedness = handedness
        self.score = score
        self.count = count if count is not None else len(gesture or "")
        self.bbox = (0.1, 0.2, 0.3, 0.4)


class _Result:
    def __init__(self, hands, at=None):
        self.hands = hands
        self.at = time.time() if at is None else at

    def fresh(self, now=None):
        return True


def _feed(gate, gesture, n, hand="Right", t0=0.0, dt=0.1):
    """n observations of one gesture (None = no hand). Returns events fired.

    Time is passed in explicitly. The gate's rate limiting is real, so a test
    that let it use the wall clock would be a test of how fast the machine
    runs the loop.
    """
    out = []
    for i in range(n):
        hands = [_Hand(gesture, hand)] if gesture else []
        out += gate.observe(_Result(hands), now=t0 + i * dt,
                            mono=t0 + i * dt)
    return out


# -- debounce and latch ---------------------------------------------------
def test_a_held_gesture_fires_exactly_once():
    """THE WHOLE POINT. Twenty observations of PEACE is one event, not twenty."""
    g = gestures.GestureGate()
    evs = _feed(g, "PEACE", 20)
    assert len(evs) == 1, f"a held gesture fired {len(evs)} times"
    assert evs[0].gesture == "PEACE" and evs[0].hand == "RIGHT"


def test_it_takes_a_real_hold_to_fire():
    """A hand passing THROUGH a shape on the way to another must not fire it."""
    g = gestures.GestureGate()
    assert _feed(g, "PEACE", gestures.MAJORITY - 1) == [], \
        "fired before the debounce window was satisfied"


def test_one_bad_frame_does_not_break_a_held_gesture():
    """THE REGRESSION A CONSECUTIVE-RUN DEBOUNCE WOULD FAIL.

    Detection is not perfect. A blurred frame mid-hold reads as something else;
    a debounce that demands N consecutive agreements resets on it, and with
    flicker at any rate near N it never commits at all.
    """
    g = gestures.GestureGate()
    out = []
    for i, val in enumerate(["PEACE", "PEACE", "FIST", "PEACE", "PEACE"]):
        out += _feed(g, val, 1, t0=i * 0.1)
    assert len(out) == 1, f"flicker produced {len(out)} events"
    assert out[0].gesture == "PEACE", "the stray frame won"


def test_the_same_gesture_refires_only_after_it_clears():
    """Hold, drop the hand, hold again = two deliberate gestures = two events.
    Without dropping it, it stays latched."""
    g = gestures.GestureGate()
    a = _feed(g, "FIST", 6, t0=0.0)
    b = _feed(g, "FIST", 6, t0=10.0)          # still up, never cleared
    assert len(a) == 1 and len(b) == 0, "refired without clearing"
    _feed(g, None, 6, t0=20.0)                # hand leaves
    c = _feed(g, "FIST", 6, t0=30.0)
    assert len(c) == 1, "would not refire after clearing"


def test_no_hand_is_never_an_event():
    """Absence clears the latch. It is not itself something to act on."""
    g = gestures.GestureGate()
    assert _feed(g, None, 20) == [], "empty frames produced events"


def test_a_hand_making_no_vocabulary_gesture_fires_nothing():
    """THE COMPLAINT THAT PROMPTED THE CLOSED VOCABULARY.

    classify() used to name every finger pattern, so a hand in view was
    permanently asserting a command. hands.py now returns None for anything
    outside the vocabulary, and the gate must treat that exactly like an empty
    frame -- a hand resting in shot is not an instruction.
    """
    g = gestures.GestureGate()
    # _Hand(None) stands for a real hand whose shape is not in the vocabulary.
    assert _feed(g, None, 20) == []
    out = []
    for i in range(20):
        out += g.observe(_Result([_Hand(None, "Right")]),
                         now=i * 0.1, mono=i * 0.1)
    assert out == [], "a hand in an unrecognised shape fired a command"


def test_moving_between_gestures_does_not_fire_the_poses_in_between():
    """Opening a fist passes through several finger patterns. With an open
    vocabulary each was a nameable gesture and each fired; with a closed one
    they fall in the gap and only the endpoints commit."""
    g = gestures.GestureGate(min_gap=0.0)
    out, t = [], 0.0
    for value in ("FIST", None, None, None, "OPEN"):      # None = transitional
        for _ in range(5):
            out += g.observe(_Result([_Hand(value, "Right")] if value
                                     else [_Hand(None, "Right")]),
                             now=t, mono=t)
            t += 0.1
    assert [e.gesture for e in out] == ["FIST", "OPEN"], \
        f"transitional poses fired: {[e.gesture for e in out]}"


def test_the_vocabulary_can_be_narrowed():
    """An owner who only wants PINCH to do anything should not have to bind
    everything else to nothing on every client -- and narrowing here also
    stops the unwanted ones consuming the rate limit."""
    g = gestures.GestureGate(vocabulary={"PINCH"})
    assert _feed(g, "PEACE", 6, t0=0.0) == [], "fired outside the vocabulary"
    _feed(g, None, 6, t0=1.0)
    assert len(_feed(g, "PINCH", 6, t0=2.0)) == 1
    assert g.suppressed == 0, "a narrowed gesture must not count as rate-limited"


def test_latency_and_dwell_are_reported():
    """The lag has two independent halves and they are fixed by different
    knobs -- HANDS_HZ for inference, WINDOW/MAJORITY for the hold. Reporting
    one number would hide which one to turn."""
    g = gestures.GestureGate()
    out = []
    for i in range(6):                      # frames 0.1s apart
        out += g.observe(_Result([_Hand("FIST", "Right")], at=i * 0.1),
                         now=i * 0.1 + 0.05, mono=i * 0.1)
    assert len(out) == 1
    ev = out[0]
    assert 40 < ev.latency_ms < 60, f"latency_ms={ev.latency_ms}"
    assert ev.dwell_ms >= 190, f"dwell_ms={ev.dwell_ms} (expected ~200)"
    d = ev.as_dict()
    assert "latency_ms" in d and "dwell_ms" in d


def test_hands_are_independent():
    """A gesture on the left must not latch the right."""
    g = gestures.GestureGate()
    out = []
    for i in range(8):
        out += g.observe(_Result([_Hand("PEACE", "Right"),
                                  _Hand("FIST", "Left")]),
                         now=i * 0.1, mono=i * 0.1)
    assert len(out) == 2, f"expected one event per hand, got {len(out)}"
    assert {e.hand for e in out} == {"LEFT", "RIGHT"}
    assert {e.gesture for e in out} == {"PEACE", "FIST"}


def test_unlabelled_hands_get_their_own_slot():
    g = gestures.GestureGate()
    evs = _feed(g, "POINT", 6, hand="?")
    assert len(evs) == 1 and evs[0].hand == "?"


# -- rate limiting --------------------------------------------------------
def test_min_gap_drops_rather_than_defers():
    """A suppressed gesture is DROPPED. Firing an action a second after the
    hand that meant it has moved on is worse than not firing it."""
    g = gestures.GestureGate(min_gap=5.0)
    a = _feed(g, "FIST", 6, t0=0.0)
    _feed(g, None, 6, t0=1.0)
    b = _feed(g, "PEACE", 6, t0=2.0)          # inside the 5 s gap
    assert len(a) == 1 and len(b) == 0
    # ... and it must not surface later, once the gap has passed.
    c = _feed(g, "PEACE", 6, t0=30.0)
    assert len(c) == 0, "a suppressed gesture was deferred, not dropped"
    assert g.suppressed == 1


def test_the_rate_limit_is_a_sliding_window_and_cannot_wedge():
    """TRAP 19, in a new place.

    The camera tool's per-turn cap only ever went up, so once exhausted it
    refused permanently and reported its limit as spent until the gateway was
    restarted. Anything gating an action here must recover on its own.
    """
    g = gestures.GestureGate(min_gap=0.0, max_per_min=3)
    fired = 0
    for i in range(10):                       # ten gestures inside one minute
        _feed(g, None, 6, t0=i * 2.0)
        fired += len(_feed(g, "FIST", 6, t0=i * 2.0 + 1.0))
    assert fired == 3, f"limit not enforced: {fired} fired"
    # A full minute later it must work again, with no restart and no reset.
    _feed(g, None, 6, t0=200.0)
    assert len(_feed(g, "FIST", 6, t0=201.0)) == 1, \
        "the rate limit wedged -- it never recovered"


def test_reset_clears_the_latch_and_the_ring():
    """The camera closing and reopening is a new session. A gesture latched
    before the close must not suppress the same gesture after it, and the ring
    must not hand a reconnecting client edges from the old session."""
    g = gestures.GestureGate()
    _feed(g, "OPEN", 6, t0=0.0)
    assert g.log.since(0), "nothing was published to begin with"
    g.reset()
    assert g.log.since(0) == [], "old events survived a reset"
    assert len(_feed(g, "OPEN", 6, t0=100.0)) == 1, "still latched after reset"


# -- the event log --------------------------------------------------------
def test_sequence_numbers_are_unique_and_increasing():
    g = gestures.GestureGate(min_gap=0.0)
    seqs = []
    for i in range(5):
        _feed(g, None, 6, t0=i * 2.0)
        seqs += [e.seq for e in _feed(g, "FIST", 6, t0=i * 2.0 + 1.0)]
    assert seqs == sorted(set(seqs)) == seqs and len(seqs) == 5


def test_since_returns_only_newer_events():
    log = gestures.EventLog()
    for i in range(4):
        log.publish(at=0.0, mono=0.0, hand="RIGHT", gesture="FIST",
                    fingers_up=0, bbox=(0, 0, 0, 0), score=1.0)
    assert [e.seq for e in log.since(0)] == [1, 2, 3, 4]
    assert [e.seq for e in log.since(2)] == [3, 4]
    assert log.since(4) == [], "handed back an event the client already had"


def test_age_is_monotonic_derived_not_wall_clock():
    """TRAP 6. This Pi has no battery-backed RTC and is confidently wrong about
    the time for ~34 s after boot. If freshness travelled as a wall-clock
    timestamp, a client comparing it against its own clock would reject every
    event during that window -- and, worse, silently accept stale ones if the
    skew went the other way."""
    log = gestures.EventLog()
    mono = time.monotonic()
    ev = log.publish(at=time.time() + 86400,      # a wildly wrong wall clock
                     mono=mono, hand="RIGHT", gesture="FIST",
                     fingers_up=0, bbox=(0, 0, 0, 0), score=1.0)
    d = ev.as_dict(mono + 2.0)
    assert abs(d["age_s"] - 2.0) < 0.01, \
        f"age came from the wall clock: {d['age_s']}"


def test_wait_returns_events_already_present():
    log = gestures.EventLog()
    log.publish(at=0.0, mono=0.0, hand="R", gesture="FIST", fingers_up=0,
                bbox=(0, 0, 0, 0), score=1.0)
    t0 = time.monotonic()
    assert len(log.wait_for(0, timeout=2.0)) == 1
    assert time.monotonic() - t0 < 0.05, "waited for an event it already had"


def test_wait_blocks_then_delivers():
    log = gestures.EventLog()
    threading.Timer(0.2, lambda: log.publish(
        at=0.0, mono=0.0, hand="R", gesture="FIST", fingers_up=0,
        bbox=(0, 0, 0, 0), score=1.0)).start()
    t0 = time.monotonic()
    got = log.wait_for(0, timeout=2.0)
    assert len(got) == 1 and 0.15 < time.monotonic() - t0 < 1.5


def test_wait_times_out_rather_than_hanging():
    log = gestures.EventLog()
    t0 = time.monotonic()
    assert log.wait_for(0, timeout=0.3) == []
    assert time.monotonic() - t0 < 1.0


def test_the_ring_is_bounded():
    """A replay buffer for a dropped connection, not a history of the room."""
    log = gestures.EventLog(capacity=4)
    for _ in range(10):
        log.publish(at=0.0, mono=0.0, hand="R", gesture="FIST", fingers_up=0,
                    bbox=(0, 0, 0, 0), score=1.0)
    assert [e.seq for e in log.since(0)] == [7, 8, 9, 10]


# -- HTTP -----------------------------------------------------------------
class _Server:
    def __enter__(self):
        self.buf = stream.StreamBuffer()
        self.log = gestures.EventLog()
        self.srv = stream.StreamServer(
            self.buf, lambda: {"state": "test", "started_at": 1234.0},
            "127.0.0.1", 0, "s3cret", self.log)
        self.port = self.srv._httpd.server_address[1]
        self.srv.start()
        self.base = f"http://127.0.0.1:{self.port}"
        return self

    def __exit__(self, *a):
        self.srv.stop()


def _code(url):
    try:
        return urllib.request.urlopen(url, timeout=5).status
    except urllib.error.HTTPError as e:
        return e.code


def _sse(r, want_messages: int, seconds: float = 4.0) -> str:
    """Read until `want_messages` data lines have arrived, or time runs out.

    Deliberately line-oriented. Reading a fixed byte count -- the obvious thing
    -- hangs on this endpoint, because a gesture feed is SILENT almost all the
    time by design and its heartbeat is 10 s apart. A test that blocks for the
    socket timeout on every quiet feed is a test that cannot tell "no events"
    from "broken".
    """
    out, got = [], 0
    deadline = time.time() + seconds
    while got < want_messages and time.time() < deadline:
        try:
            line = r.readline()
        except (TimeoutError, OSError):
            break
        if not line:
            break
        out.append(line.decode())
        if out[-1].startswith("data:"):
            got += 1
    return "".join(out)


def test_events_are_behind_the_token():
    """'What are the people in this room doing with their hands' is not
    meaningfully less private than a picture of them doing it."""
    with _Server() as s:
        assert _code(f"{s.base}/events.json") == 403, "served with NO token"
        assert _code(f"{s.base}/events.json?k=wrong") == 403
        assert _code(f"{s.base}/events?k=wrong") == 403, \
            "the SSE feed bypassed the token"
        assert _code(f"{s.base}/events.json?k=s3cret") == 200


def test_events_json_reports_the_ring():
    with _Server() as s:
        for _ in range(3):
            s.log.publish(at=1.0, mono=time.monotonic(), hand="RIGHT",
                          gesture="PEACE", fingers_up=2,
                          bbox=(0, 0, 1, 1), score=0.9)
        doc = json.loads(urllib.request.urlopen(
            f"{s.base}/events.json?k=s3cret", timeout=5).read())
        assert doc["cursor"] == 3 and len(doc["events"]) == 3
        assert doc["epoch"] == 1234.0, "no way to notice a service restart"
        assert doc["events"][0]["gesture"] == "PEACE"
        since = json.loads(urllib.request.urlopen(
            f"{s.base}/events.json?k=s3cret&since=2", timeout=5).read())
        assert [e["seq"] for e in since["events"]] == [3]


def test_sse_does_not_replay_by_default():
    """A laptop waking up must not act on gestures made while it was asleep."""
    with _Server() as s:
        for _ in range(3):
            s.log.publish(at=1.0, mono=time.monotonic(), hand="RIGHT",
                          gesture="FIST", fingers_up=0, bbox=(0, 0, 1, 1),
                          score=0.9)
        r = urllib.request.urlopen(f"{s.base}/events?k=s3cret", timeout=5)
        try:
            assert "text/event-stream" in r.headers["Content-Type"]
            threading.Timer(0.3, lambda: s.log.publish(
                at=2.0, mono=time.monotonic(), hand="LEFT", gesture="OPEN",
                fingers_up=5, bbox=(0, 0, 1, 1), score=0.9)).start()
            body = _sse(r, 2)          # hello, then the live event
            assert "event: hello" in body, "no hello, so no way to spot a restart"
            assert "FIST" not in body, "replayed history nobody asked for"
            assert "OPEN" in body, "the live event never arrived"
        finally:
            r.close()


def test_sse_replays_only_when_asked():
    with _Server() as s:
        for _ in range(2):
            s.log.publish(at=1.0, mono=time.monotonic(), hand="RIGHT",
                          gesture="ROCK", fingers_up=3, bbox=(0, 0, 1, 1),
                          score=0.9)
        r = urllib.request.urlopen(f"{s.base}/events?k=s3cret&since=0",
                                   timeout=5)
        try:
            body = _sse(r, 3)          # hello, then both replayed events
            assert body.count("ROCK") == 2, "since=0 did not replay the ring"
        finally:
            r.close()


def test_a_subscriber_counts_as_a_viewer():
    """No way to receive gestures from a room without also being counted as
    watching it -- that count is what holds the sensor open and lights the
    panel's CAM indicator."""
    with _Server() as s:
        assert s.buf.viewers == 0
        r = urllib.request.urlopen(f"{s.base}/events?k=s3cret", timeout=5)
        try:
            r.read(40)
            assert s.buf.viewers == 1, "subscribed without being a viewer"
        finally:
            r.close()
        deadline = time.time() + 6
        while s.buf.viewers and time.time() < deadline:
            # Publish so the handler tries to WRITE. A closed peer is only
            # discovered by writing to it -- which is exactly why the endpoint
            # emits a heartbeat comment on an idle feed, and why this test
            # would otherwise have to sit through the full 10 s to pass.
            s.log.publish(at=0.0, mono=time.monotonic(), hand="R",
                          gesture="FIST", fingers_up=0, bbox=(0, 0, 1, 1),
                          score=0.9)
            time.sleep(0.1)
        assert s.buf.viewers == 0, \
            "subscriber never released -- the sensor would stay open"


def test_events_are_404_when_gestures_are_off():
    buf = stream.StreamBuffer()
    srv = stream.StreamServer(buf, lambda: {"started_at": 0.0},
                              "127.0.0.1", 0, "s3cret", None)
    port = srv._httpd.server_address[1]
    srv.start()
    try:
        assert _code(f"http://127.0.0.1:{port}/events?k=s3cret") == 404
        assert _code(f"http://127.0.0.1:{port}/events.json?k=s3cret") == 404
    finally:
        srv.stop()


# -- the Windows client's struct layout -----------------------------------
# Tested HERE, on the Pi, because the bug it encodes shipped and could not be
# caught on the machine that runs it: --dry-run never calls SendInput, so the
# first real keypress was the first time the struct was validated. The types
# are explicit-width rather than ctypes.wintypes precisely so this can run.
import ctypes                                                # noqa: E402

sys.path.insert(0, os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "clients", "windows"))
import hermes_gesture                                        # noqa: E402


def test_input_struct_is_the_size_windows_validates_against():
    """THE REGRESSION. SendInput checks its third argument against
    sizeof(INPUT); anything else is ERROR_INVALID_PARAMETER (87) and nothing
    is pressed. Observed in the field as `SendInput sent 0/2 (error 87)`."""
    want = 40 if ctypes.sizeof(ctypes.c_void_p) == 8 else 28
    assert ctypes.sizeof(hermes_gesture.INPUT) == want


def test_the_union_is_sized_by_mouseinput_not_keybdinput():
    """WHY it was wrong. The union's size comes from its LARGEST member. Every
    abbreviated copy of this snippet declares only `ki` and lands 8 bytes short
    on x64 -- which is exactly what shipped here."""
    assert (ctypes.sizeof(hermes_gesture.MOUSEINPUT)
            > ctypes.sizeof(hermes_gesture.KEYBDINPUT)), \
        "if this ever inverts, the union member that sets the size has changed"
    assert (ctypes.sizeof(hermes_gesture._INPUTUNION)
            == ctypes.sizeof(hermes_gesture.MOUSEINPUT))


def test_ulong_ptr_is_pointer_sized():
    """Declaring dwExtraInfo as DWORD is the other classic version of this
    bug: it misaligns every field after it on x64."""
    assert (ctypes.sizeof(hermes_gesture._ULONG_PTR)
            == ctypes.sizeof(ctypes.c_void_p))


def test_every_bound_key_name_resolves():
    """The key table is the client's security boundary and is deliberately
    fixed. A name in it that does not resolve to a virtual key code would be a
    binding that silently does nothing."""
    assert all(isinstance(v, int) and 0 < v < 0x100
               for v in hermes_gesture.VK.values())
    for name in ("play_pause", "next_track", "volume_mute", "ctrl", "f24"):
        assert name in hermes_gesture.VK
    assert hermes_gesture.EXTENDED <= set(hermes_gesture.VK.values()), \
        "an extended-key code that is not in the table can never be sent"


# -- Hermes naming an action for the laptop ------------------------------
def test_hermes_intents_do_not_inherit_gesture_bindings():
    """TWO SEPARATE GRANTS, and this is the property that keeps them separate.

    Binding PEACE means "a hand in front of my camera may do this". If a
    HERMES intent also matched that bare binding, then asking Hermes to do
    something would silently inherit every gesture ever bound -- granting a
    gesture would be granting the agent, which is not what anyone means by it.
    """
    import sys, os
    sys.path.insert(0, os.path.join(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
        "clients", "windows"))
    import hermes_gesture as hg
    cfg = {"url": "http://x", "bindings": {
        "PEACE": {"type": "key", "keys": "play_pause"},
        "HERMES GMAIL": {"type": "url", "url": "https://mail.google.com"}}}
    d = hg.Dispatcher(cfg, hg.Keyboard(dry_run=True))
    assert d.lookup("RIGHT", "PEACE")[0] == "PEACE", "a hand lost its binding"
    assert d.lookup("HERMES", "PEACE")[0] is None, \
        "Hermes reached a binding meant for a hand"
    assert d.lookup("HERMES", "GMAIL")[0] == "HERMES GMAIL"
    assert d.lookup("HERMES", "ANYTHING")[0] is None


def test_an_intent_is_an_ordinary_event_on_the_same_wire():
    """So every existing protection applies unchanged -- age_s, no replay, the
    viewer requirement and the rate limits are not re-implemented for this."""
    log = gestures.EventLog()
    ev = gestures.publish_intent(log, "gmail")
    d = ev.as_dict()
    assert d["hand"] == "HERMES" and d["gesture"] == "GMAIL"
    assert "age_s" in d and d["seq"] == 1
    assert [e.seq for e in log.since(0)] == [1]


def test_intent_names_are_a_closed_shape():
    """A name that could carry punctuation or whitespace would be somewhere to
    smuggle something into whatever the laptop does with it."""
    log = gestures.EventLog()
    ev = gestures.publish_intent(log, "  open_gmail  ")
    assert ev.gesture == "OPEN_GMAIL", ev.gesture
    assert len(gestures.publish_intent(log, "x" * 200).gesture) <= 32


def test_an_app_action_cannot_name_an_arbitrary_uri():
    """THE CONFIG PICKS A KEY; THIS FILE OWNS THE VALUE.

    'url' refuses anything that is not http(s) precisely because a protocol
    handler is a much larger thing to hand to a room -- handlers come from
    whatever is installed, some take arguments, and file: reaches the disk.
    Launching Spotify needs exactly one such scheme, so it is resolved from a
    table in the client rather than written in the config. If a config could
    supply the URI, adding 'app' would have quietly repealed the url rule.
    """
    import hermes_gesture as hg
    ok = hg.Dispatcher({"url": "http://x", "bindings": {
        "HERMES SPOTIFY": {"type": "app", "app": "spotify"}}},
        hg.Keyboard(dry_run=True))
    assert ok.lookup("HERMES", "SPOTIFY")[0] == "HERMES SPOTIFY"

    for bad in ("file", "ms-settings", "spotify:", "", "SPOTIFY x"):
        try:
            hg.Dispatcher({"url": "http://x", "bindings": {
                "HERMES X": {"type": "app", "app": bad}}},
                hg.Keyboard(dry_run=True))
        except ValueError:
            continue
        raise AssertionError(f"app {bad!r} was accepted; the table is not closed")


def test_a_spotify_uri_is_pinned_to_a_content_id():
    """THE ONE PLACE A CONFIG SUPPLIES A URI, AND WHY IT IS SAFE TO.

    Playing a named album needs an id, so this action cannot use the closed
    APPS table. It is allowed only because the shape is pinned until there is
    nothing to smuggle: fixed scheme, fixed kinds, base62 id of exactly 22
    characters, and no query, path or trailing text. "Play this Spotify id"
    does not generalise to "open this URI" -- that stays refused.
    """
    import hermes_gesture as hg
    kb = hg.Keyboard(dry_run=True)
    good = "spotify:album:4aawyAB9vmqN3uQ7FjRGTy"
    d = hg.Dispatcher({"url": "http://x", "bindings": {
        "HERMES RUMOURS": {"type": "spotify", "uri": good}}}, kb)
    assert d.lookup("HERMES", "RUMOURS")[1]["uri"] == good

    for bad in ("spotify:album:short",                  # wrong length
                "spotify:evil:4aawyAB9vmqN3uQ7FjRGTy",  # kind not in the set
                good + "?autoplay=1",                   # query appended
                good + " && calc",                      # trailing anything
                "spotify:album:4aawyAB9vmqN3uQ7FjRGT/",  # non-base62
                "file:///C:/Windows", "spotify:", ""):
        try:
            hg.Dispatcher({"url": "http://x", "bindings": {
                "HERMES X": {"type": "spotify", "uri": bad}}}, kb)
        except ValueError:
            continue
        raise AssertionError(f"spotify uri {bad!r} was accepted")


def test_the_url_action_still_refuses_protocol_handlers():
    """Regression guard on the boundary the 'app' table exists to preserve."""
    import hermes_gesture as hg
    for bad in ("spotify:", "file:///C:/Windows", "ms-settings:", "javascript:0"):
        try:
            hg.Dispatcher({"url": "http://x", "bindings": {
                "HERMES X": {"type": "url", "url": bad}}},
                hg.Keyboard(dry_run=True))
        except ValueError:
            continue
        raise AssertionError(f"url {bad!r} was accepted")


def _run() -> int:
    fails = 0
    for name, fn in sorted(globals().items()):
        if not name.startswith("test_") or not callable(fn):
            continue
        try:
            fn()
            print(f"  PASS  {name}")
        except Exception as e:
            fails += 1
            print(f"  FAIL  {name}: {type(e).__name__}: {e}")
    print(f"\n{'ALL PASS' if not fails else f'{fails} FAILURES'}")
    return fails


if __name__ == "__main__":
    sys.exit(1 if _run() else 0)
