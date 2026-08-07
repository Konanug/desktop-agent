"""hermes-camera entrypoint.

The third process. Owns the camera sensor exclusively and nothing else -- it
never touches /dev/fb0 and never writes to the SPI bus.

WHY A SEPARATE SERVICE
  * Not the display service: its loop is timing-critical to ~1 ms and lives
    inside 13.3% of spare SPI bandwidth. A 30 fps capture pipeline does not
    belong in it.
  * Not the gateway: picamera2 holds an exclusive kernel resource, and the
    gateway is restarted routinely. A wedged sensor would take Discord down.
  * As a third process, any one of the three can die without the other two
    noticing -- the property the whole system is built on. It also means
    `systemctl --user stop hermes-camera` is a real off switch that does not
    disturb the agent.

Loop shape: serve capture requests from a tmpfs directory, keep a decimated
ring of recent frames while awake, feed the live MJPEG stream when somebody is
watching, and close the sensor once nothing has asked for a while.

THREE CONSUMERS, ONE GRAB
The stream (camera/stream.py) is not a second owner of the camera -- it cannot
be, the sensor is exclusive -- it is a third consumer of the frames this loop
already pulls. pump_frames() takes ONE frame per tick and hands it to whichever
of the stream and the ring is due, so adding the browser view did not double
the capture rate.
"""

from __future__ import annotations

import argparse
import collections
import json
import os
import signal
import sys
import threading
import time
from pathlib import Path

import numpy as np
from PIL import Image

from . import encode, gestures, hands, protocol, stream
from .sensor import Sensor

POLL_ASLEEP = 0.10      # invisible against a ~434 ms cold wake
POLL_AWAKE = 0.02
# A fixed poll cannot express a frame period that is not a multiple of it --
# the same arithmetic that made the panel hitch four times a second (trap 9).
# At POLL_AWAKE and 15 fps the stream would land on 80 ms instead of 66.7 ms,
# a silent 12.5 fps. A 5 ms tick holds the period to within one poll.
POLL_STREAM = 0.005
STATUS_PERIOD = 1.0
SWEEP_PERIOD = 1.0
CAPTURE_TTL = 60.0      # captures are worthless once the turn has moved on

# Watchdog. OBSERVED ONCE, not theorised: the capture loop wedged with the
# sensor open and powered, producing no frames at all -- ring empty, stream at
# 0 fps -- while the HTTP thread carried on answering happily. Every browser
# connection sat for five seconds and closed with zero frames, which reads
# exactly like a network fault. It survived for four minutes until the service
# was restarted by hand, and it has not reproduced since, including under
# deliberate browser-like load.
#
# The cause is still unknown. What is NOT acceptable is the failure being
# silent and permanent, so this converts it into a loud, self-healing one:
# while the sensor is open the ring pumps at RING_HZ regardless of viewers, so
# a frame drought is unambiguous evidence the loop is stuck. Exiting hands the
# problem to systemd's Restart=always (3 s), and StartLimitBurst still guards
# against a crash loop.
STALL_TIMEOUT = 15.0
WATCHDOG_PERIOD = 5.0

_stop = False


def _handle_signal(signum, _frame):
    global _stop
    _stop = True


