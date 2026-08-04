"""State-machine truth tests.

The property under test is the project's core rule: THE PANEL MUST NEVER CLAIM
HERMES IS HEALTHY WHEN IT IS NOT. A state file is an assertion by a process
that may be dead, wedged, or lying about its own liveness; systemd is
observation. Observation must win every time.

Run:  python3 -m pytest tests/ -q        (or: python3 tests/test_states.py)
"""
import sys, time, os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from display.states import StateMachine, Screen
from display.health import Health

NOW = time.time()
UP   = Health(unit_active=True,  unit_failed=False, unit_state="active",   clock_synced=True, checked_at=NOW)
DOWN = Health(unit_active=False, unit_failed=False, unit_state="inactive", clock_synced=True, checked_at=NOW)
FAIL = Health(unit_active=False, unit_failed=True,  unit_state="failed",   clock_synced=True, checked_at=NOW)


def st(**kw):
    d = {"schema": 1, "updated_at": NOW, "activity": "idle", "activity_since": NOW,
         "model_state": "ok", "started_at": NOW - 100,
         "display": {"mode": "idle", "expires_at": None}}
    d.update(kw)
    return d


# A state file insisting all is well while the process is gone.
LIAR = st(activity="thinking", updated_at=NOW)

CASES = [
    ("healthy idle",                 st(),                                             UP,   Screen.IDLE),
    ("thinking",                     st(activity="thinking"),                          UP,   Screen.THINKING),
    ("tool use",                     st(activity="tool_use", tool="terminal"),         UP,   Screen.TOOL_USE),
    ("auth error",                   st(model_state="error"),                          UP,   Screen.AUTH_ERROR),
    ("heartbeat 45s late",           st(updated_at=NOW - 45),                          UP,   Screen.RECONNECTING),
    ("heartbeat 120s gone",          st(updated_at=NOW - 120),                         UP,   Screen.HERMES_OFFLINE),
    ("activity pinned 200s",         st(activity="thinking", activity_since=NOW - 200), UP,  Screen.STALLED),
    ("unit stopped, file says busy", LIAR,                                             DOWN, Screen.HERMES_OFFLINE),
    ("unit failed, file says busy",  LIAR,                                             FAIL, Screen.FAILED),
    ("no state file at all",         None,                                             UP,   Screen.STARTUP),
    ("no file AND unit down",        None,                                             DOWN, Screen.HERMES_OFFLINE),
    ("empty dict",                   {},                                               UP,   Screen.STARTUP),
]


def test_resolution():
    for name, state, health, want in CASES:
        got = StateMachine().update(state, health, NOW).screen
        assert got is want, f"{name}: got {got.value}, want {want.value}"


def test_fault_bypasses_dwell():
    """min_dwell smooths cosmetic flicker; it must never delay bad news."""
    m = StateMachine()
    m.update(st(), UP, NOW)
    got = m.update(LIAR, DOWN, NOW + 0.01).screen  # far inside min_dwell
    assert got is Screen.HERMES_OFFLINE, got


def test_dwell_suppresses_flicker():
    """A sub-dwell cosmetic change is deferred, then promoted by tick()."""
    m = StateMachine()
    m.update(st(), UP, NOW)
    assert m.update(st(activity="thinking"), UP, NOW + 0.01).screen is Screen.IDLE
    assert m.tick(NOW + 1.0).screen is Screen.THINKING


def test_first_frame_is_not_deferred():
    """Regression: initial STARTUP stamped with now() delayed the first real
    state by min_dwell, flashing STARTUP on every launch."""
    assert StateMachine().update(st(), UP, NOW).screen is Screen.IDLE


if __name__ == "__main__":
    fails = 0
    for name, state, health, want in CASES:
        got = StateMachine().update(state, health, NOW).screen
        ok = got is want
        fails += not ok
        print(f"  {'PASS' if ok else 'FAIL'}  {name:32s} -> {got.value}")
    for fn in (test_fault_bypasses_dwell, test_dwell_suppresses_flicker, test_first_frame_is_not_deferred):
        try:
            fn(); print(f"  PASS  {fn.__name__}")
        except AssertionError as e:
            fails += 1; print(f"  FAIL  {fn.__name__}: {e}")
    print(f"\n{'ALL PASS' if not fails else f'{fails} FAILURES'}")
    sys.exit(1 if fails else 0)
