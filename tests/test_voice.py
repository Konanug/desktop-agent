"""A microphone in a room is the most sensitive thing this project handles.

What is pinned here is not "does speech recognition work" -- that needs a voice
and a room, and no test can stand in for it. It is the parts that are silent
when they go wrong:

  * A RATE LIMIT THAT WEDGES. Trap 19: the camera's per-turn counter only went
    up, so it refused permanently and said its limit was spent. Anything gating
    an action here is a sliding window and must be shown to recover.

  * A ZERO USED AS "NEVER". Trap 28: time.monotonic()'s origin is undefined, so
    `_last = 0.0` reads as "just fired" on a machine whose clock starts near
    zero and swallows the first utterance of every session.

  * A TRANSCRIPT LEAKING INTO STATE. status.json is read by the panel and is
    not private. Everything else in this project keeps state and content apart;
    the one file describing a microphone must not be where that slips.

  * A REPLY SPOKEN ON A LOOP. speak.txt is an EDGE, not a level. Left in place
    it would be re-read and recited every tick -- the same mistake gestures
    made once already.

  * THE MIC LIGHT FAILING OFF. Unknown must mean ON, exactly as for the camera.

No microphone, no models and no network required.

Run:  python3 tests/test_voice.py
"""

import hashlib
import hmac
import json
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from voice import protocol, sink                             # noqa: E402


# -- rate limiting --------------------------------------------------------
def test_the_first_utterance_of_a_session_is_not_swallowed():
    """TRAP 28. `_last = 0.0` would read as 'fired just now' on any machine
    whose monotonic clock starts near zero -- and under test it always does."""
    rl = sink.RateLimit()
    assert rl.check(mono=0.0) is None, "the very first utterance was refused"


def test_min_gap_is_enforced():
    rl = sink.RateLimit(min_gap=3.0)
    assert rl.check(mono=100.0) is None
    rl.record(mono=100.0)
    assert rl.check(mono=101.0) is not None, "no minimum gap"
    assert rl.check(mono=104.0) is None, "gap never lifted"


def test_per_minute_cap_slides_and_recovers():
    """TRAP 19 in a new place: it must recover on its own, with no restart."""
    rl = sink.RateLimit(min_gap=0.0, per_min=3, per_hour=100)
    t = 0.0
    for _ in range(3):
        assert rl.check(mono=t) is None
        rl.record(mono=t); t += 1.0
    assert rl.check(mono=t) is not None, "cap not enforced"
    # A minute after the first, the window has slid past it.
    assert rl.check(mono=70.0) is None, "the per-minute limit WEDGED"


def test_per_hour_cap_slides_and_recovers():
    """A television talking to itself all evening must hit a ceiling -- and
    the ceiling must not be permanent."""
    rl = sink.RateLimit(min_gap=0.0, per_min=1000, per_hour=5)
    for i in range(5):
        assert rl.check(mono=float(i)) is None
        rl.record(mono=float(i))
    assert rl.check(mono=10.0) is not None, "hourly cap not enforced"
    assert rl.check(mono=3700.0) is None, "the hourly limit WEDGED"


def test_a_refusal_explains_which_limit():
    rl = sink.RateLimit(min_gap=5.0)
    rl.record(mono=0.0)
    why = rl.check(mono=1.0)
    assert why and "gap" in why, f"unhelpful refusal reason: {why!r}"


# -- the wire -------------------------------------------------------------
def test_signature_matches_what_the_gateway_verifies():
    """The gateway computes HMAC-SHA256 over "<timestamp>.<body>". Getting the
    joining byte or the order wrong yields a 401 that looks like a wrong
    secret, which sends the diagnosis somewhere else entirely."""
    secret, ts = "s3cret", "1700000000"
    body = json.dumps({"type": "voice", "text": "hello"}).encode()
    expected = hmac.new(secret.encode(), ts.encode() + b"." + body,
                        hashlib.sha256).hexdigest()
    assert len(expected) == 64
    # Order matters: body-then-timestamp is a DIFFERENT and wrong signature.
    wrong = hmac.new(secret.encode(), body + b"." + ts.encode(),
                     hashlib.sha256).hexdigest()
    assert expected != wrong


