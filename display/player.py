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
        """Switch packs. Returns True if the active pack changed."""
        pack = self.get(name)
        if pack is None or pack is self._current:
            return False
        self._current = pack
        self._started = now or time.time()
        self._last_index = -1
        return True

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
        if idx == self._last_index:
            return None
        self._last_index = idx
        return p.frame(idx), p.origin

    def close(self) -> None:
        for p in self._packs.values():
            p.close()
        self._packs.clear()
        self._current = None
