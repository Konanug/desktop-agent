"""The usage figures on the panel must never be borrowed from the wrong line.

`claude -p "/usage"` prints a session line and a weekly line. They look alike.
An early regex used re.S with a lazy gap between "used" and "resets", so when
the session line carried no reset time the match ran on to the NEXT line and
reported the WEEKLY reset date as the session's -- the panel showed a session
resetting a week away when it actually reset that evening.

Nothing about that output looks wrong, which is why it is pinned here.

Run:  python3 tests/test_usage_parse.py
"""

import os
import sys

sys.path.insert(0, os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "tools"))

from claude_usage import _SESSION_RE, _WEEK_RE  # noqa: E402

REAL = ("You are currently using your subscription to power your Claude Code usage\n"
        "\n"
        "Current session: 22% used · resets Aug 6, 12:20am (America/Toronto)\n"
        "Current week (all models): 13% used · resets Aug 12, 6:59am (America/Toronto)\n")

NO_RESET = ("Current session: 0% used\n"
            "Current week (all models): 13% used · resets Aug 12, 6:59am (America/Toronto)\n")


def test_reads_the_session_line():
    m = _SESSION_RE.search(REAL)
    assert m and int(m.group(1)) == 22
    assert m.group(2).strip() == "Aug 6, 12:20am"
    assert int(_WEEK_RE.search(REAL).group(1)) == 13


def test_never_borrows_the_weekly_reset_date():
    """THE REGRESSION. No reset on the session line must yield NOTHING, not
    the weekly date sitting on the line below."""
    m = _SESSION_RE.search(NO_RESET)
    assert m and int(m.group(1)) == 0
    assert m.group(2) is None, f"session borrowed {m.group(2)!r} from another line"


def test_percentages_are_not_confused_with_each_other():
    m, w = _SESSION_RE.search(REAL), _WEEK_RE.search(REAL)
    assert int(m.group(1)) != int(w.group(1)), "session and week read the same value"


def test_unrecognised_output_matches_nothing():
    """A changed wording must fail closed -- query_official returns {} and the
    panel draws no bar, rather than a confident wrong number."""
    assert _SESSION_RE.search("Usage: 40 percent of your plan\n") is None


def _run() -> int:
    fails = 0
    for name, fn in sorted(globals().items()):
        if not name.startswith("test_") or not callable(fn):
            continue
        try:
            fn(); print(f"  PASS  {name}")
        except Exception as e:
            fails += 1; print(f"  FAIL  {name}: {type(e).__name__}: {e}")
    print(f"\n{'ALL PASS' if not fails else f'{fails} FAILURES'}")
    return fails


if __name__ == "__main__":
    sys.exit(1 if _run() else 0)