def _claim_single_instance():
    """Refuse to start if another instance already owns the sensor.

    The camera is an exclusive kernel resource, so a second copy does not
    degrade -- it fails, and it fails with `Camera __init__ sequence did not
    complete`, which says nothing about the actual cause. That is easy to hit:
    systemd starts one, then someone runs `python3 -m camera` by hand to test
    something and the service silently stops being able to capture.

    An flock held for the process lifetime turns that into one clear line. The
    lock is released automatically on exit, including a crash, because the fd
    dies with the process -- no stale lockfile to clean up.

    Returns the fd, which must be kept alive; do not let it be collected.
    """
    import fcntl
    lock = protocol.runtime_dir() / "service.lock"
    fd = os.open(lock, os.O_CREAT | os.O_RDWR, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        try:
            holder = os.read(fd, 32).decode().strip() or "unknown"
        except OSError:
            holder = "unknown"
        os.close(fd)
        print(f"[camera] another instance already owns the sensor (pid {holder}). "
              f"Refusing to start a second one.", flush=True)
        return None
    os.ftruncate(fd, 0)
    os.write(fd, str(os.getpid()).encode())
    return fd


def _atomic_write(path, data: bytes) -> None:
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_bytes(data)
    os.replace(tmp, path)


def _muted() -> str | None:
    """Reason the camera is muted, or None. Checked before every open."""
    if protocol.persistent_disable_path().exists():
        return "disabled by owner (~/.config/hermes-pi/camera.disabled)"
    if protocol.disabled_path().exists():
        return "muted (runtime DISABLED flag)"
    return None


def _sanitise(text: str, limit: int = 48) -> str:
    """Reason strings come from the model and end up in the journal."""
    out = "".join(c for c in str(text or "") if c.isprintable())
    return out[:limit].strip() or "unspecified"


class Service:
    def __init__(self, idle_timeout: float = protocol.IDLE_TIMEOUT):
        self.sensor = Sensor()
        self.idle_timeout = idle_timeout
        self.last_request = 0.0
        self.last_status = 0.0
        self.state = "off"
        self.motion = 0.0
        self._ring: collections.deque = collections.deque(
            maxlen=int(protocol.RING_HZ * protocol.RING_SECONDS))
        self._last_ring = 0.0
        self._prev_small: np.ndarray | None = None
        self.started_at = time.time()

        # Live stream. The buffer exists even when the server does not, so
        # every call site can push unconditionally instead of guarding.
        self.stream_buf = stream.StreamBuffer()
        self.stream_srv: stream.StreamServer | None = None
        self._last_stream = 0.0
        self._stream_count = 0
        self._stream_window = time.time()
        self.stream_fps = 0.0
        self._live_pinned = False
        self._last_wake_try = 0.0
        self._last_sweep = 0.0

        # Liveness. Both are written by the CAPTURE LOOP and by nothing else,
        # which is the whole point -- see status_doc().
        self.loop_tick = time.monotonic()
        self.last_frame_at = 0.0

        # Hand tracking. Optional in the strongest sense: if mediapipe or the
        # model is missing the tracker never starts and everything else --
        # including Hermes' ability to see -- carries on unchanged.
        self.tracker = hands.HandTracker(interval=1.0 / protocol.HANDS_HZ,
                                         max_hands=protocol.HANDS_MAX)
        self.hands_enabled = os.environ.get(
            "HERMES_CAMERA_HANDS", "on").lower() not in ("off", "0", "no")
        self._last_hands = 0.0
        self._hands_written = 0.0
        self._last_hands_err: str | None = None

        # Gesture EDGES, published to /events for a subscriber to act on.
        # Nothing on this Pi acts on them and nothing here can reach Hermes --
        # see camera/gestures.py and docs/GESTURES.md for why that line is
        # where it is.
        self.gate = gestures.GestureGate(vocabulary=hands.vocabulary())
        self.gestures_enabled = os.environ.get(
            "HERMES_CAMERA_GESTURES", "on").lower() not in ("off", "0", "no")
        self._last_gesture_at = 0.0

    # -- status ---------------------------------------------------------
    def status_doc(self) -> dict:
        """STATE, NEVER CONTENT. Same rule as state.json.

        Also served verbatim to the browser at /status.json, which is why
        `live` is computed HERE rather than inferred in the page: whether the
        picture on screen is current is a fact this process knows and the
        browser cannot work out from a stalled <img>.

        `updated_at` IS NOT A HEARTBEAT, and reading it as one wasted a real
        diagnosis. This document is built on demand, so when it is fetched over
        HTTP the timestamp is written by the HTTP thread -- it stays perfectly
        current while the capture loop is wedged solid. `loop_idle_s` and
        `last_frame_age_s` are written only by the capture loop and are the
        figures that actually mean something is alive.
        """
        frame, _seq, at = self.stream_buf.latest()
        now = time.time()
        return {
            "schema": protocol.SCHEMA,
            "updated_at": now,
            # Liveness of the CAPTURE LOOP specifically.
            "loop_idle_s": round(time.monotonic() - self.loop_tick, 2),
            "last_frame_age_s": (round(now - self.last_frame_at, 2)
                                 if self.last_frame_at else None),
            "sensor_error": self.sensor.last_error,
            "pid": os.getpid(),
            "started_at": self.started_at,
            "state": self.state,
            # The kernel's view, not ours. If these ever disagree, the kernel
            # is right and we are the ones lying.
            "sensor_powered": protocol.sensor_powered(),
            "muted": _muted(),
            "motion": round(self.motion, 4),
            "ring_frames": len(self._ring),
            "idle_timeout": self.idle_timeout,
            "viewers": self.stream_buf.viewers,
            # The subset actually reading frames. viewers - pixel_viewers is
            # how many clients are watching the room for gestures alone.
            "pixel_viewers": self.stream_buf.pixel_viewers,
            "stream_fps": round(self.stream_fps, 2),
            "live": frame is not None and time.time() - at <= stream.STALE_AFTER,
            # Whether tracking is RUNNING is state. What it saw is content, and
            # lives in hands.json -- see protocol.hands_path().
            "hands_tracking": self.tracker.available and self.hands_enabled,
            "hands_error": self.tracker.error,
            "hands_ms": round(self.tracker.mean_ms, 1),
            # Counts and a cursor are STATE. What the gesture WAS is content
            # and lives on /events, which is the same split as hands.json.
            "gestures_enabled": self.gestures_enabled,
            "gesture_cursor": self.gate.log.cursor,
            "gestures_fired": self.gate.fired,
            "gestures_suppressed": self.gate.suppressed,
        }

    def publish_status(self, now: float, force: bool = False) -> None:
        if not force and now - self.last_status < STATUS_PERIOD:
            return
        self.last_status = now
        doc = self.status_doc()
        try:
            _atomic_write(protocol.status_path(),
                          json.dumps(doc, indent=2).encode())
        except OSError:
            pass

    # -- sensor ---------------------------------------------------------
    def ensure_awake(self) -> bool:
        if self.sensor.is_open:
            return True
        why = _muted()
        if why:
            self.sensor.last_error = why
            return False
        self.state = "warming"
        self.publish_status(time.time(), force=True)
        ok = self.sensor.open()
        # Start the frame clock at the open, not at the first frame: otherwise
        # a reopen inherits the stale timestamp from the previous session and
        # the watchdog fires on a camera that is merely warming up.
        self.last_frame_at = time.time() if ok else 0.0
        self.state = "awake" if ok else "off"
        self.publish_status(time.time(), force=True)
        if ok:
            print(f"[camera] sensor opened", flush=True)
        else:
            print(f"[camera] open failed: {self.sensor.last_error}", flush=True)
        return ok

    def sleep_sensor(self) -> None:
        if not self.sensor.is_open:
            return
        self.sensor.close()
        self.state = "off"
        self._ring.clear()
        self._prev_small = None
        self.motion = 0.0
        self._live_pinned = False
        self.stream_fps = 0.0
        self.last_frame_at = 0.0        # closed sensor is not a stall
        # Forget the last frame. Anything still holding the page open must see
        # NO SIGNAL, not the final picture before the camera closed.
        self.stream_buf.drop()
        if self.tracker.available:
            self.tracker.stop()
            self._reset_tracker()
        self.publish_status(time.time(), force=True)
        print("[camera] sensor closed (idle)", flush=True)

    # -- stream demand --------------------------------------------------
    def watching(self, now: float) -> bool:
        """Is someone looking at the live view right now?

        A viewer is a reason to be awake -- that is the stream's on switch, and
        there is deliberately no other one. The linger covers a page reload,
        which closes and reopens the connection and would otherwise pay a
        434 ms cold wake every refresh.
        """
        if self.stream_srv is None:
            return False
        if self.stream_buf.viewers > 0:
            return True
        return now - self.stream_buf.last_viewer_at < protocol.STREAM_LINGER

    def apply_live_tuning(self, live: bool) -> None:
        """Short exposure while watched, quiet exposure otherwise."""
        if not self.sensor.is_open or live == self._live_pinned:
            return
        limits = (protocol.STREAM_FRAME_DURATION if live
                  else protocol.FRAME_DURATION_LIMITS)
        if self.sensor.set_frame_duration(limits):
            self._live_pinned = live
            print(f"[camera] frame duration {limits[0]}-{limits[1]} us "
                  f"({'live view' if live else 'stills'})", flush=True)

    def apply_tracking(self, live: bool) -> None:
        """Track hands only while somebody is watching.

        Two reasons, and the second is the important one. It costs ~60 ms of a
        core per pass, which is not worth spending on an empty room. And a
        camera that continuously analyses what people in the room are DOING is
        a materially different thing from one that streams pixels -- tying it
        to an attached viewer keeps it bounded by something the owner can see.
        """
        if not self.hands_enabled:
            return
        if live and not self.tracker.available:
            if self.tracker.start():
                print("[camera] hand tracking started "
                      f"({protocol.HANDS_HZ:.0f} Hz, model {hands.model_path()})",
                      flush=True)
            elif self.tracker.error and self.tracker.error != self._last_hands_err:
                self._last_hands_err = self.tracker.error
                print(f"[camera] hand tracking unavailable: "
                      f"{self.tracker.error}", flush=True)
        elif not live and self.tracker.available:
            self.tracker.stop()
            self._reset_tracker()
            print("[camera] hand tracking stopped (nobody watching)", flush=True)

    def _reset_tracker(self) -> None:
        """A stopped tracker must not leave a result behind that later reads as
        current, nor a hands.json a desktop client could act on."""
        self.tracker = hands.HandTracker(interval=1.0 / protocol.HANDS_HZ,
                                         max_hands=protocol.HANDS_MAX)
        # Same reasoning one level up: a latched gesture and a ring of old
        # edges from the previous session must not survive into the next one,
        # or the first thing a reconnecting client does is act on a gesture
        # somebody made before the camera was even closed.
        self.gate.reset()
        self._last_gesture_at = 0.0
        try:
            protocol.hands_path().unlink(missing_ok=True)
        except OSError:
            pass

    # -- frames ---------------------------------------------------------
    def pump_frames(self, now: float) -> None:
        """ONE grab, fed to whichever consumer is due.

        The ring keeps a decimated history so 'what did I just do' can look
        BACKWARDS -- without it, a question asked after the interesting moment
        could only sample forward, i.e. show the wrong few seconds. The stream
        wants the same frames an order of magnitude more often. Grabbing twice
        would double the sensor traffic to no purpose, so they share.
        """
        if not self.sensor.is_open:
            return
        live = self.watching(now)
        # ENCODING IS DRIVEN BY PIXEL VIEWERS, NOT BY VIEWERS. A gesture
        # subscriber is a viewer in every sense that matters -- it holds the
        # sensor open and lights the panel -- but it reads no frames, and
        # encoding 15 fps of JPEG for it is pure waste. Before this split a
        # gesture-only client cost 77.0% of a core.
        pixels = live and self.stream_buf.pixel_viewers > 0
        stream_due = pixels and now - self._last_stream >= 1.0 / protocol.STREAM_FPS
        hands_due = (live and self.tracker.available
                     and now - self._last_hands >= 1.0 / protocol.HANDS_HZ)
        ring_due = now - self._last_ring >= 1.0 / protocol.RING_HZ

        # Rolling delivered rate, updated whether or not a frame went out, so a
        # stream that has quietly stopped reads 0 rather than freezing at
        # whatever it managed last.
        if now - self._stream_window >= 1.0:
            self.stream_fps = self._stream_count / (now - self._stream_window)
            self._stream_count = 0
            self._stream_window = now

        if not (stream_due or hands_due or ring_due):
            return

        # Ask the sensor for the largest size any consumer needs, and no more.
        # Only a capture bound for the model wants the full size, and
        # correcting pixels nobody will look at is the most expensive thing
        # this loop can do.
        long_edge = (protocol.STREAM_LONG_EDGE if (stream_due or hands_due)
                     else max(protocol.RING_SIZE))
        got = self.sensor.grab(long_edge=long_edge)
        if got is None:
            return
        frame, wall, mono = got
        self.last_frame_at = wall           # the watchdog's only evidence

        if hands_due:
            # Detection is size-insensitive (measured), so handing over the
            # frame the loop already made costs no extra capture, no extra
            # resize and no extra copy -- the thread just reads it.
            self._last_hands = now
            self.tracker.offer(frame, wall)

        # Observations are published on the TRACKER's schedule, not the
        # encoder's. They used to hang off the stream path, which meant a
        # client subscribed only to gestures got none unless something else
        # happened to be watching the pixels at the same time.
        if live and self.tracker.available:
            result = self.tracker.result()
            self._publish_hands(result, now)
            self._publish_gestures(result, now)
        else:
            result = None

        if stream_due:
            self._push_stream(frame, wall, now, result)
        if ring_due:
            self._push_ring(frame, wall, mono, now)

    def _push_stream(self, frame, wall: float, now: float, result) -> None:
        overlay = (lambda img: hands.draw(img, result, now)) if result else None
        try:
            jpeg = encode.to_stream_jpeg(frame, overlay=overlay)
        except Exception as e:                              # pragma: no cover
            print(f"[camera] stream encode failed: {e}", flush=True)
            return
        self.stream_buf.push(jpeg, wall)
        self._last_stream = now
        self._stream_count += 1

    def _publish_hands(self, result, now: float) -> None:
        """Write hands.json for anything that wants to act on a gesture.

        Rate-limited to the tracker's own rate: rewriting an identical document
        at the stream rate would be pure churn.

        `stale` is carried explicitly rather than left for a reader to work out
        from the timestamp. A desktop client polling this file must not act on
        a gesture from two seconds ago, and making that require arithmetic on
        the reader's side is how it ends up not being done.
        """
        if result is None or now - self._hands_written < 1.0 / protocol.HANDS_HZ:
            return
        self._hands_written = now
        doc = {
            "schema": protocol.SCHEMA,
            "updated_at": now,
            "captured_at": result.at,
            "age_s": round(now - result.at, 3),
            "stale": not result.fresh(now),
            "detect_ms": round(result.latency, 1),
            "hands": [h.as_dict() for h in result.hands],
            # Said in the file itself, because this is the file someone will
            # find first when wiring a gesture to an action.
            "note": ("observation only -- nothing in this project acts on it. "
                     "See docs/SECURITY.md before wiring a gesture to a "
                     "command."),
        }
        try:
            _atomic_write(protocol.hands_path(),
                          json.dumps(doc).encode())
        except OSError:
            pass

    def _publish_gestures(self, result, now: float) -> None:
        """Feed the debouncer ONCE PER NEW RESULT.

        This is called from the stream path, which runs at STREAM_FPS while the
        tracker runs at HANDS_HZ -- so the same result is seen roughly 1.5
        times. Feeding it twice would count as two observations and halve the
        real hold time the debounce demands, turning "a deliberate gesture"
        into "a hand that passed through a shape". Deduped on the FRAME's
        timestamp, which the tracker copies through and never invents.

        A stale result is not observed at all, for the reason hands.draw()
        refuses to draw one: a gesture from a second ago is not evidence about
        now. It also means the gate simply stops advancing if detection dies,
        rather than latching on whatever it last saw.
        """
        if not self.gestures_enabled or result is None:
            return
        if result.at == self._last_gesture_at or not result.fresh(now):
            return
        self._last_gesture_at = result.at
        for ev in self.gate.observe(result, now=now):
            # One journal line per edge. This is the audit trail for anything
            # a subscriber does off the back of it, and journald here is
            # persistent -- see docs/GESTURES.md.
            print(f"[camera] gesture seq={ev.seq} {ev.hand} {ev.gesture} "
                  f"[{ev.fingers_up}] score={ev.score:.2f} "
                  f"latency={ev.latency_ms:.0f}ms dwell={ev.dwell_ms:.0f}ms",
                  flush=True)

    def _push_ring(self, frame, wall: float, mono: float, now: float) -> None:
        # Preserve aspect here too. RING_SIZE is written landscape, but a
        # rotated sensor produces portrait frames, and resizing straight to it
        # squashed every ring frame -- which then squashed the contact sheet,
        # so warm "what did I just do" looked different from cold.
        small = np.asarray(
            Image.fromarray(frame).resize(
                encode._fit_long_edge(
                    (frame.shape[1], frame.shape[0]), max(protocol.RING_SIZE)),
                Image.BILINEAR),
            dtype=np.uint8)
        self._ring.append((small, wall, mono))
        self._last_ring = now

        if self._prev_small is not None and self._prev_small.shape == small.shape:
            self.motion = float(np.abs(small.astype(np.int16)
                                       - self._prev_small.astype(np.int16)).mean())
        self._prev_small = small

    # -- requests -------------------------------------------------------
    def next_request(self):
        d = protocol.requests_dir()
        try:
            files = sorted(d.glob("*.json"), key=lambda p: p.stat().st_mtime)
        except OSError:
            return None
        for f in files:
            try:
                req = json.loads(f.read_text())
            except Exception:
                f.unlink(missing_ok=True)
                continue
            f.unlink(missing_ok=True)
            return req
        return None

    def serve(self, req: dict) -> None:
        rid = str(req.get("id") or int(time.time() * 1000))
        profile = req.get("profile") if req.get("profile") in protocol.PROFILES \
            else protocol.DEFAULT_PROFILE
        mode = "watch" if req.get("mode") == "watch" else "look"
        reason = _sanitise(req.get("reason"))
        self.last_request = time.time()

        if not self.ensure_awake():
            self._fail(rid, self.sensor.last_error or "camera unavailable")
            return

        if mode == "watch":
            self._serve_watch(rid, profile, reason)
        else:
            self._serve_look(rid, profile, reason)

    def _serve_look(self, rid: str, profile: str, reason: str) -> None:
        got = self.sensor.grab()
        if got is None:
            self._fail(rid, self.sensor.last_error or "capture failed")
            return
        frame, wall, mono = got
        jpeg, size, quality = encode.to_jpeg(frame, profile)
        self._write_capture(rid, jpeg, {
            "id": rid, "kind": "look", "profile": profile,
            "captured_at": wall, "captured_monotonic": mono,
            "w": size[0], "h": size[1], "bytes": len(jpeg),
            "quality": quality, "motion": round(self.motion, 4),
        })
        self._write_preview(frame)
        print(f"[camera] capture id={rid} profile={profile} "
              f"bytes={len(jpeg)} reason=\"{reason}\"", flush=True)

    def _serve_watch(self, rid: str, profile: str, reason: str) -> None:
        """A contact sheet of several moments, as ONE image.

        Two cases, and the difference matters enough that it is reported to the
        model rather than smoothed over: if the ring already holds history the
        sheet covers the seconds BEFORE the question (which is when the thing
        being asked about actually happened). If the camera was asleep, the
        ring is empty and we can only sample FORWARD -- a different span, and
        the model must not assume it saw the right moment.
        """
        want = 4
        backward = len(self._ring) >= want
        if backward:
            # Spread the picks ACROSS the ring, not the last four frames.
            # The ring runs at 5 Hz, so the last four cover only ~0.8 s -- too
            # short to show someone throwing something. Evenly spaced picks
            # cover the ring's full ~4 s, which is the span the question
            # "what did I just do" is actually about.
            ring = list(self._ring)
            step = (len(ring) - 1) / (want - 1)
            picked = [ring[round(i * step)] for i in range(want)]
        else:
            picked = []
            deadline = time.time() + 1.8
            while len(picked) < want and time.time() < deadline:
                got = self.sensor.grab()
                if got:
                    frame, wall, mono = got
                    picked.append((frame, wall, mono))
                time.sleep(0.45)
            if not picked:
                self._fail(rid, "no frames captured")
                return

        newest = picked[-1][1]
        labels = [f"-{newest - w:.1f}s" if newest - w > 0.05 else "now"
                  for _, w, _ in picked]
        jpeg, size, quality = encode.contact_sheet(
            [f for f, _, _ in picked], labels, profile)
        span = newest - picked[0][1]
        self._write_capture(rid, jpeg, {
            "id": rid, "kind": "watch", "profile": profile,
            "captured_at": newest, "captured_monotonic": picked[-1][2],
            "w": size[0], "h": size[1], "bytes": len(jpeg),
            "quality": quality, "frames": len(picked),
            "span_s": round(span, 2), "backward": backward,
            "motion": round(self.motion, 4),
        })
        self._write_preview(picked[-1][0])
        print(f"[camera] watch id={rid} frames={len(picked)} "
              f"span={span:.1f}s backward={backward} bytes={len(jpeg)} "
              f"reason=\"{reason}\"", flush=True)

    # -- output ---------------------------------------------------------
    def _write_capture(self, rid: str, jpeg: bytes, meta: dict) -> None:
        d = protocol.captures_dir()
        # Image FIRST, metadata SECOND. The metadata existing is the reader's
        # proof that the image beside it is complete.
        _atomic_write(d / f"{rid}.jpg", jpeg)
        _atomic_write(d / f"{rid}.json", json.dumps(meta).encode())

    def _fail(self, rid: str, error: str) -> None:
        _atomic_write(protocol.captures_dir() / f"{rid}.json",
                      json.dumps({"id": rid, "error": str(error)[:200]}).encode())
        print(f"[camera] capture id={rid} FAILED: {error}", flush=True)

    def _write_preview(self, frame) -> None:
        """Publish what was sent, for the panel to show.

        Written into the display service's own spool so the existing trust
        boundary is reused unchanged: the renderer still accepts only a file of
        exactly the expected length from a directory it controls.
        """
        try:
            base = os.environ.get("XDG_RUNTIME_DIR") or f"/run/user/{os.getuid()}"
            display_dir = Path(base) / "hermes-display"
            if not display_dir.is_dir():
                return          # display service has never run; nothing to show on
            # The images spool is created lazily by hermes_display the first
            # time it shows something, so on a panel that has only ever
            # rendered status it does not exist yet. Create it with the same
            # 0700 the display plugin uses rather than skipping silently.
            spool = display_dir / "images"
            spool.mkdir(mode=0o700, exist_ok=True)
            _atomic_write(spool / "camera-preview.rgb565",
                          encode.to_panel_rgb565(frame))
        except Exception as e:
            print(f"[camera] preview unavailable: {e}", flush=True)

    def stall_reason(self, now: float | None = None) -> str | None:
        """Why the capture loop looks dead, or None if it looks alive.

        Only meaningful while the sensor is OPEN, because that is when frames
        are unconditionally expected: the ring pumps at RING_HZ whether or not
        anyone is watching, so a frame drought with the sensor open cannot be
        explained by there being nothing to do.
        """
        if not self.sensor.is_open:
            return None
        now = now or time.time()
        if self.last_frame_at and now - self.last_frame_at > STALL_TIMEOUT:
            return (f"no frame for {now - self.last_frame_at:.0f}s with the "
                    f"sensor open")
        if time.monotonic() - self.loop_tick > STALL_TIMEOUT:
            return (f"capture loop has not ticked for "
                    f"{time.monotonic() - self.loop_tick:.0f}s")
        return None

    def start_watchdog(self) -> None:
        def run():
            while True:
                time.sleep(WATCHDOG_PERIOD)
                why = self.stall_reason()
                if why is None:
                    continue
                # Say everything useful BEFORE exiting; this journal line is
                # the only account of the failure that will survive.
                print(f"[camera] WATCHDOG: {why}. state={self.state} "
                      f"powered={protocol.sensor_powered()} "
                      f"viewers={self.stream_buf.viewers} "
                      f"sensor_error={self.sensor.last_error} "
                      f"-- exiting so systemd restarts a working one",
                      flush=True)
                sys.stdout.flush()
                sys.stderr.flush()
                # os._exit, not sys.exit: the loop is wedged, so an exception
                # in this thread would be ignored and interpreter shutdown
                # could block on the very thing that is stuck.
                os._exit(70)
        threading.Thread(target=run, name="watchdog", daemon=True).start()

    def sweep(self, now: float) -> None:
        # Throttled: the loop ticks at 200 Hz while streaming and globbing the
        # spool that often is pure waste against a 60 s TTL.
        if now - self._last_sweep < SWEEP_PERIOD:
            return
        self._last_sweep = now
        for f in protocol.captures_dir().glob("*"):
            try:
                if now - f.stat().st_mtime > CAPTURE_TTL:
                    f.unlink(missing_ok=True)
            except OSError:
                pass


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="hermes-camera")
    ap.add_argument("--idle-timeout", type=float, default=protocol.IDLE_TIMEOUT)
    ap.add_argument("--capture-test", action="store_true",
                    help="measure cold and warm capture, then exit")
    ap.add_argument("--stream-url", action="store_true",
                    help="print the live stream URL (with token) and exit")
    args = ap.parse_args(argv)

    if args.stream_url:
        # Takes no lock and touches no hardware: safe to run while the service
        # is up, which is the only time the answer is useful.
        port = int(os.environ.get("HERMES_CAMERA_STREAM_PORT",
                                  str(protocol.STREAM_PORT)))
        tok = stream.load_or_create_token()
        print(f"http://{stream.local_ip()}:{port}/?k={tok}")
        return 0

    signal.signal(signal.SIGTERM, _handle_signal)
    signal.signal(signal.SIGINT, _handle_signal)
    protocol.ensure_dirs()

    # Held for the process lifetime; released by the kernel on exit.
    _lock = _claim_single_instance()
    if _lock is None:
        return 1

    svc = Service(idle_timeout=args.idle_timeout)

    if args.capture_test:
        return _capture_test(svc)

    print(f"[camera] started, idle timeout {svc.idle_timeout:.0f}s, "
          f"power file {protocol.sensor_power_path()}", flush=True)
    svc.stream_srv = stream.start(
        svc.stream_buf, svc.status_doc,
        svc.gate.log if svc.gestures_enabled else None)
    svc.start_watchdog()
    svc.publish_status(time.time(), force=True)

    while not _stop:
        now = time.time()
        svc.loop_tick = time.monotonic()
        req = svc.next_request()
        if req is not None:
            svc.serve(req)
            now = time.time()

        # A viewer on the live page is a wake reason in its own right, ranked
        # below a capture request only because requests are served first.
        live = svc.watching(now)
        if live and not svc.sensor.is_open and now - svc._last_wake_try > 2.0:
            # Throttled: if the sensor is muted or broken, an unthrottled retry
            # would spin at the poll rate and fill the journal with the same
            # line forever.
            svc._last_wake_try = now
            svc.ensure_awake()
        svc.apply_live_tuning(live)
        svc.apply_tracking(live)

        svc.pump_frames(now)

        if (svc.sensor.is_open and not live
                and now - svc.last_request > svc.idle_timeout):
            svc.sleep_sensor()

        svc.sweep(now)
        svc.publish_status(now)
        # While streaming the loop must tick faster than the frame period or
        # the delivered rate quietly lands at half of STREAM_FPS.
        time.sleep(POLL_STREAM if live and svc.sensor.is_open
                   else POLL_AWAKE if svc.sensor.is_open else POLL_ASLEEP)

    if svc.stream_srv is not None:
        svc.stream_srv.stop()
    svc.sleep_sensor()
    svc.state = "off"
    svc.publish_status(time.time(), force=True)
    print("[camera] stopped", flush=True)
    return 0


