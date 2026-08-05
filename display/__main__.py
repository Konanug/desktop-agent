"""hermes-display entrypoint.

Owns /dev/fb0 and renders Hermes' real state. Runs as a systemd user service,
independent of the gateway: either can crash and restart without the other
noticing.

Loop shape: poll the state file at ~10 Hz (a stat is microseconds), probe
systemd on a slow tick, resolve one screen, and blit only zones that changed.
Idle costs zero SPI bytes.
"""

from __future__ import annotations

import argparse
import os
import signal
import sys
import time

from pathlib import Path

from .health import HealthProbe
from .panel import Framebuffer, discover
from .player import Player
from .render import Renderer
from .states import Resolved, Screen, StateMachine
from .watcher import RequestWatcher, StateWatcher, default_request_path

# Loop period. Must be shorter than the fastest animation frame interval
# (12 fps = 83ms) or the player can never reach its target rate. A stat(2) per
# iteration costs microseconds, so 30 Hz is affordable; measured idle CPU stays
# low because an unchanged frame is skipped rather than re-blitted.
POLL = 0.03
TICK_LOG = 300.0    # periodic instrumentation line into the journal

# How often the Pillow chrome (header/footer/label) is re-rendered.
#
# Zone dirty-hashing stops chrome being BLITTED when unchanged, but it does not
# stop it being DRAWN: every iteration built three PIL images and rasterised
# text just to hash the result and throw it away. At 33 Hz that was the largest
# single CPU cost in the process.
#
# Nothing in the chrome changes faster than once a minute (clock, uptime) apart
# from state, which is handled separately by forcing a redraw on change. 0.5 s
# keeps the clock visually instant while cutting the rasterising ~15x.
CHROME_PERIOD = 0.5

_stop = False

# Screen -> animation pack. Screens absent here (STARTUP, IMAGE, TEXT_CARD)
# fall back to the text body, which is correct: those are not "Hermes is
# doing something" states.
_PACK_FOR = {
    Screen.IDLE: "idle",
    Screen.RECEIVING: "receiving",
    Screen.THINKING: "thinking",
    Screen.TOOL_USE: "tool_use",
    Screen.RESPONDING: "responding",
    Screen.RECONNECTING: "reconnecting",
    Screen.HERMES_OFFLINE: "offline",
    Screen.STALLED: "reconnecting",
    Screen.AUTH_ERROR: "error",
    Screen.FAILED: "error",
}


