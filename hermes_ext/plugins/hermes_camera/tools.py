"""Ask the camera service for a frame and hand the pixels to the model.

TRUST AND HONESTY BOUNDARY
This module is the camera's equivalent of hermes_display/tools.py, but the risk
runs the other way. There, untrusted bytes were coming IN and had to be stopped
before they reached the framebuffer. Here, real pixels of the owner's room are
going OUT to a model, and the danger is subtler: the model answering as though
it saw something when it did not.

So the rules are:

  * NEVER return a stale frame. If a fresh one cannot be had, return a text
    error that says plainly that nothing was seen. A frame older than
    MAX_FRAME_AGE is not "a bit old", it is a different moment.
  * ALWAYS state the frame's age in the text that accompanies the image.
  * If the age cannot be trusted, say so instead of printing a number. The Pi
    has no battery-backed RTC and its wall clock is confidently wrong for ~34 s
    after boot, so a computed age can be nonsense through no fault of ours.
  * The model never chooses a path, a filename or a URL -- same discipline as
    the display plugin. It chooses only a reason and a detail level.

WHY THE PATHS ARE DUPLICATED FROM camera/protocol.py
This code runs inside the Hermes process, which must not import from this
repo's packages -- Hermes is installed separately and its import path is not
ours to extend. The constants below are a deliberate copy of the contract in
camera/protocol.py; docs/CAMERA-CONTRACT.md is the authority if they ever
disagree.
"""

from __future__ import annotations

import base64
import json
import os
import time
import uuid
from pathlib import Path

# --- contract, mirrored from camera/protocol.py -------------------------
MAX_FRAME_AGE = 2.0
CAPTURE_TIMEOUT = 8.0          # generous: cold wake measured ~0.7 s
POLL = 0.01
PROFILES = ("normal", "fine")
# A RATE limit, not a quota. Sized against measured frame cost (~17 KB of
# base64 each, so six is ~102 KB) but the important property is the window:
# it slides, so the allowance always comes back on its own.
#
# Hermes imposes NO image cap of its own -- multimodal tool results are exempt
# from its 100k-per-result and 200k-per-turn character budgets. This is the
# only limit in the system, which is exactly why it must not be able to wedge.
MAX_FRAMES_PER_WINDOW = 6
CAPTURE_WINDOW = 90.0          # seconds; the window slides, so it self-heals
MAX_REASON = 48

# A frame this far out cannot be a real age -- it is a clock problem.
IMPLAUSIBLE_AGE = 60.0

# key -> capture timestamps inside the window
_recent: dict[str, list[float]] = {}


def _runtime_dir() -> Path:
    base = os.environ.get("XDG_RUNTIME_DIR") or f"/run/user/{os.getuid()}"
    return Path(base) / "hermes-camera"


def _sanitise(text, limit: int = MAX_REASON) -> str:
    out = "".join(c for c in str(text or "") if c.isprintable())
    return out[:limit].strip() or "unspecified"


def _status() -> dict:
    try:
        return json.loads((_runtime_dir() / "status.json").read_text())
    except Exception:
        return {}


def camera_available() -> bool:
    """check_fn: is the tool worth offering at all?

    TTL-cached 30 s by the registry, so it is a tidiness measure, not a
    control. The handler re-checks at call time -- otherwise flipping the kill
    switch would leave the tool live for up to half a minute.
    """
    if (Path.home() / ".config" / "hermes-pi" / "camera.disabled").exists():
        return False
    if (_runtime_dir() / "DISABLED").exists():
        return False
    st = _status()
    if not st or st.get("muted"):
        return False
    # A heartbeat older than this means the service is gone or wedged.
    return (time.time() - float(st.get("updated_at") or 0)) < 15.0


def _blocked_reason() -> str | None:
    if (Path.home() / ".config" / "hermes-pi" / "camera.disabled").exists():
        return ("The camera is disabled by the owner "
                "(~/.config/hermes-pi/camera.disabled). You have NOT seen "
                "anything. Do not describe the room.")
    if (_runtime_dir() / "DISABLED").exists():
        return ("The camera is muted. You have NOT seen anything. "
                "Do not describe the room.")
    st = _status()
    if not st:
        return ("The camera service is not running, so no frame could be "
                "taken. You have NOT seen anything. Do not describe the room.")
    age = time.time() - float(st.get("updated_at") or 0)
    if age > 15.0:
        return (f"The camera service last reported {age:.0f}s ago and appears "
                f"wedged. You have NOT seen anything. Do not describe the room.")
    if st.get("muted"):
        return (f"The camera is unavailable: {st['muted']}. You have NOT seen "
                f"anything. Do not describe the room.")
    return None