def _capture_test(svc: Service) -> int:
    """Cold vs warm, measured. These numbers belong in docs/CAMERA.md."""
    t0 = time.time()
    if not svc.ensure_awake():
        print(f"FAIL {svc.sensor.last_error}")
        return 1
    got = svc.sensor.grab()
    if got is None:
        print("FAIL no frame")
        return 1
    jpeg, size, q = encode.to_jpeg(got[0], "normal")
    cold = time.time() - t0
    print(f"cold  {1000*cold:7.1f} ms   {size[0]}x{size[1]} q{q} {len(jpeg):,} B"
          f"  base64 {4*((len(jpeg)+2)//3):,}")

    for label, profile in (("warm normal", "normal"), ("warm fine", "fine")):
        t = time.time()
        got = svc.sensor.grab()
        jpeg, size, q = encode.to_jpeg(got[0], profile)
        print(f"{label:12s} {1000*(time.time()-t):7.1f} ms   "
              f"{size[0]}x{size[1]} q{q} {len(jpeg):,} B"
              f"  base64 {4*((len(jpeg)+2)//3):,}")

    print(f"sensor powered during: {protocol.sensor_powered()}")
    svc.sleep_sensor()
    time.sleep(6.0)     # past the 5 s autosuspend delay
    print(f"sensor powered 6s after close: {protocol.sensor_powered()}")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        sys.exit(0)