def test_post_refuses_without_a_secret_rather_than_sending_unsigned():
    ok, detail = sink.post("hello", url="http://127.0.0.1:1/nope", secret="")
    assert not ok and "secret" in detail.lower()


def test_delivery_failure_is_returned_not_raised():
    """A gateway restart is a normal event. It must not take down a service
    whose entire job is to keep listening."""
    ok, detail = sink.post("hello", url="http://127.0.0.1:1/refused",
                           secret="x", timeout=1.0)
    assert ok is False and isinstance(detail, str) and detail


# -- state, never content -------------------------------------------------
def test_status_never_carries_a_transcript():
    """The panel reads this file. What was SAID must not be in it."""
    from voice.__main__ import Service
    svc = Service.__new__(Service)
    svc.rec = type("R", (), {"proc": None})()
    svc.wake = type("W", (), {"available": True, "error": None, "score": 0.0})()
    svc.stt = type("S", (), {"available": True, "error": None, "last_ms": 12.0})()
    svc.speaker = type("P", (), {"available": True, "error": None})()
    svc.limit = sink.RateLimit()
    svc.state, svc.started_at = "listening", time.time()
    svc.heard = svc.refused = 0
    svc.last_error = None
    svc.loop_tick = time.monotonic()
    svc.level = svc.level_peak = svc.wake_peak = 0.0
    svc.ends = None
    from voice import listen
    svc.floor = listen.NoiseFloor()
    svc.capped = 0
    svc.capture_started = 0.0
    doc = svc.status_doc()
    blob = json.dumps(doc).lower()
    for leaky in ("text", "transcript", "utterance", "said", "heard_text"):
        assert leaky not in doc, f"status.json exposes {leaky!r}"
    assert "state" in doc and "mic_open" in doc
    # And it must serialise -- a status file that cannot be written is a panel
    # that silently stops knowing whether the mic is on.
    json.loads(json.dumps(doc))


def test_status_survives_numpy_scalars():
    """THE REGRESSION. onnxruntime returns numpy float32 wake scores, which
    survive round() and then kill json.dumps in the status writer -- a crash
    landing nowhere near the code that produced the value. The camera learned
    this already (tests/test_hands.py); this is the same rule, arrived at the
    hard way a second time."""
    import numpy as np
    from voice.__main__ import Service
    svc = Service.__new__(Service)
    svc.rec = type("R", (), {"proc": None})()
    svc.wake = type("W", (), {"available": True, "error": None,
                              "score": np.float32(0.4242)})()
    svc.stt = type("S", (), {"available": True, "error": None,
                             "last_ms": np.float32(12.0)})()
    svc.speaker = type("P", (), {"available": True, "error": None})()
    svc.limit = sink.RateLimit()
    svc.state, svc.started_at = "listening", time.time()
    svc.heard = svc.refused = 0
    svc.last_error = None
    svc.loop_tick = time.monotonic()
    svc.level = np.float32(3.5); svc.level_peak = np.float32(9.0)
    svc.wake_peak = np.float32(0.1); svc.ends = None
    from voice import listen
    svc.floor = listen.NoiseFloor()
    for _ in range(8):
        svc.floor.push((np.random.default_rng(2).standard_normal(1280) * 50).astype(np.int16))
    svc.capped = 0
    svc.capture_started = 0.0
    json.loads(json.dumps(svc.status_doc()))       # must not raise


# -- speaking -------------------------------------------------------------
def test_pending_speech_is_consumed_exactly_once():
    """An EDGE, not a level. Left in place the reply would be recited every
    tick, which is precisely the mistake gestures made with hands.json."""
    from voice import speak
    protocol.ensure_dirs()
    protocol.speak_path().write_text("hello there")
    assert speak.take_pending() == "hello there"
    assert speak.take_pending() is None, "the same reply would be said twice"


def test_missing_speech_file_is_not_an_error():
    from voice import speak
    protocol.speak_path().unlink(missing_ok=True)
    assert speak.take_pending() is None


