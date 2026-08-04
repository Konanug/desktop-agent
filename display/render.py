"""Draws screens and pushes only what changed.

Phase 6 is deliberately text-only -- no animation, no artwork. The goal is a
panel that is never *wrong*, which has to be true before it is made pretty.
Phase 7 replaces the body zone with pre-rendered RGB565 animation packs.

DIRTY TRACKING
The panel is split into three horizontal zones (header / body / footer). Each
is re-rendered into a numpy array, hashed, and blitted only when its hash
changes. A fully idle panel therefore writes ZERO bytes to the SPI bus, and a
clock tick costs one 480x28 strip (~27 KB) rather than a full 300 KB frame.
"""

from __future__ import annotations

import time
from dataclasses import dataclass

import numpy as np
from PIL import Image, ImageDraw, ImageFont

from .states import Resolved, Screen

# Cyan-on-black (decision D5). High contrast is not just an aesthetic here:
# RGB565 truncates to 5/6/5 bits, so low-contrast gradients band visibly.
CYAN = (0, 229, 255)
DIM = (0, 90, 110)
WHITE = (230, 240, 245)
GREY = (110, 130, 140)
AMBER = (255, 179, 0)
RED = (255, 60, 60)
GREEN = (60, 220, 120)
BLACK = (0, 0, 0)

FONT_DIR = "/usr/share/fonts/truetype/dejavu"

# screen -> (label, accent colour)
_LABEL: dict[Screen, tuple[str, tuple[int, int, int]]] = {
    Screen.STARTUP:        ("STARTING",     DIM),
    Screen.IDLE:           ("IDLE",         CYAN),
    Screen.RECEIVING:      ("RECEIVING",    CYAN),
    Screen.THINKING:       ("THINKING",     CYAN),
    Screen.TOOL_USE:       ("TOOL",         CYAN),
    Screen.RESPONDING:     ("RESPONDING",   GREEN),
    Screen.IMAGE:          ("IMAGE",        CYAN),
    Screen.TEXT_CARD:      ("MESSAGE",      CYAN),
    Screen.RECONNECTING:   ("RECONNECTING", AMBER),
    Screen.HERMES_OFFLINE: ("OFFLINE",      RED),
    Screen.AUTH_ERROR:     ("AUTH NEEDED",  RED),
    Screen.STALLED:        ("STALLED",      AMBER),
    Screen.FAILED:         ("FAILED",       RED),
    Screen.SHUTDOWN:       ("SHUTTING DOWN", DIM),
}


def _font(name: str, size: int) -> ImageFont.FreeTypeFont:
    try:
        return ImageFont.truetype(f"{FONT_DIR}/{name}", size)
    except Exception:
        return ImageFont.load_default()


@dataclass
class Zone:
    y: int
    h: int
    _hash: int = 0


