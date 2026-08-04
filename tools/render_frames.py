#!/usr/bin/env python3
"""Generate the JARVIS visual as pre-rendered RGB565 animation packs.

WHY PRE-RENDER
Playback must cost almost nothing. The SPI bus is saturated at ~1.43 MB/s
(measured, docs/HARDWARE.md), so every CPU cycle spent deriving pixels is a
cycle stolen from a Pi that also runs the agent. A `.pack` stores frames in the
panel's native RGB565, so playing one is a memcpy into the framebuffer -- no
decode, no compositing, no per-frame allocation.

WHY PROCEDURAL RATHER THAN DRAWN
Eight states x ~60 frames is ~480 images. Hand-drawing that is not maintainable
by one person, and every palette or timing tweak would mean redrawing. Here a
change is an edit and a re-run. These are still real rendered frames -- they
are generated, not faked.

WHY FULL WIDTH, BOUNDED HEIGHT
Measured (tools/bench_spi.py): fbtft is ROW-granular, not rectangle-granular.
A 240x240 centred blit transmits 228.8 KiB -- the whole 480-wide rows -- and a
480x240 blit costs the same (ratio 1.00x). Narrowing a region buys nothing.

So frames are FULL WIDTH (free) and bounded in HEIGHT (the only thing that
costs). 480x240 = 240 dirty rows ~= 6.3 fps at 16 MHz, ~12.6 at 32 MHz. The
extra width is spent on peripheral detail that would otherwise be black.

Format (see pack_format.md):
    <name>.pack  raw little-endian RGB565, frames * h * w * 2 bytes
    <name>.json  {name,w,h,frames,fps,loop,origin:[x,y]}

Usage:
    python3 tools/render_frames.py --out assets/anim
"""

from __future__ import annotations

import argparse
import json
import math
from dataclasses import dataclass
from pathlib import Path

import numpy as np

WIDTH = 480         # full panel width -- costs nothing extra (row-granular)
HEIGHT = 232        # dirty rows; THIS is what sets the frame rate
DIAM = 224          # diameter of the central visual within that band
SUPERSAMPLE = 2     # render at 2x and box-filter down: cheap anti-aliasing,
                    # which matters because RGB565 banding makes hard edges
                    # look worse than they would on a 24-bit panel.


@dataclass
class Style:
    """A state's look. Colours are RGB floats 0..1 before quantisation.

    SEAMLESS LOOPING IS STRUCTURAL, NOT LUCK.
    Every animated term below advances by an INTEGER number of cycles across
    the pack. Frame `frames` is therefore identical to frame 0, so wrapping is
    invisible.

    The first version used float multipliers (spin * 1.3, phase * 2, ...). Those
    leave a fractional remainder at the wrap, and the visible symptom is exactly
    what it sounds like: the rings snap back a few degrees once per loop. Any
    new animated term MUST be expressed as an integer cycle count here.
    """
    core: tuple[float, float, float] = (0.00, 0.90, 1.00)   # #00E5FF cyan
    ring: tuple[float, float, float] = (0.00, 0.70, 0.85)
    mesh: tuple[float, float, float] = (0.00, 0.45, 0.60)
    intensity: float = 1.0

    # Integer cycles completed over one loop. Higher = faster motion.
    # Direction is handled per-ring so adjacent rings counter-rotate.
    ring_cycles: int = 1     # rotations of the ring gating
    mesh_cycles: int = 1     # rotations of the radial mesh
    pulse_cycles: int = 2    # core breaths
    wobble_cycles: int = 1   # ring-radius wobble
    tick_cycles: int = 1     # peripheral tick rotation

    mesh_density: int = 24   # radial spokes (angular symmetry order)
    rings: int = 3


CYAN = Style()
AMBER = Style(core=(1.00, 0.70, 0.00), ring=(0.85, 0.55, 0.00), mesh=(0.55, 0.35, 0.00))
RED = Style(core=(1.00, 0.25, 0.25), ring=(0.80, 0.15, 0.15), mesh=(0.45, 0.10, 0.10))


def _radial_grid(w: int, h: int, diam: float) -> tuple[np.ndarray, np.ndarray]:
    """Radius (1.0 at the visual's edge) and angle, over a w x h frame.

    Non-square aware: both axes are scaled by the same `diam` so the visual
    stays circular on a wide frame instead of stretching into an ellipse.
    """
    gx = (np.arange(w) - (w - 1) / 2.0) / (diam / 2.0)
    gy = (np.arange(h) - (h - 1) / 2.0) / (diam / 2.0)
    yy, xx = np.meshgrid(gy, gx, indexing="ij")
    return np.hypot(xx, yy), np.arctan2(yy, xx)


