#!/usr/bin/env python3
"""Verify the camera and MEASURE what it can actually deliver.

Companion to tools/bench_spi.py, and written for the same reason: the numbers
this project relies on come from counters, not from datasheets. A sensor that
advertises 120 fps tells you nothing about what arrives in a numpy array on
this Pi at this resolution.

What it checks, in order, so a failure says WHERE it failed:

  1. the camera is detected at all           (libcamera enumeration)
  2. frames actually arrive                  (a capture returns the right shape)
  3. the sensor is LIVE, not a dead feed     (see below)
  4. how fast frames arrive, and what it costs in CPU

Step 3 is the one worth explaining. A dark room and a disconnected ribbon both
produce a black frame, and "I got an image" is not evidence the sensor is
working. The test therefore looks for what only live silicon produces:

  * SPATIAL noise   -- read noise means the frame is never perfectly uniform
  * TEMPORAL noise  -- two successive frames are never bit-identical

Both hold in a pitch-dark room, which is the point: this has to be verifiable
at 4am with the lights off. Temporal difference is the stronger signal, because
a frozen or replayed buffer can still have a plausible spatial histogram but
cannot differ from itself.

A gain-response check (raise gain, expect more noise) is reported for interest
but NOT used as the pass criterion -- measured on this sensor in darkness the
ratio was only 1.17x, far too thin a margin to gate on.

Run:  python3 tools/camera_probe.py
"""

from __future__ import annotations

import argparse
import os
import sys
import time

HZ = os.sysconf("SC_CLK_TCK")

# Preview sizes worth knowing about. The panel body is 480x232, so the first
# entry is what a full-width panel preview would need.
SIZES = [(480, 272), (640, 360), (1536, 864)]


def _cpu() -> float:
    f = open("/proc/self/stat").read().split()
    return (int(f[13]) + int(f[14])) / HZ


def _quiet():
    """libcamera logs several INFO lines per configure() straight to stderr."""
    os.environ.setdefault("LIBCAMERA_LOG_LEVELS", "*:ERROR")


def probe(frames: int = 30) -> int:
    _quiet()
    try:
        from picamera2 import Picamera2
    except Exception as e:                                   # pragma: no cover
        print(f"FAIL  picamera2 not importable: {e}")
        print("      apt install python3-picamera2")
        return 1

    cams = Picamera2.global_camera_info()
    if not cams:
        print("FAIL  no camera detected")
        print("      check the ribbon and `camera_auto_detect=1` in config.txt")
        return 1
    for c in cams:
        print(f"camera: {c.get('Model')}  at {c.get('Id')}")

    # --- liveness: noise must respond to gain --------------------------------
    import numpy as np

    def grab(gain: float, n: int = 1):
        p = Picamera2()
        p.configure(p.create_still_configuration(
            main={"size": (1536, 864), "format": "RGB888"}))
        if gain > 1:
            p.set_controls({"AnalogueGain": gain, "ExposureTime": 500_000})
        p.start()
        time.sleep(1.5)                      # let AE/AWB settle
        out = [p.capture_array("main").copy() for _ in range(n)]
        p.stop()
        p.close()
        return out

    a, b = grab(1.0, 2)
    spatial = float(a.std())
    temporal = float(np.abs(a.astype(np.int16) - b.astype(np.int16)).mean())
    print(f"spatial noise (std within a frame):  {spatial:6.3f}")
    print(f"temporal noise (mean |frame1-frame2|): {temporal:6.3f}")

    if spatial <= 0.05:
        print("FAIL  frame is perfectly uniform -- not a live sensor")
        return 1
    if temporal <= 0.01:
        print("FAIL  two captures are identical -- frozen or replayed buffer")
        return 1
    print("PASS  sensor is live (spatial + temporal noise both present)")

    hi = grab(16.0)[0]
    print(f"  fyi gain 1x/16x mean: {a.mean():.2f} / {hi.mean():.2f} "
          f"(informational, not a pass criterion)")

    # --- throughput ----------------------------------------------------------
    for size in SIZES:
        p = Picamera2()
        p.configure(p.create_video_configuration(
            main={"size": size, "format": "RGB888"}))
        p.start()
        time.sleep(1.0)
        p.capture_array("main")              # warm-up, not measured
        c0, t0 = _cpu(), time.time()
        for _ in range(frames):
            p.capture_array("main")
        dt, dc = time.time() - t0, _cpu() - c0
        p.stop()
        p.close()
        print(f"{size[0]:>5}x{size[1]:<5} {frames/dt:6.2f} fps  "
              f"{1000*dt/frames:6.1f} ms/frame  CPU {100*dc/dt:5.1f}%")

    # The camera is not the bottleneck for anything shown on the panel.
    # docs/HARDWARE.md: the SPI bus carries 2,562,838 B/s and one 480x232 frame
    # costs 246,499 B transmitted -- a ceiling of 10.37 fps. Any preview is
    # bus-limited long before it is camera-limited.
    print("\nnote: panel ceiling is 10.37 fps (SPI), so a preview is "
          "bus-limited, not camera-limited")
    return 0