class Renderer:
    def __init__(self, fb, width: int, height: int):
        self.fb = fb
        self.w, self.h = width, height
        self.header = Zone(0, 30)
        self.footer = Zone(height - 34, 34)
        self.body = Zone(self.header.h, self.footer.y - self.header.h)

        self.f_small = _font("DejaVuSans.ttf", 13)
        self.f_mid = _font("DejaVuSans-Bold.ttf", 17)
        self.f_big = _font("DejaVuSans-Bold.ttf", 30)
        self.f_tiny = _font("DejaVuSans.ttf", 11)

    # -- helpers ---------------------------------------------------------
    def _centre(self, d: ImageDraw.ImageDraw, y: int, text: str, font, fill, w: int) -> None:
        tw = d.textbbox((0, 0), text, font=font)[2]
        d.text(((w - tw) // 2, y), text, font=font, fill=fill)

    def _flush(self, zone: Zone, img: Image.Image) -> int:
        """Blit a zone only if its pixels actually changed."""
        arr = np.asarray(img, dtype=np.uint8)
        h = hash(arr.tobytes())
        if h == zone._hash:
            return 0
        zone._hash = h
        return self.fb.blit(arr, 0, zone.y)

    # -- zones -----------------------------------------------------------
    def draw_header(self, r: Resolved, clock_ok: bool, now: float) -> int:
        img = Image.new("RGB", (self.w, self.header.h), BLACK)
        d = ImageDraw.Draw(img)
        _, accent = _LABEL.get(r.screen, ("", CYAN))

        d.ellipse((10, 12, 20, 22), fill=accent)
        d.text((28, 7), "HERMES", font=self.f_mid, fill=WHITE)

        # The Pi has no battery-backed RTC. Until NTP syncs (~34 s after boot)
        # the clock is plausibly wrong, so we show placeholders instead of a
        # confident lie. See docs/DEFERRED.md D-2.
        if clock_ok:
            stamp = time.strftime("%H:%M", time.localtime(now))
            date = time.strftime("%a %d %b", time.localtime(now))
            col = WHITE
        else:
            stamp, date, col = "--:--", "no time sync", GREY

        tw = d.textbbox((0, 0), stamp, font=self.f_mid)[2]
        d.text((self.w - tw - 10, 6), stamp, font=self.f_mid, fill=col)
        dw = d.textbbox((0, 0), date, font=self.f_tiny)[2]
        d.text((self.w - dw - 10, 20), date, font=self.f_tiny, fill=GREY)

        d.line((0, self.header.h - 1, self.w, self.header.h - 1), fill=DIM)
        return self._flush(self.header, img)

    def draw_body(self, r: Resolved) -> int:
        img = Image.new("RGB", (self.w, self.body.h), BLACK)
        d = ImageDraw.Draw(img)
        label, accent = _LABEL.get(r.screen, (r.screen.value.upper(), CYAN))

        self._centre(d, self.body.h // 2 - 34, label, self.f_big, accent, self.w)

        sub = ""
        if r.screen == Screen.TOOL_USE and r.tool:
            sub = r.tool if r.iteration is None else f"{r.tool}  ·  step {r.iteration}"
        elif r.screen == Screen.THINKING and r.iteration:
            sub = f"step {r.iteration}"
        elif r.detail:
            sub = r.detail
        if sub:
            self._centre(d, self.body.h // 2 + 8, sub[:44], self.f_small, GREY, self.w)

        return self._flush(self.body, img)

    def draw_footer(self, r: Resolved, state: dict | None, health, now: float) -> int:
        img = Image.new("RGB", (self.w, self.footer.h), BLACK)
        d = ImageDraw.Draw(img)
        d.line((0, 0, self.w, 0), fill=DIM)

        link_ok = bool(state) and r.screen not in (
            Screen.HERMES_OFFLINE, Screen.RECONNECTING, Screen.FAILED,
        )
        model_ok = bool(state) and r.screen != Screen.AUTH_ERROR and health.unit_active

        def badge(x: int, text: str, ok: bool) -> int:
            d.ellipse((x, 13, x + 8, 21), fill=GREEN if ok else RED)
            d.text((x + 14, 10), text, font=self.f_tiny, fill=GREY)
            return x + 14 + d.textbbox((0, 0), text, font=self.f_tiny)[2] + 18

        x = badge(12, "discord", link_ok)
        badge(x, "model", model_ok)

        if state and health.unit_active:
            up = int(now - float(state.get("started_at") or now))
            txt = f"up {up//3600}h {(up%3600)//60:02d}m" if up >= 3600 else f"up {up//60}m {up%60:02d}s"
            tw = d.textbbox((0, 0), txt, font=self.f_tiny)[2]
            d.text((self.w - tw - 12, 10), txt, font=self.f_tiny, fill=GREY)

        return self._flush(self.footer, img)

    def draw(self, r: Resolved, state: dict | None, health, now: float | None = None) -> int:
        now = now or time.time()
        n = self.draw_header(r, health.clock_synced, now)
        n += self.draw_body(r)
        n += self.draw_footer(r, state, health, now)
        return n

    def invalidate(self) -> None:
        """Force a full repaint on the next draw (used after startup/resume)."""
        self.header._hash = self.body._hash = self.footer._hash = 0
