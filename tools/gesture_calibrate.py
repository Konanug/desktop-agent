#!/usr/bin/env python3
"""Set the pinch thresholds from YOUR hand, at YOUR distance, on YOUR camera.

    python3 tools/gesture_calibrate.py                 # live readout
    python3 tools/gesture_calibrate.py --collect pinch # sample a pose for 8s

WHY THIS EXISTS RATHER THAN A CONSTANT I PICKED
PINCH is the first gesture here that cannot be read off five booleans. It is a
threshold on a continuous ratio, and a threshold is only ever right for the
hand, lens and distance it was measured against. The values shipped in
camera/hands.py were derived from twelve landmark fixtures of OTHER poses --
they establish where "not pinching" sits (0.92-1.09) but say nothing about
where your pinch lands.

The honest way to close that gap is to measure it, which is what this does.

THE TWO NUMBERS, AND WHY BOTH ARE NEEDED

  pinch_ratio  thumb tip -> index tip, over hand scale.  SMALL when pinched.
  index_reach  index tip -> wrist,     over hand scale.  Separates a PINCH
               from a FIST, which also brings those two tips together. A
               curled hand measures ~0.87; extended fingers 1.84+.

Both are ratios against the hand's own wrist-to-knuckle length, so they do not
change as you move toward or away from the camera. Both are computed with the
frame aspect applied -- without it the same pinch reads 2.6x differently
depending on which way your hand is turned (see camera/hands.py _dist).

Reads the live /hands.json feed, so the camera service keeps sole ownership of
the sensor. Subscribing counts as watching the room, exactly like the stream.
"""

from __future__ import annotations

import argparse
import json
import os
import statistics
import sys
import time
import urllib.error
import urllib.request

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from camera import protocol, stream                          # noqa: E402


def _url(path: str) -> str:
    port = int(os.environ.get("HERMES_CAMERA_STREAM_PORT",
                              str(protocol.STREAM_PORT)))
    tok = stream.load_or_create_token()
    return f"http://127.0.0.1:{port}{path}?k={tok}"


def _poll():
    """One hands.json reading, or None.

    Hitting /snapshot.jpg first would be the obvious way to keep the sensor
    awake, but /hands.json alone does not count as a viewer -- so this holds a
    stream connection open in the background instead. See main().
    """
    try:
        with urllib.request.urlopen(_url("/hands.json"), timeout=2) as r:
            return json.loads(r.read())
    except (urllib.error.URLError, OSError, ValueError):
        return None


def _keepalive():
    """Hold a viewer slot so the sensor stays awake and tracking runs.

    A calibration session is unambiguously watching the room, and it should be
    counted and indicated as such -- the CAM light must be on while this runs.
    """
    import threading

    def run():
        while True:
            try:
                with urllib.request.urlopen(_url("/stream.mjpg"),
                                            timeout=30) as r:
                    while r.read(65536):
                        pass
            except Exception:
                time.sleep(1.0)
    threading.Thread(target=run, daemon=True).start()


def live() -> int:
    print("Hold a pose steady in front of the camera. Ctrl-C to stop.\n")
    print(f"{'hand':6s} {'label':10s} {'pinch':>7s} {'reach':>7s} "
          f"{'fingers':>8s}   verdict")
    print("-" * 62)
    last = None
    while True:
        doc = _poll()
        if not doc:
            time.sleep(0.3)
            continue
        if doc.get("stale") or not doc.get("hands"):
            if last != "none":
                print("   (no hand in view)")
                last = "none"
            time.sleep(0.2)
            continue
        last = None
        for h in doc["hands"]:
            p, r = h.get("pinch_ratio"), h.get("index_reach")
            if p is None:
                print("  this camera service predates pinch -- restart it")
                return 1
            verdict = ("PINCH" if p < __import__("camera.hands",
                                                 fromlist=["x"]).PINCH_MAX
                       and r >= __import__("camera.hands",
                                           fromlist=["x"]).PINCH_MIN_REACH
                       else h.get("gesture") or "-")
            print(f"{h['handedness'][:5]:6s} {h.get('label', '?'):10s} "
                  f"{p:7.3f} {r:7.3f} {h['fingers_up']:8d}   {verdict}")
        time.sleep(0.25)


def collect(pose: str, seconds: float) -> int:
    """Sample one held pose and report its distribution.

    A single reading is not a threshold. What matters is the SPREAD while you
    hold the pose naturally -- if pinching and fisting overlap on these two
    numbers for your hand, no threshold separates them and the answer is a
    different discriminator, not a cleverer cutoff.
    """
    print(f"Hold {pose.upper()} steady for {seconds:.0f}s. Starting in 3s...")
    time.sleep(3)
    pinch, reach, t0 = [], [], time.time()
    while time.time() - t0 < seconds:
        doc = _poll()
        if doc and not doc.get("stale") and doc.get("hands"):
            h = doc["hands"][0]
            if h.get("pinch_ratio") is not None:
                pinch.append(h["pinch_ratio"])
                reach.append(h["index_reach"])
        time.sleep(0.1)
    if len(pinch) < 5:
        print(f"only {len(pinch)} readings -- was a hand in view?")
        return 1

    def stat(name, xs):
        print(f"  {name:12s} n={len(xs):3d}  min {min(xs):.3f}  "
              f"median {statistics.median(xs):.3f}  max {max(xs):.3f}")
    print(f"\n{pose.upper()}:")
    stat("pinch_ratio", pinch)
    stat("index_reach", reach)
    print(f"\nTo use these, set in systemd/hermes-camera.service:")
    print(f"  Environment=HERMES_PINCH_MAX=<between your pinch max and your "
          f"not-pinch min>")
    print(f"  Environment=HERMES_PINCH_MIN_REACH=<below your pinch min "
          f"reach, above a fist's>")
    print("Then: systemctl --user restart hermes-camera")
    return 0


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="gesture_calibrate")
    ap.add_argument("--collect", metavar="POSE",
                    help="sample a held pose (e.g. pinch, fist, open)")
    ap.add_argument("--seconds", type=float, default=8.0)
    args = ap.parse_args(argv)

    _keepalive()
    time.sleep(1.5)              # let the sensor wake and tracking start
    try:
        return collect(args.collect, args.seconds) if args.collect else live()
    except KeyboardInterrupt:
        print()
        return 0


if __name__ == "__main__":
    sys.exit(main())