def _ring(r: np.ndarray, radius: float, width: float) -> np.ndarray:
    """Soft annulus. Gaussian rather than hard-edged: a 1px edge on a 16-bit
    panel aliases badly, and the glow is what reads as 'volumetric'."""
    return np.exp(-((r - radius) ** 2) / (2 * width * width))


def render_frame(style: Style, u: float, w: int, h: int, diam: float) -> np.ndarray:
    """One frame at loop position u in [0,1). Returns (h, w, 3) float 0..1.

    u is the fraction THROUGH THE LOOP, not an absolute time. Every term below
    multiplies u by an integer cycle count and 2*pi, so u=1 reproduces u=0
    exactly and the wrap is invisible.
    """
    r, a = _radial_grid(w, h, diam)
    acc = np.zeros((h, w, 3), np.float32)
    TAU = 2 * math.pi

    # --- concentric rings, counter-rotating -----------------------------
    for i in range(style.rings):
        frac = (i + 1) / (style.rings + 1)
        radius = 0.35 + frac * 0.55

        # Integer cycles; the constant `i` is a fixed phase offset, which does
        # not affect seamlessness.
        wobble = 0.012 * math.sin(TAU * style.wobble_cycles * (1 + i) * u + i)
        band = _ring(r, radius + wobble, 0.022 + 0.006 * i)

        # Rotating angular gaps make the spin legible; a perfectly symmetric
        # ring looks static however fast it turns.
        direction = 1 if i % 2 == 0 else -1
        gate = 0.55 + 0.45 * np.cos(
            a * (3 + i) + direction * TAU * style.ring_cycles * (i + 1) * u
        )
        acc += (band * gate * (0.9 - 0.15 * i))[..., None] * np.array(style.ring, np.float32)

    # --- radial mesh: the 'web' ----------------------------------------
    spokes = 0.5 + 0.5 * np.cos(a * style.mesh_density - TAU * style.mesh_cycles * u)
    spokes = spokes ** 6                       # sharpen into discrete lines
    envelope = np.clip(1.0 - np.abs(r - 0.62) / 0.42, 0, 1) ** 1.5
    acc += (spokes * envelope * 0.5)[..., None] * np.array(style.mesh, np.float32)

    # --- core -----------------------------------------------------------
    breathe = 1.0 + 0.18 * math.sin(TAU * style.pulse_cycles * u)
    core = np.exp(-(r ** 2) / (2 * (0.085 * breathe) ** 2))
    halo = np.exp(-(r ** 2) / (2 * 0.26 ** 2)) * 0.35
    acc += ((core + halo) * 1.15)[..., None] * np.array(style.core, np.float32)

    # --- peripheral ticks: free real estate ------------------------------
    # These rows are transmitted whether or not anything is drawn in them, so
    # the outer width costs nothing. Sparse arc segments either side give the
    # visual scale and make the rotation readable at a glance.
    outer = np.exp(-((r - 1.45) ** 2) / (2 * 0.05 ** 2))
    ticks = (0.5 + 0.5 * np.cos(a * 48 + TAU * style.tick_cycles * u)) ** 12
    lateral = np.clip(np.abs(np.cos(a)) - 0.55, 0, 1) / 0.45   # sides only
    acc += (outer * ticks * lateral * 0.55)[..., None] * np.array(style.mesh, np.float32)

    # Vignette relative to the visual, not the frame, so the wide black
    # surround stays black instead of picking up a rectangular glow.
    acc *= np.clip(1.30 - r * 0.62, 0, 1)[..., None]

    return np.clip(acc * style.intensity, 0, 1)


def render_pack(style: Style, frames: int, w: int = WIDTH, h: int = HEIGHT,
                diam: int = DIAM) -> np.ndarray:
    """Render `frames` frames as (frames, h, w, 3) uint8, seamlessly looping.

    u = f/frames spans [0,1), so with integer cycle counts frame `frames`
    would equal frame 0 -- the loop closes exactly.
    """
    S = SUPERSAMPLE
    out = np.zeros((frames, h, w, 3), np.uint8)
    for f in range(frames):
        img = render_frame(style, f / frames, w * S, h * S, diam * S)
        img = img.reshape(h, S, w, S, 3).mean(axis=(1, 3))   # box-filter 2x -> 1x
        out[f] = (img * 255).astype(np.uint8)
    return out