def _request(mode: str, profile: str, reason: str) -> dict:
    """Post a request and wait for the capture. Returns metadata or {'error':}."""
    d = _runtime_dir()
    rid = uuid.uuid4().hex[:12]
    req = {"id": rid, "mode": mode, "profile": profile,
           "reason": reason, "created_at": time.time()}
    try:
        (d / "requests").mkdir(mode=0o700, parents=True, exist_ok=True)
        tmp = d / "requests" / f"{rid}.json.tmp"
        tmp.write_text(json.dumps(req))
        os.replace(tmp, d / "requests" / f"{rid}.json")
    except OSError as e:
        return {"error": f"cannot reach the camera service: {e}"}

    meta_path = d / "captures" / f"{rid}.json"
    jpg_path = d / "captures" / f"{rid}.jpg"
    deadline = time.time() + CAPTURE_TIMEOUT
    while time.time() < deadline:
        # The metadata file is written LAST, so its existence proves the image
        # beside it is complete. Never read the jpg on its own.
        if meta_path.exists():
            try:
                meta = json.loads(meta_path.read_text())
            except Exception:
                time.sleep(POLL)
                continue
            if meta.get("error"):
                return {"error": meta["error"]}
            try:
                meta["_bytes"] = jpg_path.read_bytes()
            except OSError as e:
                return {"error": f"capture file unreadable: {e}"}
            return meta
        time.sleep(POLL)
    return {"error": f"the camera did not respond within {CAPTURE_TIMEOUT:.0f}s"}


def _age_of(meta: dict) -> tuple[float | None, str]:
    """(age_seconds, human) or (None, why-it-cannot-be-trusted)."""
    captured = float(meta.get("captured_at") or 0)
    if captured <= 0:
        return None, "unknown (no capture timestamp)"
    age = time.time() - captured
    # Trap 6: the clock is confidently wrong for ~34 s after boot. A negative
    # or absurd age means the clock moved, not that the frame is old. Refusing
    # to print a number is more honest than printing a wrong one.
    if age < -1.0 or age > IMPLAUSIBLE_AGE:
        return None, "unknown (system clock may not be synced)"
    return max(0.0, age), f"{max(0.0, age):.2f}s"


def _envelope(meta: dict, header: str) -> dict:
    """The one structured shape Hermes accepts besides a plain string.

    tools/registry.py:_normalize_handler_result takes exactly `str` or this;
    anything else is replaced with an error, so every failure path in this
    module returns a string.
    """
    b64 = base64.b64encode(meta["_bytes"]).decode()
    return {
        "_multimodal": True,
        "content": [
            {"type": "text", "text": header},
            {"type": "image_url",
             "image_url": {"url": f"data:image/jpeg;base64,{b64}"}},
        ],
        "text_summary": (f"Camera frame {meta.get('w')}x{meta.get('h')}, "
                         f"{len(meta['_bytes'])} bytes. Answer from what you see."),
        "meta": {k: v for k, v in meta.items() if k != "_bytes"},
    }


PREVIEW_SECONDS = 8.0
PREVIEW_W, PREVIEW_H = 480, 264      # display body zone; part of that contract


def _show_on_panel() -> None:
    """Put the frame that was just sent onto the Pi's own screen.

    The strongest privacy affordance available here. A tally light says "the
    camera was used"; this says "and here is exactly what left the room". The
    camera service has already written the RGB565 into the display's spool, so
    all that is needed is a request pointing at it.

    Deliberately best-effort and silent on failure: the panel is a courtesy to
    whoever is in the room, and it must never be able to fail a capture the
    user actually asked for.

    NOTE this makes request.json have two writers -- this plugin and
    hermes_display. Both are in-process in the gateway and both write whole
    documents via atomic replace, so the last writer simply wins, which is the
    correct behaviour for "what should the panel show right now".
    """
    try:
        base = os.environ.get("XDG_RUNTIME_DIR") or f"/run/user/{os.getuid()}"
        d = Path(base) / "hermes-display"
        if not (d / "images" / "camera-preview.rgb565").exists():
            return
        now = time.time()
        payload = {"schema": 1, "mode": "image", "image": "camera-preview.rgb565",
                   "text": None, "requested_at": now,
                   "expires_at": now + PREVIEW_SECONDS,
                   "w": PREVIEW_W, "h": PREVIEW_H}
        tmp = d / f".request.json.{os.getpid()}.tmp"
        tmp.write_text(json.dumps(payload), encoding="utf-8")
        os.replace(tmp, d / "request.json")
    except Exception:
        pass


def _turn_key(kwargs: dict) -> str:
    return str(kwargs.get("task_id") or kwargs.get("session_id") or "-")