def test_speak_tool_sanitises_and_caps():
    """TRUST BOUNDARY. The model picks the text and it crosses a file into
    another process's subprocess pipeline."""
    sys.path.insert(0, os.path.join(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
        "hermes_ext", "plugins"))
    from hermes_voice.tools import _sanitise, MAX_CHARS
    assert _sanitise("a\x00b\x07c") == "abc", "control characters survived"
    assert _sanitise("one\n\n  two\t three") == "one two three"
    assert len(_sanitise("x" * (MAX_CHARS + 500))) == MAX_CHARS
    assert _sanitise("   ") == "" and _sanitise(None) == ""


# -- the panel light ------------------------------------------------------
def test_unknown_mic_state_fails_toward_ON():
    """Same rule as the camera. Never tell someone the microphone is off
    unless that was positively observed -- and unlike the camera there is no
    kernel fact to fall back on, so this trusts a file that may be missing."""
    from display.health import HealthProbe
    p = HealthProbe()
    p._mic_file = "/nonexistent/hermes-voice/status.json"
    on, busy = p._mic()
    assert on is not True
    # Installed-but-unreadable must be UNKNOWN (None), never False.
    import display.health as H
    real = H._voice_unit_present
    try:
        H._voice_unit_present = lambda: True
        assert p._mic()[0] is None, "an unreadable status file claimed MIC OFF"
        H._voice_unit_present = lambda: False
        assert p._mic()[0] is False, "no service at all should read as off"
    finally:
        H._voice_unit_present = real


def test_mic_busy_only_while_capturing():
    from display.health import HealthProbe
    import tempfile
    p = HealthProbe()
    with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False) as f:
        path = f.name
    p._mic_file = path
    for state, expect_busy in (("listening", False), ("capturing", True),
                               ("thinking", True), ("speaking", False)):
        open(path, "w").write(json.dumps({"mic_open": True, "state": state}))
        on, busy = p._mic()
        assert on is True and busy is expect_busy, f"{state} -> busy={busy}"
    os.unlink(path)


# -- the mic must not stay on -------------------------------------------
# Reported as "sometimes it was left on way after I intended". It was, and for
# two independent reasons. These are the pins.
class _FakeEnds:
    """Speech is whatever is loud; silence is whatever is not."""
    def __init__(self, threshold=100.0):
        self.threshold = threshold
        self.floor = threshold / 3.0
    def set_floor(self, f):
        self.floor = f
        self.threshold = max(f * 3.0, 120.0)
    def is_speech(self, frame):
        import numpy as np
        return float(np.sqrt(np.mean(frame.astype(np.float32) ** 2))) > self.threshold


def _svc_for_capture(frames):
    """A Service wired to read a fixed list of frames, then block forever."""
    import numpy as np
    from voice.__main__ import Service
    from voice import listen
    svc = Service.__new__(Service)
    seq = list(frames)

    class R:
        def read_frame(self):
            return seq.pop(0) if seq else np.zeros(1280, np.int16)
    svc.rec = R()
    svc.ends = _FakeEnds()
    svc.floor = listen.NoiseFloor()
    svc.pre = listen.PreRoll()
    svc.capped = 0
    svc.capture_started = 0.0
    return svc


def _loud(n):
    import numpy as np
    rng = np.random.default_rng(0)
    return [(rng.standard_normal(1280) * 3000).astype(np.int16) for _ in range(n)]


def _quiet(n):
    import numpy as np
    return [np.zeros(1280, np.int16) for _ in range(n)]


def test_a_false_wake_with_no_speech_ends_fast():
    """THE REGRESSION THAT PROMPTED THIS.

    The original loop only broke on silence once speech had ALREADY been heard
    (`quiet_for >= SILENCE_END and spoke_for > 0`), so a wake word firing on a
    television with nobody in the room could not end early and recorded until
    the ceiling. The cheapest case was accidentally the most expensive.
    """
    svc = _svc_for_capture(_quiet(400))
    t0 = time.monotonic()
    out = svc.capture_utterance()
    frames_read = 400 - 0
    assert out is None, "silence should not be treated as an utterance"
    # It must give up around LEAD_SILENCE, nowhere near MAX_UTTERANCE.
    assert svc.capped == 0, "a silent false wake ran to the ceiling"


