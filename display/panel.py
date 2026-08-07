"""The only hardware-specific file in the renderer.

Everything above this module draws into a Pillow RGB image and knows nothing
about framebuffers, pixel formats, or SPI. Swapping the panel -- a different
SPI TFT, or later an HDMI/DRM screen -- means rewriting this file alone.

Current hardware (discovered at runtime, not hardcoded):
    Waveshare HDMI LCD, 800x480, RGB565, via vc4 KMS fbdev emulation.

    Until 2026-08-06 this was an ILI9486 3.5" SPI TFT at 480x320 on fbtft. The
    swap changed less than expected: the pixel format is RGB565 either way, so
    pack_rgb565() is untouched and only the geometry and the driver name moved.

WHAT THE HDMI SWAP DELETED
Every bandwidth constraint this file used to be organised around. On SPI the
bus was the bottleneck: 2,562,838 B/s measured at 32 MHz, one 480x232 frame
costing 246,499 TRANSMITTED bytes, a hard ceiling of 10.37 fps, and a design
that budgeted dirty ROWS because fbtft is row-granular. None of that survives
here. The framebuffer is memory the display controller scans out on its own;
writing to it costs a memcpy and nothing else, and there is no per-row cost, no
transmit budget, and no error/timeout counter to watch.

Dirty rectangles are therefore now a CPU optimisation only, not a bandwidth
one, and a much weaker one. They are kept because they are already written and
still save real work, but nothing here should be traded away to protect them.

The relevant number changed from "bytes on the bus" to "bytes memcpy'd":
800x480x2 = 768,000 B per full frame, against memory bandwidth measured in
GB/s. Full-frame repaints are affordable now; they were not before.
"""

from __future__ import annotations

import mmap
import os
from dataclasses import dataclass

import numpy as np

SYSFS = "/sys/class/graphics"

# The driver that backs this panel. Resolved BY NAME because the index is not
# ours to rely on -- see resolve().
#
# `vc4drmfb` since 2026-08-06: the ILI9486 SPI panel was replaced by a Waveshare
# HDMI LCD, so this is now DRM's fbdev emulation rather than fbtft. Overridable
# so a bring-up on other hardware does not need a code edit.
PANEL_DRIVER = os.environ.get("HERMES_PANEL_DRIVER", "vc4drmfb")


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

        False on this panel (stride 1600 == 800 * 2), which lets full-frame
        writes be a single contiguous memcpy instead of a per-row loop. It was
        also false on the old SPI panel (960 == 480 * 2), so the fast path has
        never actually been exercised against a padded framebuffer -- do not
        assume it is proven if you change hardware again.
        """
        return self.stride != self.width * (self.bpp // 8)


def _read(fb: str, attr: str) -> str:
    with open(f"{SYSFS}/{fb}/{attr}") as f:
        return f.read().strip()


def resolve(driver: str = PANEL_DRIVER) -> str:
    """Find the framebuffer belonging to `driver`, by NAME, not by index.

    fb0 IS NOT OURS TO ASSUME. The index is assigned in registration order, so
    it depends on which drivers are loaded and how fast each one probes. With
    only fbtft present the panel is fb0; add a second display driver -- enabling
    KMS for an HDMI screen does exactly this -- and the panel can become fb1
    without a single line of this project changing. The renderer would then
    write the Hermes visual into the other screen's memory, at the wrong
    geometry, and report success.

    This is trap 13 in a new place. The camera learned it the same way: "is the
    sensor in use" could not be answered from /dev/video0 either, and
    protocol.sensor_power_path() resolves the imx708 by name for the same
    reason. The panel kept a hardcoded index until an HDMI screen was plugged
    in and made the risk real.

    Raises rather than falling back to fb0. A renderer that cannot find its own
    panel must say so, not paint into whatever is at index zero.
    """
    try:
        candidates = sorted(os.listdir(SYSFS))
    except OSError as e:
        raise RuntimeError(f"no framebuffers at {SYSFS}: {e}") from None
    found = {}
    for fb in candidates:
        if not fb.startswith("fb"):
            continue
        try:
            found[fb] = _read(fb, "name")
        except OSError:
            continue
    for fb, name in found.items():
        if name == driver:
            return fb
    raise RuntimeError(
        f"no framebuffer with driver {driver!r}; found "
        + (", ".join(f"{k}={v}" for k, v in found.items()) or "none")
        + ". Is the tft35a overlay still in /boot/firmware/config.txt?")


def discover(fb: str | None = None) -> PanelInfo:
    """Read geometry from sysfs rather than assuming it.

    Guards against the LCD-show class of bug, where a stale config names the
    wrong device or resolution and everything silently renders into nothing.

    `fb` defaults to whichever framebuffer the panel driver actually owns. Pass
    an explicit name only to override deliberately.
    """
    fb = resolve() if fb is None else fb
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