def _count_recent(kwargs: dict) -> int:
    """Captures by this caller inside the recent window, including this one.

    A SLIDING WINDOW, not a per-turn tally, and the distinction is the whole
    point. The first version counted per "turn" using `task_id` -- but Hermes
    documents task_id as an identifier for *session* isolation, not for a turn.
    It is stable for the whole Discord conversation, so the counter never reset
    and the tool refused permanently after N captures. The agent reported its
    "capture limit is exhausted" and could not look again until the gateway was
    restarted, which is a wedged tool, not a guard.

    A time window cannot wedge. A model looping burns the allowance in seconds
    and is stopped; a person asking again a minute later is simply not the case
    this exists to catch, and gets a fresh allowance automatically.
    """
    key = _turn_key(kwargs)
    now = time.time()
    hist = [t for t in _recent.get(key, ()) if now - t < CAPTURE_WINDOW]
    hist.append(now)
    _recent[key] = hist
    if len(_recent) > 32:                      # bound the dict, oldest first
        for k in sorted(_recent, key=lambda k: _recent[k][-1])[:16]:
            _recent.pop(k, None)
    return len(hist)


def _seconds_until_free(kwargs: dict) -> float:
    """How long until the caller may capture again. Never a guess."""
    hist = _recent.get(_turn_key(kwargs)) or []
    if len(hist) < MAX_FRAMES_PER_WINDOW:
        return 0.0
    return max(0.0, CAPTURE_WINDOW - (time.time() - hist[-MAX_FRAMES_PER_WINDOW]))


def _look_or_watch(args: dict, mode: str, **kwargs) -> str | dict:
    blocked = _blocked_reason()
    if blocked:
        return blocked

    n = _count_recent(kwargs)
    if n > MAX_FRAMES_PER_WINDOW:
        wait = _seconds_until_free(kwargs)
        return (f"Camera rate limit: {MAX_FRAMES_PER_WINDOW} captures in "
                f"{CAPTURE_WINDOW:.0f}s. This clears on its own in about "
                f"{wait:.0f}s -- it is not stuck and needs no restart. "
                f"Answer from what you have already seen, or say plainly that "
                f"you could not make it out and ask the user to move closer or "
                f"improve the light. Do NOT guess at what is in front of the "
                f"camera.")

    profile = args.get("detail") if args.get("detail") in PROFILES else "normal"
    reason = _sanitise(args.get("reason"))

    meta = _request(mode, profile, reason)
    if meta.get("error"):
        return (f"No live camera frame: {meta['error']}. "
                f"You have NOT seen anything. Do not describe the room.")

    age, age_text = _age_of(meta)
    if age is None:
        return (f"A frame was captured but its age cannot be trusted: "
                f"{age_text}. Rather than risk describing a stale moment as "
                f"live, it is not being shown. You have NOT seen anything.")
    if age > MAX_FRAME_AGE:
        return (f"The only available frame is {age_text} old, older than the "
                f"{MAX_FRAME_AGE:.0f}s freshness limit, so it is not being "
                f"shown. You have NOT seen anything current.")

    # Show it in the room. After the freshness checks, so the panel only ever
    # displays a frame that was actually good enough to send.
    _show_on_panel()

    question = _sanitise(kwargs.get("user_task"), 160)
    when = time.strftime("%H:%M:%S", time.localtime(float(meta["captured_at"])))

    if mode == "watch":
        span = float(meta.get("span_s") or 0)
        backward = bool(meta.get("backward"))
        covers = (f"the {span:.1f} seconds BEFORE your message"
                  if backward else
                  f"the {span:.1f} seconds AFTER your message, because the "
                  f"camera was asleep when you asked -- not the moment you "
                  f"were describing")
        header = (
            f"Camera contact sheet from the Raspberry Pi's Camera Module 3.\n"
            f"{meta.get('frames')} stills in a grid, covering {covers}.\n"
            f"Read left to right, top to bottom. Each tile has its time offset "
            f"burned in. This is a grid of stills, not video.\n"
            f"Newest tile captured {age_text} ago at {when} local.\n"
            f"Motion level over that window: {meta.get('motion')}.\n"
        )
    else:
        header = (
            f"Live camera frame from the Raspberry Pi's Camera Module 3.\n"
            f"Captured {age_text} ago at {when} local. "
            f"{meta.get('w')}x{meta.get('h')} JPEG.\n"
            f"This is the room in front of the Pi as of that moment. If the age "
            f"above is more than a couple of seconds, say so rather than "
            f"calling it live.\n"
        )
    if question:
        header += f'\nThe user asked: "{question}"'

    return _envelope(meta, header)


def camera_look(args: dict, **kwargs) -> str | dict:
    """One frame, now."""
    return _look_or_watch(args, "look", **kwargs)


def camera_watch(args: dict, **kwargs) -> str | dict:
    """Several moments as a single contact sheet -- for motion and activity."""
    return _look_or_watch(args, "watch", **kwargs)
