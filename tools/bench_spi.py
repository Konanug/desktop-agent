#!/usr/bin/env python3
"""Measure REAL SPI throughput to the panel, and check for bus errors.

WHY THIS EXISTS
Timing framebuffer writes measures memcpy, not the panel. fbtft's deferred I/O
collects dirty pages and flushes them from a workqueue, so an mmap write
returns long before any bit reaches the display -- an early naive benchmark
here reported "2729 MB/s", which is ~1300x the theoretical ceiling of a 16 MHz
SPI bus.

The kernel counts the bytes it actually clocks out, at
/sys/class/spi_master/spi0/spi0.0/statistics/. Sampling bytes_tx across a
wall-clock window measures the real thing.

The `errors` and `timedout` counters are the corruption signal that matters
when raising the clock: visual inspection can miss intermittent glitches, but
these do not.

Usage:
    systemctl --user stop hermes-display     # it also writes to fb0
    python3 tools/bench_spi.py --seconds 10
"""

from __future__ import annotations

import argparse
import subprocess
import sys
import time
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from display.panel import Framebuffer, discover  # noqa: E402

STATS = Path("/sys/class/spi_master/spi0/spi0.0/statistics")


def stat(name: str) -> int:
    try:
        return int((STATS / name).read_text().strip())
    except Exception:
        return -1


def spi_clock_hz() -> int:
    """Configured SPI clock from the device tree (Hz), or -1."""
    for p in Path("/proc/device-tree").rglob("tft35a@0/spi-max-frequency"):
        try:
            return int.from_bytes(p.read_bytes(), "big")
        except Exception:
            pass
    return -1


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--seconds", type=float, default=10.0)
    ap.add_argument("--region", choices=["full", "centre"], default="full",
                    help="full = 480x320; centre = the 260x260 animation zone")
    args = ap.parse_args()

    info = discover()
    hz = spi_clock_hz()
    print(f"panel : {info.name} {info.width}x{info.height} {info.bpp}bpp")
    print(f"clock : {hz/1e6:.1f} MHz (device tree)" if hz > 0 else "clock : unknown")

    if not STATS.exists():
        print(f"ERROR: {STATS} missing -- cannot measure real throughput.")
        return 2

    if args.region == "full":
        w, h, x, y = info.width, info.height, 0, 0
    else:
        w = h = 260
        x, y = (info.width - w) // 2, (info.height - h) // 2
    frame_bytes = w * h * 2
    print(f"region: {w}x{h} at ({x},{y}) = {frame_bytes/1024:.1f} KiB/frame\n")

    # Two frames that differ in EVERY pixel, so fbtft cannot shortcut the
    # dirty-region calculation and under-report the work.
    a = np.zeros((h, w, 3), np.uint8); a[:, :, 2] = 255
    b = np.zeros((h, w, 3), np.uint8); b[:, :, 0] = 255

    with Framebuffer(info) as fb:
        fb.blit(a, x, y)
        time.sleep(0.5)                       # let any backlog drain

        b0, m0, e0, t0 = stat("bytes_tx"), stat("messages"), stat("errors"), stat("timedout")
        wall0 = time.perf_counter()
        writes = 0
        while time.perf_counter() - wall0 < args.seconds:
            fb.blit(b if writes % 2 else a, x, y)
            writes += 1
            # Pace to the driver's deferred-IO rate (fps=31). Spinning flat out
            # just overwrites RAM the workqueue has not shipped yet, inflating
            # the write count without moving more bytes.
            time.sleep(1 / 60)
        wall = time.perf_counter() - wall0
        b1, m1, e1, t1 = stat("bytes_tx"), stat("messages"), stat("errors"), stat("timedout")

    tx = b1 - b0
    print(f"wall time      : {wall:.2f} s")
    print(f"mmap writes    : {writes}  ({writes/wall:.1f}/s)  <- RAM only, NOT the panel")
    print(f"SPI bytes_tx   : {tx:,} B  ({tx/wall/1e6:.3f} MB/s)")
    print(f"SPI messages   : {m1-m0:,}")
    if hz > 0:
        print(f"bus utilisation: {(tx*8)/wall/hz*100:.1f}% of {hz/1e6:.1f} MHz")
    print(f"\nEFFECTIVE PANEL REFRESH: {tx/frame_bytes/wall:.1f} fps "
          f"for a {w}x{h} region")

    print(f"\nerrors  : {e0} -> {e1}   {'OK' if e1 == e0 else '*** INCREASED ***'}")
    print(f"timedout: {t0} -> {t1}   {'OK' if t1 == t0 else '*** INCREASED ***'}")
    dmesg = subprocess.run(
        ["dmesg", "--level=err,warn", "--since", "-2min"],
        capture_output=True, text=True).stdout
    hits = [l for l in dmesg.splitlines() if "spi" in l.lower() or "fb" in l.lower()]
    print(f"dmesg spi/fb warnings (2min): {len(hits)}")
    for l in hits[:5]:
        print("  " + l)
    return 0


if __name__ == "__main__":
    sys.exit(main())