def show_image(fb, renderer, resolved, request) -> None:
    """Blit a plugin-prepared image, or render a text card.

    The bytes were validated, decoded and re-encoded to raw RGB565 by the
    plugin inside Hermes. This process never decodes untrusted input -- it
    only accepts a file of exactly the expected length, from a directory it
    controls. A malformed or hostile image cannot reach the framebuffer owner.
    """
    import numpy as np

    body_y = renderer.header.h
    body_h = renderer.footer.y - body_y

    if resolved.screen == Screen.TEXT_CARD:
        renderer.draw_body(resolved)
        return

    name = (request or {}).get("image")
    w = int((request or {}).get("w") or 0)
    h = int((request or {}).get("h") or 0)
    if not name or not w or not h:
        renderer.draw_body(resolved)
        return

    path = default_request_path().parent / "images" / str(name)
    try:
        # Basename only, resolved under our own spool: no traversal, no
        # following a path the model chose.
        if path.name != str(name) or not path.is_file():
            raise FileNotFoundError(name)
        raw = path.read_bytes()
        if len(raw) != w * h * 2:
            raise ValueError(f"expected {w*h*2} bytes, got {len(raw)}")
        arr = np.frombuffer(raw, dtype="<u2").reshape(h, w)
        fb.blit_packed(arr, 0, body_y + max(0, (body_h - h) // 2))
    except Exception as e:
        print(f"[display] image unusable: {e}", flush=True)
        renderer.draw_body(resolved)


def _clear_body(fb, renderer) -> None:
    """Blank the whole body zone.

    Needed when leaving an IMAGE screen. The camera preview is 264 rows tall
    and fills the body exactly; the animation that replaces it is 232 rows at
    y=28, and the label strip covers 262..290. That leaves rows 26, 27, 260 and
    261 painted by nobody, so the last few lines of the previous photo survive
    as a bright band above the state label -- which looks like a rendering
    fault and is, in a small way, the panel showing something that is no longer
    true.
    """
    import numpy as np
    h = renderer.footer.y - renderer.header.h
    fb.blit(np.zeros((h, renderer.w, 3), np.uint8), 0, renderer.header.h)


def _handle_signal(signum, _frame):
    global _stop
    _stop = True


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="hermes-display")
    ap.add_argument("--fb", default="fb0")
    ap.add_argument("--unit", default="hermes-gateway.service")
    ap.add_argument("--once", action="store_true", help="render one frame and exit (for testing)")
    ap.add_argument("--packs", default=str(Path(__file__).resolve().parent.parent / "assets" / "anim"),
                    help="animation pack directory; falls back to text screens if absent")
    ap.add_argument("--no-anim", action="store_true", help="force the text-only screens")
    args = ap.parse_args(argv)

    signal.signal(signal.SIGTERM, _handle_signal)
    signal.signal(signal.SIGINT, _handle_signal)

    info = discover(args.fb)
    print(f"[display] {info.name} {info.width}x{info.height} {info.bpp}bpp "
          f"stride={info.stride} padded={info.padded}", flush=True)

    watcher = StateWatcher()
    requests = RequestWatcher()
    probe = HealthProbe(unit=args.unit)
    machine = StateMachine()

    with Framebuffer(info) as fb:
        renderer = Renderer(fb, info.width, info.height)
        player = Player(Path(args.packs))
        animate = (not args.no_anim) and player.available()
        print(f"[display] animation: {'on' if animate else 'off (text screens)'} "
              f"({args.packs})", flush=True)
        # Claim the whole panel: clears whatever was left by the console, a
        # previous run, or a crash mid-frame.
        fb.fill((0, 0, 0))
        renderer.invalidate()

        state, _ = watcher.poll()
        request, _ = requests.poll()
        health = probe.get()
        resolved = machine.update(state, health, request=request)
        renderer.draw(resolved, state, health)

        if args.once:
            print(f"[display] once: {resolved.screen.value}", flush=True)
            return 0

        last_log = time.time()
        last_chrome = 0.0
        last_screen = resolved.screen
        while not _stop:
            now = time.time()
            state, changed = watcher.poll()
            request, req_changed = requests.poll()
            health = probe.get(now)

            resolved = machine.update(state, health, now, request=request)
            machine.tick(now)
            resolved = machine.current

            # Rasterise chrome on a slow tick, but ALWAYS immediately when the
            # screen changes -- a state transition must never wait on a timer.
            chrome_due = (now - last_chrome >= CHROME_PERIOD
                          or resolved.screen != last_screen)
            if chrome_due:
                last_chrome = now

            # Whether the body belongs to show_image this iteration. Without
            # this the IMAGE screen blits a picture and then renderer.draw()
            # immediately repaints the text body on top of it, so the image is
            # never actually seen -- display_show_image had this bug from the
            # start. The body has exactly one owner per frame; say which.
            body_owned = False

            # Leaving a photo: wipe the body before the animation returns.
            # The animation and label strip between them do not cover every
            # row the preview used, so without this the last lines of the
            # picture persist as a bright band. Once, on the transition.
            if (last_screen in (Screen.IMAGE, Screen.TEXT_CARD)
                    and resolved.screen not in (Screen.IMAGE, Screen.TEXT_CARD)):
                _clear_body(fb, renderer)
                renderer.invalidate()

            if resolved.screen in (Screen.IMAGE, Screen.TEXT_CARD):
                body_owned = True
                if req_changed or last_screen != resolved.screen:
                    renderer.invalidate()
                    show_image(fb, renderer, resolved, request)
                if chrome_due:
                    renderer.draw_chrome(resolved, state, health, now)
                # Force a fresh animation frame when the request expires,
                # rather than resuming mid-loop over a stale image.
                player.select("", now)
                pack_name = None
            else:
                pack_name = _PACK_FOR.get(resolved.screen) if animate else None
            if pack_name:
                if chrome_due:
                    renderer.draw_chrome(resolved, state, health, now)
                if player.select(pack_name, now):
                    # New pack: clear the body once so a smaller frame cannot
                    # leave the previous animation's edges on screen.
                    renderer.invalidate()
                    renderer.draw_chrome(resolved, state, health, now)
                pk = player.current
                if pk is not None:
                    ox, oy = pk.origin
                    due = player.due(now)
                    if due is not None:
                        frame, _ = due
                        fb.blit_packed(frame, ox, oy)
                    # Label sits just under the animation. Its own zone hash
                    # means a 12 fps visual does not repaint static text.
                    if chrome_due:
                        top = min(oy + pk.h + 2, renderer.footer.y - 18)
                        renderer.draw_label_strip(
                            resolved, top=top,
                            height=max(16, renderer.footer.y - top))
            elif chrome_due and not body_owned:
                renderer.draw(resolved, state, health, now)

            if resolved.screen != last_screen:
                # Log transitions, never content -- same privacy rule as the
                # state file itself.
                print(f"[display] {last_screen.value} -> {resolved.screen.value}"
                      + (f" ({resolved.detail})" if resolved.detail else ""), flush=True)
                last_screen = resolved.screen

            if now - last_log >= TICK_LOG:
                print(f"[display] {fb.blits} blits, {fb.bytes_written/1024:.0f} KiB "
                      f"since start, screen={resolved.screen.value}", flush=True)
                last_log = now

            # Sleep to whichever comes first: the next state poll, or the next
            # animation frame. Waking exactly on the frame deadline is what
            # makes playback even -- a fixed POLL can only land on multiples of
            # itself, which quantised 83.3 ms frames into 90/90/90/60 ms.
            deadline = player.next_due(now) if pack_name else None
            delay = POLL if deadline is None else max(
                0.0, min(POLL, deadline - time.time()))
            time.sleep(delay)

        # Leave the panel in an honest final state rather than frozen mid-frame.
        print("[display] SIGTERM -> shutdown screen", flush=True)
        player.close()
        fb.fill((0, 0, 0))
        renderer.invalidate()
        renderer.draw(Resolved(Screen.SHUTDOWN, since=time.time()), state, health)
        return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        sys.exit(0)
