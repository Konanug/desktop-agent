"""The only picamera2-specific file. Swap this to change cameras.

Same role as display/panel.py: every hardware assumption lives here so the
service loop above it is portable. Nothing else in the project imports
picamera2.

LIFECYCLE
Lazy. The sensor is closed at rest and opens only when a frame is actually
about to be taken, then closes again after protocol.IDLE_TIMEOUT. Cold wake
measured at ~434 ms on this hardware, so there is no latency argument for
holding it open -- and every second it is open is a second the camera is
watching the room.
"""

from __future__ import annotations

import os
import time

import numpy as np

from . import protocol

# libcamera logs several INFO lines to stderr on every configure(). Set before
# picamera2 is imported or the first lines still escape into the journal.
os.environ.setdefault("LIBCAMERA_LOG_LEVELS", "*:ERROR")

AF_CONTINUOUS = 2
AF_FOCUSED = 2

# Quarter-turn correction, degrees anticlockwise: 0, 90, 180 or 270.
# The camera module is mounted rotated relative to the room, which no amount of
# libcamera configuration fixes (its transform does flips only). Override with
# HERMES_CAMERA_ROTATE if the module is remounted.
ROTATE = int(os.environ.get("HERMES_CAMERA_ROTATE", "90")) % 360

# Everything that decides what the picture LOOKS like, set explicitly.
#
# Previously only AfMode and AeEnable were set and the rest was left to
# whatever libcamera defaulted to. That is not a defensible position for a
# camera whose output a model has to reason about: "it looked fine when I
# tried it" is not the same as knowing what will happen in a different room.
#
# The goal is a plain, honest photograph -- not a pleasing one. Saturation and
# contrast are left at 1.0 (neutral) deliberately: pushing them makes a nicer
# thumbnail and a worse measurement, and the whole point of this camera is that
# something downstream is trying to work out what is actually there.
IMAGE_CONTROLS = {
    "AfMode": AF_CONTINUOUS,

    # Auto white balance ON and in auto mode. This is what stops a warm room
    # rendering orange and a cloudy window rendering blue. It was previously
    # only on because it happened to be the default.
    "AwbEnable": True,
    "AwbMode": 0,              # 0 = auto, rather than a fixed illuminant

    "AeEnable": True,
    "AeExposureMode": 0,       # normal: prefer exposure time over gain (less noise)
    # Centre-weighted metering. A big bright wall or desk filling the edges
    # otherwise drags the whole frame dark; what matters is the middle, which
    # is where a hand being held up will be.
    "AeMeteringMode": 0,
    "AeConstraintMode": 0,     # normal highlight/shadow trade-off

    "Brightness": 0.0,         # neutral
    "Contrast": 1.0,           # neutral -- do NOT stylise
    "Saturation": 1.0,         # neutral; oversaturating helps nobody read a scene
    "Sharpness": 1.0,          # default; over-sharpening invents edge detail
    "NoiseReductionMode": 0,   # auto
}


class Sensor:
    """Owns the camera. Exclusive: only one process on the box may hold it."""

    def __init__(self) -> None:
        self._cam = None
        self._opened_at = 0.0
        self.last_error: str | None = None

    # -- state ----------------------------------------------------------
    @property
    def is_open(self) -> bool:
        return self._cam is not None

    def powered(self) -> bool | None:
        """Kernel's view, not ours. See protocol.sensor_powered."""
        return protocol.sensor_powered()

    # -- lifecycle ------------------------------------------------------
    def open(self, wait_for_focus: float = 1.2) -> bool:
        """Open and settle. Returns False and sets last_error on failure."""
        if self._cam is not None:
            return True
        try:
            from picamera2 import Picamera2
        except Exception as e:                                # pragma: no cover
            self.last_error = f"picamera2 unavailable: {e}"
            return False

        try:
            cam = Picamera2()
            cfg = cam.create_video_configuration(
                main={"size": protocol.STREAM_SIZE, "format": "RGB888"},
                controls={"FrameDurationLimits": protocol.FRAME_DURATION_LIMITS},
                buffer_count=4,
                # queue=False makes capture_array() wait for the NEXT freshly
                # exposed frame instead of handing back one already sitting in
                # the queue. Borrowed from a working camera project of the
                # owner's, and it matters more here than it does there: this
                # module's whole contract with the model is that a frame is
                # from *now*, and camera_look refuses anything older than 2s.
                # A queued frame could be up to a few hundred ms stale while
                # being timestamped at the moment we received it -- an age that
                # would read as fresh and be wrong.
                queue=False,
            )
            # MEASURED: without this libcamera picks the 1536x864 mode, which is
            # a 0.67x centre crop -- a narrower field of view. Forcing the full
            # mode keeps the sides of the room in frame.
            cfg["sensor"] = {"output_size": protocol.SENSOR_OUTPUT_SIZE,
                             "bit_depth": 10}
            cam.configure(cfg)
            cam.set_controls(IMAGE_CONTROLS)
            cam.start()
        except Exception as e:
            self.last_error = f"camera open failed: {e}"
            try:
                cam.close()
            except Exception:
                pass
            return False

        self._cam = cam
        self._opened_at = time.time()
        self.last_error = None

        # Wait for autofocus rather than sleeping a fixed guess. Measured
        # ~406 ms to Focused; the timeout is a backstop, not the expected path.
        deadline = time.time() + wait_for_focus
        try:
            while time.time() < deadline:
                if cam.capture_metadata().get("AfState") == AF_FOCUSED:
                    break
        except Exception:
            pass            # a soft frame beats no frame; do not fail the open
        return True

    def close(self) -> None:
        cam, self._cam = self._cam, None
        if cam is None:
            return
        for fn in ("stop", "close"):
            try:
                getattr(cam, fn)()
            except Exception:
                pass

    # -- capture --------------------------------------------------------
    def grab(self):
        """(frame, captured_at, captured_monotonic) or None.

        Both clocks are recorded on purpose. The Pi has no battery-backed RTC
        and its wall clock is confidently wrong for ~34 s after boot (trap 6),
        so a consumer that needs an AGE must be able to notice the wall clock
        moving in a way monotonic did not.
        """
        if self._cam is None:
            return None
        try:
            frame = self._cam.capture_array("main")
        except Exception as e:
            self.last_error = f"capture failed: {e}"
            return None

        # picamera2's "RGB888" gives BGR in the numpy array. The format name
        # describes the packed byte order, which is the reverse of the channel
        # order you get out. Treating it as RGB swaps red and blue, and the
        # symptom is a strong blue cast on everything -- skin goes blue, warm
        # light goes cold.
        #
        # MEASURED rather than taken from the docs: compare capture_array()
        # against capture_image() (picamera2's own PIL path, which is known
        # correct). Mean absolute difference was 1.43 as-is versus 0.27 when
        # channel-reversed. See docs/CAMERA.md.
        frame = frame[..., ::-1]

        if ROTATE:
            # The sensor is mounted rotated relative to the scene. libcamera's
            # transform only does h/v flips (i.e. 180 degrees), so a quarter
            # turn has to happen here. np.rot90 is a view + copy of ~1.8 MB,
            # well under a millisecond at this size.
            frame = np.rot90(frame, k=ROTATE // 90)

        return np.ascontiguousarray(frame), time.time(), time.monotonic()

    def metadata(self) -> dict:
        if self._cam is None:
            return {}
        try:
            return dict(self._cam.capture_metadata())
        except Exception:
            return {}
