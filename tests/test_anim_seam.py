"""Animation loops must close exactly.

A pack is played on repeat; if frame N is not identical to frame 0, the wrap
shows as the rings snapping back a few degrees, once per loop. That is a
periodic visual glitch that is easy to introduce and easy to miss in code
review, so it is asserted here.

The rule: every animated term in render_frame() must advance by an INTEGER
number of cycles across the loop. Float multipliers (spin * 1.3, phase * 2)
leave a remainder at the wrap and WILL fail this test.

Run:  python3 tests/test_anim_seam.py
"""
import os
import sys

import numpy as np

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "tools"))
from render_frames import PACKS, render_frame  # noqa: E402

# Small frame: the seam is a property of the maths, not the resolution.
W, H, D = 120, 58, 56


def test_every_pack_loops_seamlessly():
    for name, (style, frames, fps, loop) in PACKS.items():
        if not loop:
            continue
        first = render_frame(style, 0.0, W, H, D)
        wrap = render_frame(style, 1.0, W, H, D)   # u=1 must reproduce u=0
        delta = float(np.abs(first - wrap).max())
        assert delta < 1e-6, f"{name}: seam of {delta:.3e} at the loop wrap"


def test_cycle_counts_are_integers():
    """Guards the rule directly, so a float sneaking in fails loudly here
    rather than as a subtle visual artefact on the panel."""
    for name, (style, *_rest) in PACKS.items():
        for attr in ("ring_cycles", "mesh_cycles", "pulse_cycles",
                     "wobble_cycles", "tick_cycles"):
            v = getattr(style, attr)
            assert isinstance(v, int), f"{name}.{attr} = {v!r} is not an int"


if __name__ == "__main__":
    fails = 0
    for name, (style, frames, fps, loop) in PACKS.items():
        d = float(np.abs(render_frame(style, 0.0, W, H, D)
                         - render_frame(style, 1.0, W, H, D)).max())
        ok = d < 1e-6
        fails += not ok
        print(f"  {'PASS' if ok else 'FAIL'}  {name:13s} seam = {d:.2e}")
    try:
        test_cycle_counts_are_integers(); print("  PASS  cycle counts are integers")
    except AssertionError as e:
        fails += 1; print(f"  FAIL  {e}")
    print(f"\n{'ALL PASS' if not fails else f'{fails} FAILURES'}")
    sys.exit(1 if fails else 0)
