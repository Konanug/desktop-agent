"""Draws screens and pushes only what changed.

Chrome (header/footer/label) is drawn here with Pillow. The body is owned by
display.player, which blits pre-rendered RGB565 frames straight into the
framebuffer -- text and animation are deliberately separate so a 12 fps visual
does not drag a text repaint along with it.

If no animation packs are installed, draw_body() renders the Phase 6 text
screens instead, so the panel still works.

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
VIOLET = (200, 90, 255)   # auth/credentials -- deliberately NOT red
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
    Screen.AUTH_ERROR:     ("AUTH NEEDED",  VIOLET),
    Screen.STALLED:        ("STALLED",      AMBER),
    Screen.FAILED:         ("FAILED",       RED),
    Screen.SHUTDOWN:       ("SHUTTING DOWN", DIM),
}


def _usage_text(usage: dict | None) -> str:
    """Tokens seen locally, and when the window rolls over.

    Deliberately NOT a percentage unless a budget exists. These two figures are
    measurements; a percentage would be a claim about a limit this machine
    cannot see.
    """
    if not usage:
        return ""
    parts: list[str] = []

    # Prefer the server's own numbers. They are the ones that decide when you
    # actually get cut off; the local token count is only a floor.
    pct = usage.get("session_percent")
    if pct is not None:
        parts.append(f"{pct}%")
        if usage.get("session_resets_at_text"):
            parts.append(str(usage["session_resets_at_text"]).split(",")[-1].strip())
        return " \u00b7 ".join(parts)

    frac = usage.get("fraction_used")
    if frac is not None:
        parts.append(f"{int(frac*100)}%")
    tok = int(usage.get("billable_tokens") or 0)
    if tok:
        parts.append(f"{tok/1_000_000:.1f}M" if tok >= 1_000_000 else f"{tok/1000:.0f}k")
    resets = usage.get("window_resets_in")
    if resets:
        parts.append(f"{resets/3600:.1f}h")
    return " \u00b7 ".join(parts)


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
        self.header = Zone(0, 26)
        self.footer = Zone(height - 30, 30)
        self.body = Zone(self.header.h, self.footer.y - self.header.h)
        # Set properly by draw_label_strip once the pack geometry is known.
        self.label = Zone(self.footer.y - 22, 22)

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
    def draw_header(self, r: Resolved, clock_ok: bool, now: float,
                    camera_on: bool | None = False) -> int:
        img = Image.new("RGB", (self.w, self.header.h), BLACK)
        d = ImageDraw.Draw(img)
        _, accent = _LABEL.get(r.screen, ("", CYAN))

        d.ellipse((10, 12, 20, 22), fill=accent)
        d.text((28, 7), "HERMES", font=self.f_mid, fill=WHITE)

        # Camera tally light.
        #
        # Deliberately CHROME and not a Screen state: the camera can be powered
        # while the panel is showing OFFLINE or FAILED, and the indicator has to
        # survive that. It is driven by the kernel's runtime power state for the
        # sensor, not by anything the camera service says about itself, so a
        # crashed or dishonest service cannot switch it off.
        #
        # THE FAIL DIRECTION IS INVERTED from everything else here. Elsewhere,
        # unknown means "assume nothing good". For the camera, unknown means
        # ASSUME IT IS ON -- never tell someone the camera is off unless that
        # was positively observed.
        if camera_on is not False:
            on = camera_on is True
            col = RED if on else AMBER
            label = "CAM" if on else "CAM?"
            x = 110
            d.ellipse((x, 12, x + 9, 21), fill=col)
            d.text((x + 14, 8), label, font=self.f_tiny, fill=col)

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

    def _subtitle(self, r: Resolved) -> str:
        if r.screen == Screen.TOOL_USE and r.tool:
            return r.tool if r.iteration is None else f"{r.tool}  ·  step {r.iteration}"
        if r.screen == Screen.THINKING and r.iteration:
            return f"step {r.iteration}"
        return r.detail or ""

    def draw_body(self, r: Resolved) -> int:
        """Text-only body. Used when no animation packs are installed."""
        img = Image.new("RGB", (self.w, self.body.h), BLACK)
        d = ImageDraw.Draw(img)
        label, accent = _LABEL.get(r.screen, (r.screen.value.upper(), CYAN))
        self._centre(d, self.body.h // 2 - 34, label, self.f_big, accent, self.w)
        sub = self._subtitle(r)
        if sub:
            self._centre(d, self.body.h // 2 + 8, sub[:44], self.f_small, GREY, self.w)
        return self._flush(self.body, img)

    def draw_label_strip(self, r: Resolved, top: int, height: int) -> int:
        """State label beneath the animation.

        A separate zone with its own hash, so a spinning animation does not
        drag a text repaint along with it every frame -- the label is static
        for seconds at a time while the visual runs at 12 fps.
        """
        if self.label.y != top or self.label.h != height:
            self.label = Zone(top, height)
        img = Image.new("RGB", (self.w, height), BLACK)
        d = ImageDraw.Draw(img)
        label, accent = _LABEL.get(r.screen, (r.screen.value.upper(), CYAN))
        sub = self._subtitle(r)
        text = f"{label}   {sub[:30]}" if sub else label
        self._centre(d, max(0, (height - 15) // 2), text, self.f_small, accent, self.w)
        return self._flush(self.label, img)

    def _usage_meter(self, d: ImageDraw.ImageDraw, usage: dict | None) -> str:
        """Segmented meter along the footer's top edge. Returns a short label.

        Replaces the plain divider rule rather than taking new space -- the
        footer had no room to spare, and a rule that carries information is
        better than one that does not.

        HONESTY, which is the whole difficulty here. Two different quantities
        can be drawn, and they are NOT interchangeable:

          * budget set    -> tokens used as a fraction of the owner's declared
                             budget. A real measurement over a real denominator.
          * no budget     -> how far through the 5-hour window we are, drawn
                             dim. Also real, and clearly a different thing.

        What is never drawn is "tokens remaining", because that is not knowable
        from here: the limit is enforced server-side with unpublished weighting,
        and this only sees work done on this machine. Inventing a denominator to
        make a fuller-looking bar is the "46% PWR LVL" failure (CLAUDE.md).

        No data, or data older than five minutes, draws the plain rule again --
        a stale meter would assert a number that is no longer true.
        """
        x0, x1, top, h = 10, self.w - 10, 1, 4
        fresh = bool(usage) and (time.time() - float(usage.get("updated_at") or 0)) < 300

        if not fresh:
            d.line((0, 0, self.w, 0), fill=DIM)
            return ""

        frac = usage.get("fraction_used")
        if frac is None:
            # NO BAR WITHOUT A BUDGET.
            #
            # This used to fall back to drawing how far through the 5-hour
            # window we are. That is a real number, but it is not the number
            # anyone reads off a meter in this position: the bar sat at ~45%
            # while Claude's own /usage said 90%, and the obvious conclusion
            # was that the meter was broken rather than that it was measuring
            # something else entirely. A bar that is honest in the source and
            # misleading on the glass is just misleading.
            #
            # Tokens and reset time still appear as text, where they are
            # labelled and cannot be mistaken for a proportion.
            d.line((0, 0, self.w, 0), fill=DIM)
            return _usage_text(usage)

        colour = RED if frac >= 0.9 else AMBER if frac >= 0.75 else CYAN
        seg_w, gap = 9, 3
        n = max(1, (x1 - x0 + gap) // (seg_w + gap))
        lit = int(round(min(1.0, max(0.0, float(frac))) * n))
        for i in range(n):
            sx = x0 + i * (seg_w + gap)
            d.rectangle((sx, top, sx + seg_w - 1, top + h - 1),
                        fill=colour if i < lit else (0, 28, 34))

        return _usage_text(usage)

    def draw_footer(self, r: Resolved, state: dict | None, health, now: float,
                    usage: dict | None = None) -> int:
        img = Image.new("RGB", (self.w, self.footer.h), BLACK)
        d = ImageDraw.Draw(img)
        usage_label = self._usage_meter(d, usage)

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

        right = self.w - 12
        if usage_label:
            uw = d.textbbox((0, 0), usage_label, font=self.f_tiny)[2]
            d.text((right - uw, 10), usage_label, font=self.f_tiny, fill=CYAN)
            right -= uw + 14

        if state and health.unit_active:
            up = int(now - float(state.get("started_at") or now))
            txt = f"up {up//3600}h {(up%3600)//60:02d}m" if up >= 3600 else f"up {up//60}m {up%60:02d}s"
            tw = d.textbbox((0, 0), txt, font=self.f_tiny)[2]
            d.text((right - tw, 10), txt, font=self.f_tiny, fill=GREY)

        return self._flush(self.footer, img)

    def draw_chrome(self, r: Resolved, state: dict | None, health,
                    now: float | None = None, usage: dict | None = None) -> int:
        """Header + footer only; the body is the animation's."""
        now = now or time.time()
        return (self.draw_header(r, health.clock_synced, now,
                                 getattr(health, 'camera_on', False))
                + self.draw_footer(r, state, health, now, usage))

    def draw(self, r: Resolved, state: dict | None, health,
             now: float | None = None, usage: dict | None = None) -> int:
        now = now or time.time()
        n = self.draw_header(r, health.clock_synced, now,
                             getattr(health, 'camera_on', False))
        n += self.draw_body(r)
        n += self.draw_footer(r, state, health, now, usage)
        return n

    def invalidate(self) -> None:
        """Force a full repaint on the next draw (used after startup/resume)."""
        self.header._hash = self.body._hash = self.footer._hash = self.label._hash = 0
