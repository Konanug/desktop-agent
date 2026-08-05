"""The camera must never let the model believe it saw something it did not.

Companion to tests/test_states.py, which pins the panel's version of the same
rule. There the failure was a stale state file claiming health; here it is a
stale frame presented as live, or an error the model reads as "look again".

These run without a camera, without the service, and without Hermes: every
case fakes the tmpfs directory the plugin reads. That matters because the
honesty rules have to be verifiable on a laptop, not only on the Pi.

Run:  python3 tests/test_camera_tools.py
"""

import json
import os
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "hermes_ext", "plugins"))

_TMP = tempfile.mkdtemp(prefix="hermes-cam-test-")
os.environ["XDG_RUNTIME_DIR"] = _TMP

from hermes_camera import tools  # noqa: E402

CAM = Path(_TMP) / "hermes-camera"


def _reset(status: dict | None = None) -> None:
    """Fresh tmpfs state. status=None means 'service not running'."""
    for sub in ("requests", "captures"):
        d = CAM / sub
        d.mkdir(parents=True, exist_ok=True)
        for f in d.iterdir():
            f.unlink()
    (CAM / "DISABLED").unlink(missing_ok=True)
    st = CAM / "status.json"
    if status is None:
        st.unlink(missing_ok=True)
    else:
        st.write_text(json.dumps(status))
    tools._seen_this_turn.clear()


def _healthy_status() -> dict:
    return {"schema": 1, "updated_at": time.time(), "state": "awake",
            "muted": None, "sensor_powered": True, "motion": 0.0}


def _serve(capture: dict, jpeg: bytes = b"\xff\xd8ffake") -> None:
    """Answer whatever request is pending, as the service would."""
    reqs = sorted((CAM / "requests").glob("*.json"))
    assert reqs, "plugin did not post a request"
    rid = json.loads(reqs[0].read_text())["id"]
    reqs[0].unlink()
    if "error" not in capture:
        (CAM / "captures" / f"{rid}.jpg").write_bytes(jpeg)
    capture.setdefault("id", rid)
    (CAM / "captures" / f"{rid}.json").write_text(json.dumps(capture))


def _look_with(capture: dict, **kwargs):
    """Run camera_look against a pre-staged reply, without a real service."""
    import threading
    out = {}

    def run():
        out["r"] = tools.camera_look({"reason": "test"}, **kwargs)

    t = threading.Thread(target=run)
    t.start()
    for _ in range(300):                      # wait for the request to appear
        if any((CAM / "requests").glob("*.json")):
            break
        time.sleep(0.01)
    _serve(capture)
    t.join(timeout=10)
    return out.get("r")


def _fresh(**over) -> dict:
    c = {"captured_at": time.time(), "w": 768, "h": 432,
         "bytes": 6, "profile": "normal", "kind": "look"}
    c.update(over)
    return c


# -- the core rule ------------------------------------------------------

def test_stale_frame_is_refused_not_shown():
    """A frame older than the freshness limit must NEVER reach the model."""
    _reset(_healthy_status())
    r = _look_with(_fresh(captured_at=time.time() - 30.0))
    assert isinstance(r, str), "a stale frame was returned as an image"
    assert "NOT seen" in r or "not being shown" in r


def test_unknown_age_is_refused():
    """Trap 6: no RTC, so the clock is wrong for ~34s after boot. A nonsense
    age must be refused rather than printed as a number."""
    _reset(_healthy_status())
    for bogus in (time.time() + 500, 0):
        r = _look_with(_fresh(captured_at=bogus))
        assert isinstance(r, str), f"captured_at={bogus} was accepted"
        assert "NOT seen" in r or "cannot be trusted" in r


def test_service_down_says_you_have_not_seen_anything():
    """A bare error invites the model to answer from priors. It must be told."""
    _reset(None)
    r = tools.camera_look({"reason": "test"})
    assert isinstance(r, str)
    assert "NOT seen" in r
    assert "Do not describe the room" in r


def test_capture_error_is_reported_as_no_image():
    _reset(_healthy_status())
    r = _look_with({"error": "sensor busy"})
    assert isinstance(r, str)
    assert "NOT seen" in r


# -- guards -------------------------------------------------------------

def test_fresh_frame_returns_the_multimodal_envelope():
    _reset(_healthy_status())
    r = _look_with(_fresh(), task_id="T", user_task="what is this?")
    assert isinstance(r, dict), f"expected an envelope, got {r!r}"
    # Exactly the predicate Hermes applies in _normalize_handler_result.
    assert r.get("_multimodal") is True and isinstance(r.get("content"), list)
    assert r["content"][0]["type"] == "text"
    assert r["content"][1]["image_url"]["url"].startswith("data:image/jpeg;base64,")
    assert "text_summary" in r
    assert "what is this?" in r["content"][0]["text"]


def test_per_turn_cap_stops_a_look_loop():
    """Images are permanent in history; a model that keeps looking is the
    expensive failure, not one large picture."""
    _reset(_healthy_status())
    for i in range(tools.MAX_FRAMES_PER_TURN):
        assert isinstance(_look_with(_fresh(), task_id="LOOP"), dict), f"call {i}"
    # The over-cap call must refuse WITHOUT even asking the service, so it is
    # called directly rather than through the staged-reply helper.
    r = tools.camera_look({"reason": "test"}, task_id="LOOP")
    assert isinstance(r, str) and "already captured" in r
    assert not any((CAM / "requests").glob("*.json")), \
        "a refused call still woke the camera"


def test_disable_file_blocks_and_hides():
    _reset(_healthy_status())
    (CAM / "DISABLED").write_text("")
    assert tools.camera_available() is False
    r = tools.camera_look({"reason": "test"})
    assert isinstance(r, str) and "NOT seen" in r


def test_wedged_service_is_treated_as_down():
    """A heartbeat that stopped means no frame, even though status.json exists."""
    _reset({**_healthy_status(), "updated_at": time.time() - 600})
    assert tools.camera_available() is False
    r = tools.camera_look({"reason": "test"})
    assert isinstance(r, str) and "NOT seen" in r


def test_reason_is_sanitised():
    """It reaches the journal and the panel, so it is not free-form."""
    # isprintable() drops NUL and ESC; the bracket text is left as inert
    # characters, which is fine -- it is never interpreted, only logged.
    assert tools._sanitise("a\x00b\x1b[31mc") == "ab[31mc"
    assert len(tools._sanitise("x" * 500)) <= tools.MAX_REASON
    assert tools._sanitise("") == "unspecified"
    assert tools._sanitise(None) == "unspecified"


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
    try:
        sys.exit(1 if _run() else 0)
    finally:
        import shutil
        shutil.rmtree(_TMP, ignore_errors=True)
