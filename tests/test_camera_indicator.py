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


# -- WATCH: the camera is analysing the room with nobody attached ----------
# A wider zone: WATCH sits to the right of CAM.
WATCH_X0, WATCH_X1 = 155, 235


def _watch_lit(camera_on, camera_watch) -> int:
    """Lit pixels in the WATCH zone.

    A HIGHER BRIGHTNESS BAR than the CAM zone above, and not an arbitrary one.
    MEASURED: this strip carries ~80 pixels of header background over 30, so at
    the CAM zone's threshold an empty strip and a drawn badge are 80 against
    245 -- a test that passes for the wrong reason and would keep passing if
    the badge were removed. Over 120 the background reads 0 and the text 104,
    which is the difference actually being asserted.
    """
    fb = _FB()
    r = Renderer(fb, 480, 320)
    r.draw_header(Resolved(Screen.IDLE, since=time.time()), True,
                  time.time(), camera_on, False, False, camera_watch)
    zone = np.asarray(fb.last)[:, WATCH_X0:WATCH_X1]
    return int((zone.max(axis=2) > 120).sum())


def test_always_track_is_shown_as_its_own_badge():
    """Tracking hands unattended is a bigger claim than "the sensor is powered",
    so it gets its own word rather than a shade of the CAM light."""
    assert _watch_lit(True, True) > 40, \
        "the camera is analysing the room and the panel does not say so"


def test_no_watch_badge_when_tracking_is_gated_normally():
    """The default -- tracking follows an attached viewer -- must NOT light it,
    or the badge means nothing."""
    assert _watch_lit(True, False) < 40


def test_unknown_watch_state_fails_toward_showing_it():
    """Same inverted direction as CAM. If the camera service's status cannot be
    read while the sensor is powered, we do not get to assume the friendlier
    answer."""
    assert _watch_lit(True, None) > 40, (
        "could not tell whether the room was being analysed and the panel "
        "showed nothing")


def test_an_unpowered_camera_does_not_claim_to_be_watching():
    """The scoping half. Unknown fails toward ON, but a sensor observed asleep
    cannot be analysing anything -- and a badge that is lit permanently is one
    nobody reads."""
    assert _watch_lit(False, None) < 40, \
        "claimed the room was being analysed by a camera that is asleep"


def test_the_watch_probe_reports_none_when_status_is_unreadable():
    """There is no kernel fact for this, so it reads the service's own file.
    Missing must be unknown, never 'no'."""
    from display.health import HealthProbe
    p = HealthProbe()
    p._cam_status = "/nonexistent/hermes-camera/status.json"
    assert p._camera_watch() is None


def test_the_watch_probe_reads_the_flag(tmp=None):
    import json
    import tempfile
    from display.health import HealthProbe
    p = HealthProbe()
    with tempfile.TemporaryDirectory() as d:
        f = os.path.join(d, "status.json")
        for value in (True, False):
            with open(f, "w") as fh:
                json.dump({"always_track": value}, fh)
            p._cam_status = f
            assert p._camera_watch() is value
        # A status file that predates the field is not a promise of "no".
        with open(f, "w") as fh:
            json.dump({"state": "off"}, fh)
        assert p._camera_watch() is False, \
            "absent field should read as not-configured, matching the service"


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
