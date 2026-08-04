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

from .health import HealthProbe
from .panel import Framebuffer, discover
from .render import Renderer
from .states import Resolved, Screen, StateMachine
from .watcher import StateWatcher

POLL = 0.1          # state-file poll; a stat(2) is essentially free
TICK_LOG = 300.0    # periodic instrumentation line into the journal

_stop = False


def _handle_signal(signum, _frame):
    global _stop
    _stop = True


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="hermes-display")
    ap.add_argument("--fb", default="fb0")
    ap.add_argument("--unit", default="hermes-gateway.service")
    ap.add_argument("--once", action="store_true", help="render one frame and exit (for testing)")
    args = ap.parse_args(argv)

    signal.signal(signal.SIGTERM, _handle_signal)
    signal.signal(signal.SIGINT, _handle_signal)

    info = discover(args.fb)
    print(f"[display] {info.name} {info.width}x{info.height} {info.bpp}bpp "
          f"stride={info.stride} padded={info.padded}", flush=True)

    watcher = StateWatcher()
    probe = HealthProbe(unit=args.unit)
    machine = StateMachine()

    with Framebuffer(info) as fb:
        renderer = Renderer(fb, info.width, info.height)
        # Claim the whole panel: clears whatever was left by the console, a
        # previous run, or a crash mid-frame.
        fb.fill((0, 0, 0))
        renderer.invalidate()

        state, _ = watcher.poll()
        health = probe.get()
        resolved = machine.update(state, health)
        renderer.draw(resolved, state, health)

        if args.once:
            print(f"[display] once: {resolved.screen.value}", flush=True)
            return 0

        last_log = time.time()
        last_screen = resolved.screen
        while not _stop:
            now = time.time()
            state, changed = watcher.poll()
            health = probe.get(now)

            resolved = machine.update(state, health, now)
            machine.tick(now)
            resolved = machine.current

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

            time.sleep(POLL)

        # Leave the panel in an honest final state rather than frozen mid-frame.
        print("[display] SIGTERM -> shutdown screen", flush=True)
        renderer.invalidate()
        renderer.draw(Resolved(Screen.SHUTDOWN, since=time.time()), state, health)
        return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        sys.exit(0)