def sensor_power_path(sensor: str = "imx708") -> str | None:
    """Path to the sensor's runtime-PM status, resolved BY DEVICE NAME.

    WHY NOT OPEN FILE DESCRIPTORS -- measured 2026-08-05, do not retry this.
    The obvious "is the camera in use" probe is to look for a process holding
    an fd on the capture node. It does not work on this Pi, for two reasons
    that compound:

      1. libcamera never opens /dev/video0 (rp1-cfe-csi2_ch0) at all. It opens
         /dev/media0-1, /dev/v4l-subdev0-3, /dev/video1,4,6,7 and the whole
         /dev/video20-27 pispbe range.
      2. pipewire and wireplumber hold EXACTLY THAT SAME SET, permanently,
         from boot, on an idle system -- including v4l-subdev2, the imx708.

    So fd presence cannot distinguish "the camera is streaming" from "the
    desktop session manager enumerated the device once at boot". An indicator
    built on it would read "camera in use" 24 hours a day, which is worse than
    having none: it trains the owner to ignore it.

    The sensor's runtime power state does work. It is suspended on an idle
    system despite those open fds, and goes active only when someone actually
    streams. It is a fact about whether the hardware is powered, published by
    the kernel, and nothing in userspace can forge it.
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


def sensor_powered(path: str | None) -> bool | None:
    """True = powered, False = suspended, None = cannot tell.

    None matters: the caller must treat "cannot tell" as "assume ON". For a
    privacy indicator the fail-safe direction is inverted from everything else
    in this project -- never claim the camera is off unless positively
    observed off.

    Note the reading LAGS by up to autosuspend_delay_ms (5000 ms here): the
    sensor stays active for ~5 s after streaming stops. That lag is in the
    safe direction, so it is left alone rather than tuned.
    """
    if not path:
        return None
    try:
        return open(path).read().strip() == "active"
    except OSError:
        return None


def profile() -> int:
    """Measure the numbers Phase 1 of the camera design depends on.

    Everything here exists because the alternative was to guess. See
    docs/CAMERA.md -- the numbers this prints are the ones that belong there.
    """
    _quiet()
    import io
    import numpy as np
    from PIL import Image
    from picamera2 import Picamera2

    pw = sensor_power_path()
    print(f"sensor power file: {pw or 'NOT FOUND'}")
    print(f"  at rest:         powered={sensor_powered(pw)}  (expect False)")

    # --- sensor mode / field of view -------------------------------------
    # The mode table advertises 1536x864 as a (768,432)/3072x1728 crop -- a
    # NARROWER field of view than the full-frame modes. If libcamera silently
    # picks it for a 1024x576 request we lose the sides of the room, which is
    # exactly what "what am I doing" needs.
    print("\n-- sensor mode selection for a 1024x576 request --")
    for forced in (None, (2304, 1296)):
        p = Picamera2()
        cfg = p.create_video_configuration(main={"size": (1024, 576), "format": "RGB888"})
        if forced:
            cfg["sensor"] = {"output_size": forced, "bit_depth": 10}
        p.configure(cfg)
        sensor = p.camera_configuration().get("sensor", {})
        scaler = p.camera_ctrl_info.get("ScalerCrop")
        p.start(); time.sleep(0.6)
        md = p.capture_metadata()
        p.stop(); p.close()
        label = "auto" if forced is None else f"forced {forced[0]}x{forced[1]}"
        print(f"  {label:22s} sensor={sensor.get('output_size')} "
              f"ScalerCrop={md.get('ScalerCrop')}")
    del scaler

    # --- cold wake, autofocus, and the settle that dominates it -----------
    print("\n-- cold wake (open -> first trustworthy frame) --")
    t0 = time.time()
    p = Picamera2()
    p.configure(p.create_video_configuration(
        main={"size": (1024, 576), "format": "RGB888"},
        controls={"FrameDurationLimits": (33333, 66666)}))
    t_cfg = time.time()
    p.set_controls({"AfMode": 2, "AeEnable": True})     # 2 = Continuous
    p.start()
    t_start = time.time()

    af_state, af_at, lux = None, None, None
    deadline = time.time() + 6.0
    while time.time() < deadline:
        md = p.capture_metadata()
        af_state, lux = md.get("AfState"), md.get("Lux")
        if af_state == 2:                                # 2 = Focused
            af_at = time.time()
            break
    exp, gain = md.get("ExposureTime"), md.get("AnalogueGain")
    print(f"  construct+configure {1000*(t_cfg-t0):7.0f} ms")
    print(f"  start()             {1000*(t_start-t_cfg):7.0f} ms")
    print(f"  AF -> Focused       "
          f"{f'{1000*(af_at-t_start):7.0f} ms' if af_at else '  NOT REACHED (6s)'}"
          f"   AfState={af_state}")
    print(f"  total cold wake     {1000*((af_at or time.time())-t0):7.0f} ms")
    print(f"  scene: Lux={lux}  ExposureTime={exp} us  AnalogueGain={gain}")
    if exp and exp > 66666:
        print("  WARNING exposure exceeds the 66 ms cap -> moving hands will smear")

    print(f"  powered while streaming: {sensor_powered(pw)}  (expect True)")

    # --- encode profiles: the real bytes, for THIS room -------------------
    print("\n-- encode profiles (real bytes for the current scene) --")
    frame = p.capture_array("main")
    results = {}
    for name, size, q in (("normal", (768, 432), 72), ("fine", (1024, 576), 78)):
        t0 = time.time()
        img = Image.fromarray(frame)
        if img.size != size:
            img = img.resize(size, Image.BILINEAR)
        buf = io.BytesIO()
        img.save(buf, format="JPEG", quality=q, optimize=False)
        dt = 1000 * (time.time() - t0)
        n = buf.tell()
        b64 = 4 * ((n + 2) // 3)
        results[name] = n
        print(f"  {name:7s} {size[0]}x{size[1]:<4} q{q}  {n:7,} B  "
              f"-> base64 {b64:7,}  encode+resize {dt:5.1f} ms")

    p.stop()
    p.close()
    # autosuspend_delay_ms is 5000; wait past it so the reading is meaningful.
    time.sleep(6.0)
    print(f"\n  powered 6s after close:  {sensor_powered(pw)}  (expect False)")

    print("\nPut these numbers in docs/CAMERA.md. They are the budget.")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--frames", type=int, default=30,
                    help="frames per throughput measurement")
    ap.add_argument("--profile", action="store_true",
                    help="measure the numbers the camera design depends on")
    args = ap.parse_args()
    return profile() if args.profile else probe(args.frames)


if __name__ == "__main__":
    raise SystemExit(main())
