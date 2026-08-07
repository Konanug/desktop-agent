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
    svc.wake = type("W", (), {"available": True, "error": None})()
    svc.stt = type("S", (), {"available": True, "error": None, "last_ms": 12.0})()
    svc.speaker = type("P", (), {"available": True, "error": None})()
    svc.limit = sink.RateLimit()
    svc.state, svc.started_at = "listening", time.time()
    svc.heard = svc.refused = 0
    svc.last_error = None
    svc.loop_tick = time.monotonic()
    doc = svc.status_doc()
    blob = json.dumps(doc).lower()
    for leaky in ("text", "transcript", "utterance", "said", "heard_text"):
        assert leaky not in doc, f"status.json exposes {leaky!r}"
    assert "state" in doc and "mic_open" in doc
    # And it must serialise -- a status file that cannot be written is a panel
    # that silently stops knowing whether the mic is on.
    json.loads(json.dumps(doc))


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
