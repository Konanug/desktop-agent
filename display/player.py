"""Plays pre-rendered RGB565 animation packs.

Packs are mmap'd, not read. Two consequences that matter on a Pi:

  * The kernel page cache backs them, so ~28 MiB of frames costs almost nothing
    resident and pages in on demand.
  * A frame is already in the panel's exact pixel format, so playback is
    `framebuffer[rect] = pack[frame]` -- a memcpy. No decode, no compositing,
    no per-frame allocation. The CPU is nearly idle while the SPI bus, which is
    the real bottleneck, stays fed.

Frame pacing is wall-clock based rather than a frame counter, so a late tick
skips ahead instead of playing the animation in slow motion.
"""

from __future__ import annotations

import json
import mmap
import os
import time
from pathlib import Path

import numpy as np

# Seconds to cross-dissolve when the state changes. Long enough to read as a
# transition, short enough that the panel is never lying about the current
# state for a noticeable time -- the new state is on screen within ~a third of
# a second, it just arrives instead of cutting.
FADE = 0.35


def blend565(a: np.ndarray, b: np.ndarray, t: float) -> np.ndarray:
    """Cross-dissolve two RGB565 frames, t=0 -> a, t=1 -> b.

    Blending has to happen per CHANNEL, not on the packed uint16: the packed
    value is three bit-fields, so interpolating it as a single number bleeds
    blue into green into red and produces colour garbage mid-fade.
    """
    out = np.zeros_like(a)      # zeros, not empty: we OR into this
    for shift, mask in ((11, 0x1F), (5, 0x3F), (0, 0x1F)):
        ca = ((a >> shift) & mask).astype(np.float32)
        cb = ((b >> shift) & mask).astype(np.float32)
        c = (ca + (cb - ca) * t + 0.5).astype(np.uint16) & mask
        out |= c << shift
    return out


class Pack:
    def __init__(self, path: Path):
        meta = json.loads(path.with_suffix(".json").read_text())
        self.name: str = meta["name"]
        self.w: int = int(meta["w"])
        self.h: int = int(meta["h"])
        self.frames: int = int(meta["frames"])
        self.fps: float = float(meta["fps"])
        self.loop: bool = bool(meta.get("loop", True))
        self.origin: tuple[int, int] = tuple(meta.get("origin", (0, 0)))  # type: ignore

        expect = self.frames * self.h * self.w * 2
        actual = path.stat().st_size
        if actual != expect:
            raise ValueError(f"{path.name}: {actual}B on disk, metadata implies {expect}B")

        self._fd = os.open(path, os.O_RDONLY)
        try:
            self._mm = mmap.mmap(self._fd, 0, mmap.MAP_PRIVATE, mmap.PROT_READ)
        except Exception:
            os.close(self._fd)
            raise
        self._a = np.frombuffer(self._mm, dtype="<u2").reshape(self.frames, self.h, self.w)

    def frame(self, i: int) -> np.ndarray:
        return self._a[i % self.frames]

    @property
    def nbytes_per_frame(self) -> int:
        return self.w * self.h * 2

    def close(self) -> None:
        try:
            self._a = None  # type: ignore[assignment]
        except Exception:
            pass
        try:
            self._mm.close()
        except BufferError:
            pass
        finally:
            os.close(self._fd)


class Player:
    """Owns the pack set and decides which frame is due."""

    def __init__(self, pack_dir: Path):
        self.dir = Path(pack_dir)
        self._packs: dict[str, Pack] = {}
        self._current: Pack | None = None
        self._started = 0.0
        self._last_index = -1
        self.missing: set[str] = set()
        # Cross-dissolve bookkeeping: the last frame actually shown, and the
        # deadline until which it is still being blended into the new pack.
        self._last_frame: np.ndarray | None = None
        self._fade_from: np.ndarray | None = None
        self._fade_until = 0.0

    def available(self) -> bool:
        return self.dir.is_dir() and any(self.dir.glob("*.pack"))

    def get(self, name: str) -> Pack | None:
        if name in self._packs:
            return self._packs[name]
        if name in self.missing:
            return None
        p = self.dir / f"{name}.pack"
        if not p.exists():
            self.missing.add(name)
            return None
        try:
            self._packs[name] = Pack(p)
            return self._packs[name]
        except Exception as e:
            print(f"[player] cannot load {name}: {e}", flush=True)
            self.missing.add(name)
            return None

    def select(self, name: str, now: float | None = None) -> bool:
        """Switch packs. Returns True if the active pack changed.

        Starts a cross-dissolve from whatever was last on screen. Without it a
        state change is a hard cut between two unrelated visuals -- different
        ring counts, intensities and hues -- which reads as the panel glitching
        rather than as Hermes moving to a new state.
        """
        pack = self.get(name)
        if pack is None or pack is self._current:
            return False
        now = now or time.time()
        if self._current is not None and self._last_frame is not None:
            # Copy: the source may be an mmap view that the old pack owns.
            self._fade_from = self._last_frame.copy()
            self._fade_until = now + FADE
        self._current = pack
        self._started = now
        self._last_index = -1
        return True

    def next_due(self, now: float | None = None) -> float | None:
        """Wall-clock time the next frame is due, so the caller can sleep to it.

        A fixed poll interval cannot express a frame period that is not a
        multiple of it: at 30 Hz polling and 12 fps, frames were measured
        displaying for 90/90/90/60 ms instead of a steady 83.3 ms, and the
        short one reads as a hitch four times a second.
        """
        if self._current is None:
            return None
        now = now or time.time()
        p = self._current
        idx = int((now - self._started) * p.fps)
        return self._started + (idx + 1) / p.fps

    @property
    def current(self) -> Pack | None:
        return self._current

    def due(self, now: float | None = None) -> tuple[np.ndarray, tuple[int, int]] | None:
        """Return (frame, origin) when a NEW frame is due, else None.

        Returning None when the frame has not advanced is what keeps an idle
        panel from re-pushing identical pixels across a saturated bus.
        """
        if self._current is None:
            return None
        now = now or time.time()
        p = self._current
        idx = int((now - self._started) * p.fps)
        if not p.loop and idx >= p.frames:
            idx = p.frames - 1
        idx %= p.frames

        fading = self._fade_from is not None and now < self._fade_until
        # While dissolving, every tick produces a new blend even if the source
        # frame index has not advanced -- otherwise the fade would step only as
        # often as the pack does and look like a stutter, not a dissolve.
        if idx == self._last_index and not fading:
            return None
        self._last_index = idx

        frame = p.frame(idx)
        if fading:
            t = 1.0 - (self._fade_until - now) / FADE
            frame = blend565(self._fade_from, frame, min(max(t, 0.0), 1.0))
        elif self._fade_from is not None:
            self._fade_from = None      # dissolve finished; drop the source

        self._last_frame = frame
        return frame, p.origin

    def close(self) -> None:
        for p in self._packs.values():
            p.close()
        self._packs.clear()
        self._current = None