def test_speech_then_silence_ends_on_the_silence():
    svc = _svc_for_capture(_loud(25) + _quiet(30) + _loud(200))
    out = svc.capture_utterance()
    assert out is not None, "real speech was discarded"
    # 25 loud + ~10 quiet frames of trailing silence, not the 200 that follow.
    assert len(out) / protocol.RATE < 4.0, \
        f"kept recording past the silence: {len(out)/protocol.RATE:.1f}s"
    assert svc.capped == 0


def test_continuous_noise_still_hits_a_ceiling_and_says_so():
    """If the room never goes quiet the capture must still END -- and the
    ceiling being hit is worth reporting, because it means the threshold is
    wrong for the room rather than that the person talked for a long time."""
    svc = _svc_for_capture(_loud(1000))
    out = svc.capture_utterance()
    assert out is not None
    assert len(out) / protocol.RATE <= protocol.MAX_UTTERANCE + 0.5
    assert svc.capped == 1, "hitting the ceiling was not recorded"


def test_the_threshold_follows_the_room_not_the_startup_moment():
    """The other half of the bug: measured once at startup, the threshold went
    stale the first time the room got louder, and every capture then ran to
    the ceiling because nothing ever read as silence."""
    from voice import listen
    import numpy as np
    e = listen.Endpointer(floor=1.0)
    quiet_threshold = e.threshold
    nf = listen.NoiseFloor()
    rng = np.random.default_rng(1)
    for _ in range(80):                       # the room got loud
        nf.push((rng.standard_normal(1280) * 800).astype(np.int16))
    e.set_floor(nf.value())
    assert e.threshold > quiet_threshold * 2, \
        f"threshold did not follow the room: {quiet_threshold} -> {e.threshold}"


def test_capture_length_is_published_while_recording():
    """So 'how long has the mic actually been recording' is answerable from
    outside, which is the whole reason the question came up."""
    svc = _svc_for_capture(_quiet(10))
    assert svc.capture_started == 0.0
    svc.capture_utterance()
    assert svc.capture_started == 0.0, "capture timer not cleared afterwards"


# -- one wake, one utterance --------------------------------------------
def test_a_turn_ends_by_discarding_what_was_buffered_while_busy():
    """THE POINT OF end_turn().

    Between the end of a capture and the return to listening the service is
    transcribing, posting, and possibly speaking -- and is NOT calling
    read_frame() through any of it, so ALSA buffers the lot. Without draining,
    the next wake's pre-roll begins with whatever was said while Hermes was
    answering, including its own reply out of the speaker, presented as though
    it had just been spoken.
    """
    from voice.__main__ import Service
    from voice import listen
    import numpy as np

    drained = {"n": 0}
    svc = Service.__new__(Service)
    svc.rec = type("R", (), {"drain": lambda self, limit=30.0: (
        drained.__setitem__("n", drained["n"] + 1) or 3.2)})()
    svc.wake = type("W", (), {"reset": lambda self: None, "score": 0.0,
                              "available": True, "error": None})()
    svc.pre = listen.PreRoll()
    svc.pre.push(np.zeros(1280, np.int16))
    svc.floor = listen.NoiseFloor()
    svc.state = "thinking"
    svc.publish = lambda force=False: None

    svc.end_turn()
    assert drained["n"] == 1, "buffered audio was not discarded"
    assert len(svc.pre.frames) == 0, "stale pre-roll carried into the next turn"
    assert svc.state == "listening", "did not return to needing the wake word"


def test_every_exit_from_a_turn_closes_it():
    """A path that skips end_turn() leaves the mic unattended-but-buffering and
    the detector primed. There are four ways a turn can end -- nothing
    captured, empty transcript, rate limited, delivered -- plus finishing
    speaking, and all of them must go through the same door."""
    import inspect
    from voice import __main__ as m
    src = inspect.getsource(m)
    # No path may set state back to listening by hand; that is end_turn's job,
    # so its own body is excluded -- everything AFTER it must delegate.
    body = src.split("def handle_wake", 1)[1].split("def start", 1)[0]
    stray = [ln.strip() for ln in body.splitlines()
             if 'self.state = "listening"' in ln]
    assert not stray, f"a turn exit bypasses end_turn(): {stray}"
    assert src.count("self.end_turn()") >= 4, "not every exit closes the turn"


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