def to_rgb565(frames: np.ndarray) -> np.ndarray:
    """(F,H,W,3) uint8 -> (F,H,W) little-endian uint16, matching panel.py."""
    r = (frames[..., 0].astype(np.uint16) >> 3) << 11
    g = (frames[..., 1].astype(np.uint16) >> 2) << 5
    b = frames[..., 2].astype(np.uint16) >> 3
    return (r | g | b).astype("<u2")


def write_pack(out: Path, name: str, frames: np.ndarray, fps: int, loop: bool,
               origin: tuple[int, int]) -> int:
    packed = to_rgb565(frames)
    blob = out / f"{name}.pack"
    blob.write_bytes(packed.tobytes())
    (out / f"{name}.json").write_text(json.dumps({
        "name": name,
        "w": int(frames.shape[2]),
        "h": int(frames.shape[1]),
        "frames": int(frames.shape[0]),
        "fps": fps,
        "loop": loop,
        "origin": list(origin),
        "format": "rgb565le",
    }, indent=2))
    return blob.stat().st_size


# Per-state packs. Frame counts are deliberately modest: at ~10 fps a 48-frame
# loop is ~5 s, long enough not to read as repetitive, small enough that eight
# packs stay well inside RAM.
# fps is the MEASURED ceiling for 232 dirty rows (docs/HARDWARE.md), not a
# wish: asking for more than the bus delivers makes the loop uneven rather
# than faster. Frame counts are generous because more frames means a smaller
# angular step per frame -- the main lever on perceived smoothness once fps is
# fixed by hardware.
_FPS = 6          # 16 MHz ceiling. Raise to ~12 after the 32 MHz change.

PACKS = {
    #  name          style                                                    frames fps   loop
    "idle":         (Style(intensity=0.75, ring_cycles=1, mesh_cycles=1,
                           pulse_cycles=2, wobble_cycles=1, tick_cycles=1),      72, _FPS, True),
    "receiving":    (Style(intensity=1.00, ring_cycles=2, mesh_cycles=2,
                           pulse_cycles=4, wobble_cycles=2, tick_cycles=2),      48, _FPS, True),
    "thinking":     (Style(intensity=1.00, ring_cycles=3, mesh_cycles=3,
                           pulse_cycles=3, wobble_cycles=2, tick_cycles=3,
                           mesh_density=32, rings=4),                            60, _FPS, True),
    "tool_use":     (Style(intensity=1.00, ring_cycles=2, mesh_cycles=4,
                           pulse_cycles=2, wobble_cycles=2, tick_cycles=4,
                           mesh_density=40, rings=4),                            60, _FPS, True),
    "responding":   (Style(intensity=1.10, ring_cycles=1, mesh_cycles=2,
                           pulse_cycles=4, wobble_cycles=1, tick_cycles=1),      48, _FPS, True),
    "reconnecting": (Style(**{**AMBER.__dict__, "intensity": 0.85,
                              "ring_cycles": 1, "mesh_cycles": 1,
                              "pulse_cycles": 4, "wobble_cycles": 3}),           48, _FPS, True),
    "offline":      (Style(**{**RED.__dict__, "intensity": 0.45, "rings": 2,
                              "ring_cycles": 1, "mesh_cycles": 0,
                              "pulse_cycles": 1, "wobble_cycles": 1,
                              "tick_cycles": 0}),                                72, _FPS, True),
    "error":        (Style(**{**RED.__dict__, "intensity": 0.95, "rings": 2,
                              "ring_cycles": 0, "mesh_cycles": 0,
                              "pulse_cycles": 6, "wobble_cycles": 0,
                              "tick_cycles": 0}),                                36, _FPS, True),
}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default="assets/anim")
    ap.add_argument("--width", type=int, default=WIDTH)
    ap.add_argument("--height", type=int, default=HEIGHT)
    ap.add_argument("--diam", type=int, default=DIAM)
    ap.add_argument("--only", help="render just this pack")
    args = ap.parse_args()

    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    # Centred horizontally; vertically inside the body zone (header is 30px).
    # Full width, sitting directly under the 26px header.
    origin = ((480 - args.width) // 2, 28)

    total = 0
    for name, (style, frames, fps, loop) in PACKS.items():
        if args.only and name != args.only:
            continue
        arr = render_pack(style, frames, args.width, args.height, args.diam)
        size = write_pack(out, name, arr, fps, loop, origin)
        total += size
        print(f"  {name:13s} {frames:3d} frames @ {fps:2d}fps  {size/1024:7.1f} KiB")
    print(f"\ntotal {total/1024/1024:.2f} MiB in {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
