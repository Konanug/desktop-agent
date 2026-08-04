"""The only hardware-specific file in the renderer.

Everything above this module draws into a Pillow RGB image and knows nothing
about framebuffers, pixel formats, or SPI. Swapping the panel -- a different
SPI TFT, or later an HDMI/DRM screen -- means rewriting this file alone.

Current hardware (discovered at runtime, not hardcoded):
    ILI9486 3.5" SPI TFT, 480x320, RGB565, /dev/fb0, via fbtft/fb_ili9486.

WHY DIRTY RECTANGLES MATTER HERE
SPI is the bottleneck, not the CPU. At the stock 16 MHz the bus moves roughly
2 Mbit/s, and a full 480x320x16bpp frame is 307,200 bytes = 2.46 Mbit -- about
5-6 fps if you repaint everything, every time. Pushing only the changed
rectangle is the entire performance strategy: a 40x20 blink costs ~1.6 KB
(~1 ms) instead of 300 KB (~170 ms).
"""

from __future__ import annotations

import mmap
import os
from dataclasses import dataclass

import numpy as np

SYSFS = "/sys/class/graphics"


@dataclass(frozen=True)
class PanelInfo:
    device: str
    name: str
    width: int
    height: int
    bpp: int
    stride: int

    @property
    def size_bytes(self) -> int:
        return self.stride * self.height

    @property
    def padded(self) -> bool:
        """True when rows are wider than their pixels (stride > w * bytes/px).

        False on this panel (stride 960 == 480 * 2), which lets full-frame
        writes be a single contiguous memcpy instead of a per-row loop.
        """
        return self.stride != self.width * (self.bpp // 8)


def _read(fb: str, attr: str) -> str:
    with open(f"{SYSFS}/{fb}/{attr}") as f:
        return f.read().strip()


def discover(fb: str = "fb0") -> PanelInfo:
    """Read geometry from sysfs rather than assuming it.

    Guards against the LCD-show class of bug, where a stale config names the
    wrong device or resolution and everything silently renders into nothing.
    """
    w, h = (int(x) for x in _read(fb, "virtual_size").split(","))
    return PanelInfo(
        device=f"/dev/{fb}",
        name=_read(fb, "name"),
        width=w,
        height=h,
        bpp=int(_read(fb, "bits_per_pixel")),
        stride=int(_read(fb, "stride")),
    )


def pack_rgb565(rgb: np.ndarray) -> np.ndarray:
    """(H, W, 3) uint8 RGB  ->  (H, W) uint16 RGB565, little-endian.

    Matches the panel's reported layout `rgba 5/11,6/5,5/0`: red in bits
    15..11, green 10..5, blue 4..0. Truncating the low bits (>>3, >>2, >>3) is
    what makes 8-bit gradients band on a 16-bit display -- which is why the
    UI palette favours high contrast over subtle shading.
    """
    r = (rgb[:, :, 0].astype(np.uint16) >> 3) << 11
    g = (rgb[:, :, 1].astype(np.uint16) >> 2) << 5
    b = rgb[:, :, 2].astype(np.uint16) >> 3
    return (r | g | b).astype("<u2")


class Framebuffer:
    """mmap'd framebuffer with rectangle-granular writes.

    Requires only membership of the `video` group -- /dev/fb0 is
    root:video crw-rw----, so the renderer never needs root.
    """

    def __init__(self, info: PanelInfo | None = None):
        self.info = info or discover()
        if self.info.bpp != 16:
            raise RuntimeError(
                f"{self.info.device} is {self.info.bpp}bpp; this renderer only "
                "packs RGB565. Update pack_rgb565() for other depths."
            )
        self._fd = os.open(self.info.device, os.O_RDWR)
        try:
            self._mm = mmap.mmap(
                self._fd, self.info.size_bytes,
                mmap.MAP_SHARED, mmap.PROT_READ | mmap.PROT_WRITE,
            )
        except Exception:
            os.close(self._fd)
            raise
        # Row-major uint16 view over the mapping, so a blit is a numpy slice
        # assignment rather than manual offset arithmetic.
        row_px = self.info.stride // 2
        self._px = np.frombuffer(self._mm, dtype="<u2").reshape(self.info.height, row_px)
        self.bytes_written = 0   # instrumentation for the SPI budget (Phase 6b)
        self.blits = 0

    def blit(self, rgb: np.ndarray, x: int = 0, y: int = 0) -> int:
        """Write an (h, w, 3) uint8 RGB block at (x, y). Returns bytes written.

        Clipped to the panel, so a caller with a slightly oversized rect
        degrades gracefully instead of raising or corrupting adjacent memory.
        """
        h, w = rgb.shape[0], rgb.shape[1]
        x0, y0 = max(0, x), max(0, y)
        x1, y1 = min(self.info.width, x + w), min(self.info.height, y + h)
        if x1 <= x0 or y1 <= y0:
            return 0
        src = rgb[y0 - y:y1 - y, x0 - x:x1 - x]
        self._px[y0:y1, x0:x1] = pack_rgb565(src)
        n = (x1 - x0) * (y1 - y0) * 2
        self.bytes_written += n
        self.blits += 1
        return n

    def blit_packed(self, packed: np.ndarray, x: int = 0, y: int = 0) -> int:
        """Write pre-packed RGB565 (h, w) uint16 -- no conversion cost.

        This is the path animation frames take in Phase 7: packs are stored on
        disk already in RGB565, so playback is a memcpy with no pixel maths.
        """
        h, w = packed.shape
        x0, y0 = max(0, x), max(0, y)
        x1, y1 = min(self.info.width, x + w), min(self.info.height, y + h)
        if x1 <= x0 or y1 <= y0:
            return 0
        self._px[y0:y1, x0:x1] = packed[y0 - y:y1 - y, x0 - x:x1 - x]
        n = (x1 - x0) * (y1 - y0) * 2
        self.bytes_written += n
        self.blits += 1
        return n

    def fill(self, rgb: tuple[int, int, int] = (0, 0, 0)) -> int:
        """Solid fill. Used on startup and shutdown to claim the whole panel."""
        r, g, b = rgb
        val = ((r >> 3) << 11) | ((g >> 2) << 5) | (b >> 3)
        self._px[:, : self.info.width] = np.uint16(val)
        n = self.info.width * self.info.height * 2
        self.bytes_written += n
        self.blits += 1
        return n

    def close(self) -> None:
        # The numpy view keeps the mmap "exported"; closing with it alive
        # raises BufferError: cannot close exported pointers exist. Drop the
        # array first so the buffer refcount reaches zero.
        try:
            self._px = None  # type: ignore[assignment]
        except Exception:
            pass
        try:
            self._mm.close()
        except BufferError:
            # A stray view somewhere else still holds it. Leaking the mapping
            # is survivable (the process is exiting); failing to close the fd
            # is not, so swallow and continue to the finally below.
            pass
        finally:
            os.close(self._fd)

    def __enter__(self) -> "Framebuffer":
        return self

    def __exit__(self, *exc) -> None:
        self.close()
