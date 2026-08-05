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


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--frames", type=int, default=30,
                    help="frames per throughput measurement")
    return probe(ap.parse_args().frames)


if __name__ == "__main__":
    raise SystemExit(main())
