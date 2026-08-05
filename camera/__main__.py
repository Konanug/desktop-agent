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
ring of recent frames while awake, and close the sensor once nothing has asked
for a while.
"""

from __future__ import annotations

import argparse
import collections
import json
import os
import signal
import sys
import time
from pathlib import Path

import numpy as np
from PIL import Image

from . import encode, protocol
from .sensor import Sensor

POLL_ASLEEP = 0.10      # invisible against a ~434 ms cold wake
POLL_AWAKE = 0.02
STATUS_PERIOD = 1.0
CAPTURE_TTL = 60.0      # captures are worthless once the turn has moved on

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

    # -- status ---------------------------------------------------------
    def publish_status(self, now: float, force: bool = False) -> None:
        """STATE, NEVER CONTENT. Same rule as state.json."""
        if not force and now - self.last_status < STATUS_PERIOD:
            return
        self.last_status = now
        doc = {
            "schema": protocol.SCHEMA,
            "updated_at": now,
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
        }
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
        self.publish_status(time.time(), force=True)
        print("[camera] sensor closed (idle)", flush=True)

    # -- ring / motion --------------------------------------------------
    def pump_ring(self, now: float) -> None:
        """Keep a decimated history so 'what did I just do' can look BACKWARDS.

        Without this, a question asked after the interesting moment can only
        ever sample forward -- i.e. show the wrong few seconds.
        """
        if not self.sensor.is_open:
            return
        if now - self._last_ring < 1.0 / protocol.RING_HZ:
            return
        got = self.sensor.grab()
        if got is None:
            return
        frame, wall, mono = got
        small = np.asarray(
            Image.fromarray(frame).resize(protocol.RING_SIZE), dtype=np.uint8)
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

    def sweep(self, now: float) -> None:
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
    args = ap.parse_args(argv)

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
    svc.publish_status(time.time(), force=True)

    while not _stop:
        now = time.time()
        req = svc.next_request()
        if req is not None:
            svc.serve(req)
            now = time.time()

        svc.pump_ring(now)

        if svc.sensor.is_open and now - svc.last_request > svc.idle_timeout:
            svc.sleep_sensor()

        svc.sweep(now)
        svc.publish_status(now)
        time.sleep(POLL_AWAKE if svc.sensor.is_open else POLL_ASLEEP)

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
