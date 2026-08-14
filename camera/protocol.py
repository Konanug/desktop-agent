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
#
# THIS IS RESOLUTION, NOT FIELD OF VIEW, and the difference matters because
# raising it is often asked for as a way to "see more of the room". It is not.
# MEASURED at four configurations: with SENSOR_OUTPUT_SIZE forced, ScalerCrop is
# (0,0,4608,2592) -- 100% of the sensor -- at 1024x576, 1536x864 AND 2048x1152.
# The view is already as wide as this lens gives. Only the standard module is
# fitted here (imx708, ~75 deg diagonal); widening it is an imx708_wide, i.e. a
# purchase, not a setting. What more resolution buys is DETAIL, which is worth
# having for detection and for a person watching -- just not more room.
#
# 1536x864 costs 19.0 ms cpu/frame against 13.0 at 1024x576 (measured, at a 640
# output). Chosen with hand tracking in mind, which is size-insensitive.
STREAM_SIZE = (1536, 864)

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

# ALWAYS-ON. Off by default, and that default is a deliberate position rather
# than caution: a camera that sleeps when nothing needs it is the difference
# between "it can look when asked" and "it is watching the room", and the panel
# indicator, the viewer count and the whole lazy lifecycle exist to make the
# first one legible.
#
# With this on, the sensor stays open and the CAM light stays lit permanently.
# That is honest -- the light follows the kernel's power state, so it cannot be
# on without saying so -- but it is a different thing to live with, so it is an
# explicit choice.
#
# What it buys: no ~434 ms cold wake before a look, and motion in status.json
# stays current. What it costs: the sensor is powered continuously.
ALWAYS_ON = _os.environ.get("HERMES_CAMERA_ALWAYS_ON", "off").lower() in (
    "on", "1", "yes", "true")

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

# -- live MJPEG stream (camera/stream.py) --------------------------------
# A browser view on the LAN. Off the beaten path for this project, which had
# exactly one network-facing socket (ssh) before it -- see docs/SECURITY.md.
STREAM_PORT = 8081

# MEASURED end to end (grab + correct + resize + JPEG), main stream 1536x864:
#     out  640 long edge -> 19.0 ms cpu, 17.3 KB  -> 28.5% core at 15 fps
#     out  960 long edge -> 26.5 ms cpu, 32.1 KB  -> 39.8% core at 15 fps
#     out 1280 long edge -> 39.0 ms cpu, 53.3 KB  -> 58.5% core at 15 fps
# 960 is the point where the picture is clearly better and there is still room
# for a 60 ms hand-detection pass on another core. 1280 leaves none.
STREAM_LONG_EDGE = 960
STREAM_QUALITY = 70
STREAM_FPS = 15.0

# -- hand tracking (camera/hands.py) -------------------------------------
# 10 Hz, not the stream's 15. MEASURED: inference is ~60 ms and, importantly,
# ~60 ms at EVERY input size, because mediapipe rescales to its own fixed input
# internally. So 10 Hz is already two thirds of a core; 15 would be all of one.
# It runs on its own thread, so this rate does not pace the stream.
#
# TUNABLE, because it is one of the two knobs that set gesture LAG and they
# pull in opposite directions. Detection rate sets how quickly the debounce can
# accumulate its 3-of-5: at 10 Hz a gesture needs ~200 ms of holding before it
# can commit, at 15 Hz ~133 ms. Raising it costs CPU roughly linearly; the
# other knob (gestures.WINDOW / MAJORITY) costs false triggers instead.
# Measured cost is in docs/GESTURES.md -- change this and re-measure.
HANDS_HZ = float(_os.environ.get("HERMES_CAMERA_HANDS_HZ", "10"))
HANDS_MAX = 2

# Grace period after the last viewer disconnects. A browser reloading the page
# closes and reopens the connection, and without a linger that round trip would
# sleep the sensor and pay a 434 ms cold wake on every refresh.
STREAM_LINGER = 8.0

# Frame duration pinned while someone is WATCHING, in microseconds.
#
# FRAME_DURATION_LIMITS above allows up to 100 ms of exposure, which is the
# right trade for a still: it halves the grain when a model has to read what is
# in the frame. It is the wrong trade for a live view. At 100 ms a dim room
# runs the stream at 10 fps and smears every hand movement into a streak --
# exactly the thing gesture work needs to see. Pinning 30 fps while a viewer is
# attached buys motion clarity at the cost of noise, and reverts on disconnect
# so captures keep the quiet exposure. Set at runtime; no reconfigure, no
# re-settle.
STREAM_FRAME_DURATION = (33333, 33333)


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


def hands_path() -> Path:
    """Latest hand-tracking observation.

    SEPARATE FROM status.json ON PURPOSE. The rule at the top of this file is
    that status carries STATE, NEVER CONTENT -- nothing derived from what the
    camera can see -- so that a file the display reads can never become a side
    channel for the room. A gesture is unambiguously derived from what the
    camera can see, so it does not belong there.

    It has to be published somewhere, because a desktop client acting on
    gestures is the whole point of the exercise. Putting it in its own file
    keeps the original rule intact and makes this an explicit exception rather
    than a quiet erosion of it. Nothing in this project reads this file yet.
    """
    return runtime_dir() / "hands.json"


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
