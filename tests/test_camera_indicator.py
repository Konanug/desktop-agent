"""The camera indicator must never claim the camera is off when it might be on.

tests/test_states.py pins the panel's normal fail direction: when the truth is
unknown, assume nothing good. This pins the CAMERA indicator's, which is the
exact opposite and is easy to "fix" back to consistency by accident.

  observed powered   -> show it
  observed suspended -> hide it
  cannot tell        -> SHOW IT ANYWAY

Getting this backwards would mean a panel that says the camera is off while it
is watching the room. That is the one failure mode the indicator exists to
prevent, so it is asserted rather than left to review.

Run:  python3 tests/test_camera_indicator.py
"""

import os
import sys
import time

import numpy as np

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from display.render import Renderer          # noqa: E402
from display.states import Resolved, Screen  # noqa: E402

# The badge lives between the HERMES wordmark and the clock.
BADGE_X0, BADGE_X1 = 105, 155


class _FB:
    def __init__(self):
        self.last = None

    def blit(self, arr, x, y):
        self.last = arr
        return arr.size

    def blit_packed(self, a, x, y):
        return 0

    def fill(self, c):
        return 0


def _lit(camera_on) -> int:
    """Lit pixels in the badge zone for a given camera state."""
    fb = _FB()
    r = Renderer(fb, 480, 320)
    r.draw_header(Resolved(Screen.IDLE, since=time.time()), True,
                  time.time(), camera_on)
    zone = np.asarray(fb.last)[:, BADGE_X0:BADGE_X1]
    return int((zone.max(axis=2) > 30).sum())


def test_powered_shows_the_badge():
    assert _lit(True) > 60, "camera on but nothing drawn"


def test_suspended_hides_the_badge():
    assert _lit(False) < 60, "camera off but a badge is drawn"


def test_unknown_shows_the_badge():
    """THE POINT OF THIS FILE. Unknown must fail toward ON."""
    assert _lit(None) > 60, (
        "camera state unknown but the panel showed nothing -- this tells the "
        "owner the camera is off when it may be watching")


def test_badge_survives_a_dead_gateway():
    """The camera can be powered while Hermes is offline. The indicator is
    chrome, not a screen state, precisely so it survives that."""
    fb = _FB()
    r = Renderer(fb, 480, 320)
    r.draw_header(Resolved(Screen.HERMES_OFFLINE, since=time.time()), True,
                  time.time(), True)
    zone = np.asarray(fb.last)[:, BADGE_X0:BADGE_X1]
    assert int((zone.max(axis=2) > 30).sum()) > 60, \
        "badge vanished on the OFFLINE screen"


def test_probe_reports_none_when_the_file_is_missing():
    """A missing sysfs path must be 'unknown', never 'off'."""
    from display.health import HealthProbe
    p = HealthProbe()
    p._cam_file = "/nonexistent/runtime_status"
    assert p._camera_on() is None
    p._cam_file = None
    assert p._camera_on() is None


def test_probe_reads_active_and_suspended(tmp=None):
    import tempfile
    from display.health import HealthProbe
    p = HealthProbe()
    with tempfile.NamedTemporaryFile("w", suffix=".status", delete=False) as f:
        path = f.name
    p._cam_file = path
    for text, expect in (("active\n", True), ("suspended\n", False)):
        open(path, "w").write(text)
        assert p._camera_on() is expect, f"{text!r} -> {expect}"
    os.unlink(path)


def _run() -> int:
    fails = 0
    for name, fn in sorted(globals().items()):
        if not name.startswith("test_") or not callable(fn):
            continue
        try:
            fn()
            print(f"  PASS  {name}")
        except Exception as e:
            fails += 1
            print(f"  FAIL  {name}: {type(e).__name__}: {e}")
    print(f"\n{'ALL PASS' if not fails else f'{fails} FAILURES'}")
    return fails


if __name__ == "__main__":
    sys.exit(1 if _run() else 0)
