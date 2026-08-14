#!/usr/bin/env python3
"""The escape hatch that works when nothing else does.

Hold the ReSpeaker's button (GPIO 17) for HOLD seconds -> a terminal appears on
the screen. Hold again -> the Hermes panel comes back.

WHY A BUTTON AS WELL AS A SPOKEN PHRASE
The voice route already avoids the agent and the network, so it survives the
common case. It does not survive a broken microphone, a wedged voice service, a
mixer that came up muted, or simply not being able to make yourself understood.
This depends on none of that: a GPIO line, a shell script, and the framebuffer
console. It is the last thing to fail.

WHY A LONG HOLD
The button is on a HAT people press by accident while plugging things in, and
this is not a thing you want happening by accident mid-conversation. Three
seconds of continuous hold is deliberate and is not a brush. The press must
also be UNBROKEN -- a bounce resets it, so a flaky contact cannot accumulate
its way to a trigger.
"""

from __future__ import annotations

import subprocess
import sys
import time
from pathlib import Path

LINE = 17
HOLD = 3.0
SCRIPT = Path(__file__).resolve().parent / "console-mode.sh"


def _console_is_on() -> bool:
    for v in Path("/sys/class/vtconsole").glob("vtcon*"):
        try:
            if "frame buffer" in (v / "name").read_text().lower():
                return (v / "bind").read_text().strip() == "1"
        except OSError:
            pass
    return False


def main() -> int:
    try:
        import gpiod
    except ImportError:
        print("python3-gpiod not installed", file=sys.stderr)
        return 1

    # The ReSpeaker button pulls the line LOW when pressed, so "pressed" is the
    # inactive state of an active-high read. Requested with a pull-up because
    # an unconfigured floating input reads as random noise and would look like
    # somebody mashing the button.
    try:
        req = gpiod.request_lines(
            "/dev/gpiochip0", consumer="hermes-button",
            config={LINE: gpiod.LineSettings(
                direction=gpiod.line.Direction.INPUT,
                bias=gpiod.line.Bias.PULL_UP)})
    except Exception as e:
        print(f"cannot claim GPIO{LINE}: {e}", file=sys.stderr)
        return 1

    print(f"[button] watching GPIO{LINE}; hold {HOLD:.0f}s to toggle the console",
          flush=True)
    down_since = None
    fired = False
    while True:
        try:
            pressed = req.get_value(LINE) == gpiod.line.Value.INACTIVE
        except Exception:
            time.sleep(1.0)
            continue

        if pressed:
            if down_since is None:
                down_since = time.monotonic()
            elif not fired and time.monotonic() - down_since >= HOLD:
                fired = True
                arg = "off" if _console_is_on() else "on"
                print(f"[button] held {HOLD:.0f}s -> console-mode {arg}",
                      flush=True)
                subprocess.Popen(["bash", str(SCRIPT), arg])
        else:
            down_since = None      # ANY release resets; a bounce cannot add up
            fired = False
        time.sleep(0.05)


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        sys.exit(0)
