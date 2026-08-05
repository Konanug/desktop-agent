"""The interface between the camera service and the Hermes plugin.

Mirrors docs/STATE-CONTRACT.md in spirit: two processes that must not import
each other agree on a directory of small files. See docs/CAMERA-CONTRACT.md.

WHY FILES AND NOT A SOCKET
docs/ARCHITECTURE.md argues for a socket when the traffic is request/response
rather than durable state, and a capture IS request/response. Files still win
here for a different reason: no handshake, no reconnect logic, no second
failure mode to reason about at 3am. The polling cost is bounded and small --
the service is already spinning at the frame rate when awake, and polls at
100 ms when asleep, which disappears against a ~450 ms cold wake.

WHY A REQUESTS *DIRECTORY*
Parallel tool calls are real. A single request.json would have two writers
racing, which is the exact problem that forced state.json and request.json
apart on the display side. One file per request, served oldest-first, removes
the race instead of managing it.

ORDERING RULE
The .jpg is written first and the .json second. The metadata file existing is
therefore proof the image beside it is complete. A reader that finds only the
.jpg must wait, never read.

STATUS IS STATE, NEVER CONTENT
status.json carries what the service is doing -- never anything derived from
what the camera can see. Same rule as state.json.
"""

from __future__ import annotations

import os
from pathlib import Path

SCHEMA = 1

# Sensor is imx708 on this Pi; resolved by NAME so an i2c renumber cannot
# silently point the privacy indicator at the wrong device.
SENSOR_NAME = "imx708"

# Profiles. Measured on this hardware 2026-08-05 (tools/camera_probe.py
# --profile), in a lit room:
#   normal  768x432  q72 -> ~10.8 KB  -> ~14.4 KB base64
#   fine   1024x576  q78 -> ~20.0 KB  -> ~26.6 KB base64
# Ceilings are far above those and exist only so a pathological scene (heavy
# noise, confetti) cannot balloon the context. See docs/CAMERA.md.
PROFILES = {
    "normal": {"size": (768, 432), "quality": 72, "max_bytes": 96_000},
    "fine":   {"size": (1024, 576), "quality": 78, "max_bytes": 160_000},
}
DEFAULT_PROFILE = "normal"

# The sensor mode to force. MEASURED: a 1024x576 request auto-selects the
# 1536x864 mode, which is a (768,432)/3072x1728 crop -- 0.67x, a visibly
# NARROWER field of view. 2304x1296 gives ScalerCrop (0,0,4608,2592): the full
# frame. "What am I doing" needs the sides of the room, so this is forced.
SENSOR_OUTPUT_SIZE = (2304, 1296)

# Stream size the service runs at. 16:9, matching the sensor's native aspect,
# so nothing is cropped on the way out. Both profiles derive from it.
STREAM_SIZE = (1024, 576)

# Exposure ceiling. This is a NOISE vs MOTION-BLUR trade, and it only binds in
# poor light -- in a bright room AE picks a far shorter exposure and the cap is
# irrelevant.
#
# MEASURED in a dim room, same scene and brightness:
#     cap  66 ms -> exposure 66 ms, gain 16.0 (max), temporal noise 2.01
#     cap 100 ms -> exposure 99 ms, gain 10.8,       temporal noise 0.94
#
# Halving the grain is worth ~1.5x more blur here, because the subject is
# usually something being deliberately held up rather than something moving
# fast. If gesture tracking is ever built, revisit -- that case wants the
# shorter cap. Override with HERMES_CAMERA_MAX_EXPOSURE_US.
import os as _os
FRAME_DURATION_LIMITS = (
    33333, int(_os.environ.get("HERMES_CAMERA_MAX_EXPOSURE_US", "100000")))

# How long the sensor stays open after the last request. Cold wake measured at
# ~434 ms, so this is not about hiding latency -- it is about not reopening the
# sensor for a natural follow-up question, while still closing it within a
# breath of the conversation moving on.
IDLE_TIMEOUT = 20.0

# A frame older than this is not offered to the model at all. See
# camera_look's freshness gate: a stale frame presented as live is exactly the
# failure "the panel never invents state" exists to prevent.
MAX_FRAME_AGE = 2.0

# Phase 2 ring: 5 Hz x 4 s of decimated frames, so "what did I just do?" can
# look BACKWARDS when the camera is already awake.
RING_HZ = 5.0
RING_SECONDS = 4.0
RING_SIZE = (384, 216)


def runtime_dir() -> Path:
    base = os.environ.get("XDG_RUNTIME_DIR") or f"/run/user/{os.getuid()}"
    return Path(base) / "hermes-camera"


def ensure_dirs() -> Path:
    d = runtime_dir()
    (d / "requests").mkdir(mode=0o700, parents=True, exist_ok=True)
    (d / "captures").mkdir(mode=0o700, parents=True, exist_ok=True)
    d.chmod(0o700)
    return d


def status_path() -> Path:
    return runtime_dir() / "status.json"


def requests_dir() -> Path:
    return runtime_dir() / "requests"


def captures_dir() -> Path:
    return runtime_dir() / "captures"


def disabled_path() -> Path:
    """Transient mute. On tmpfs, so it does not survive a reboot -- a mute you
    forgot about must not silently persist for months."""
    return runtime_dir() / "DISABLED"


def persistent_disable_path() -> Path:
    """Durable owner kill switch.

    Deliberately under ~/.config/hermes-pi/ and NOT ~/.hermes/: anything Hermes
    manages, Hermes can rewrite. This is the owner's file, not the agent's.
    (It is still not enforceable against an agent that has a shell -- see
    docs/SECURITY.md. It is a convenience, and it is documented as one.)
    """
    return Path.home() / ".config" / "hermes-pi" / "camera.disabled"


def sensor_power_path(sensor: str = SENSOR_NAME) -> str | None:
    """Runtime-PM status file for the sensor, resolved by device name.

    This is the ONLY reliable "is the camera actually on" signal on this Pi.
    Open file descriptors do not work: libcamera never opens the capture node,
    and pipewire/wireplumber permanently hold every node it does open --
    including the imx708 subdev -- from boot on an idle system. An fd-based
    indicator would read "in use" 24/7. Measured 2026-08-05; see
    tools/camera_probe.py for the full write-up.
    """
    import glob
    for d in glob.glob("/sys/bus/i2c/devices/*"):
        try:
            if open(f"{d}/name").read().strip() == sensor:
                p = f"{d}/power/runtime_status"
                return p if os.path.exists(p) else None
        except OSError:
            continue
    return None


def sensor_powered(path: str | None = None) -> bool | None:
    """True powered / False suspended / None unknown.

    None is load-bearing: the caller must treat unknown as ON. For a privacy
    indicator the fail-safe direction is inverted from the rest of this
    project -- never claim the camera is off unless positively observed off.

    Lags by autosuspend_delay_ms (5000 ms measured) after streaming stops.
    That lag over-reports "on", which is the safe direction, so it stands.
    """
    p = path if path is not None else sensor_power_path()
    if not p:
        return None
    try:
        return open(p).read().strip() == "active"
    except OSError:
        return None
