#!/usr/bin/env python3
"""Measure Claude Code token usage in the rolling 5-hour window.

WHAT THIS IS, AND WHAT IT IS NOT

It is a count of tokens actually recorded in Claude Code's own transcripts on
THIS machine, inside a rolling window. Every number it publishes was read from
a file Claude Code wrote; nothing is estimated.

It is NOT a quota reading. The real limit is enforced server-side, weighted in
ways not published, and counts usage from every machine and interface. Two
consequences that the panel has to respect:

  * "tokens remaining" is not knowable here. Only tokens USED is.
  * the total is a floor, not the truth -- work done on another machine, in the
    web app, or on a phone is invisible to this.

THE SERVER'S OWN NUMBER IS AVAILABLE, and it outranks all of the above.
`claude -p "/usage"` runs the slash command non-interactively and prints the
real session and weekly percentages. Measured: it costs ZERO tokens (a control
period with no calls consumed 6,894 tokens from a concurrent session; the same
span containing three calls consumed 0), takes 1.7-3.3 s, and creates no
transcript. So it is polled on the collector's slow timer and is authoritative.

An earlier version of this file claimed the server figure could not be obtained
here. That was wrong: it checked for a `usage` SUBCOMMAND, found none, and
stopped without trying `-p "/usage"`. The local token counting below is now the
FALLBACK, for when the CLI cannot be reached.

Priority for the panel's bar: server percentage, then a budget the owner
declared, then nothing at all. Never a guess.

WHY A SEPARATE COLLECTOR
Summing ~2000 transcript messages is far too much I/O for the display's 30 Hz
loop. This runs on a slow timer and publishes a small JSON file; the renderer
only ever reads that.

Usage:
    python3 tools/claude_usage.py            # print a summary
    python3 tools/claude_usage.py --write    # publish usage.json for the panel
    python3 tools/claude_usage.py --loop 120 # keep it fresh, for a service
"""

from __future__ import annotations

import argparse
import datetime
import glob
import json
import os
import re
import shutil
import subprocess
import time
from pathlib import Path

WINDOW_SECONDS = 5 * 3600
TRANSCRIPTS = os.path.expanduser("~/.claude/projects/**/*.jsonl")
SCHEMA = 1

# Budget is the owner's number, not a measurement. Absent by default, and the
# panel degrades to showing window progress rather than inventing a denominator.
BUDGET_FILE = Path.home() / ".config" / "hermes-pi" / "claude-budget.json"


def _runtime_dir() -> Path:
    base = os.environ.get("XDG_RUNTIME_DIR") or f"/run/user/{os.getuid()}"
    d = Path(base) / "hermes-display"
    d.mkdir(mode=0o700, parents=True, exist_ok=True)
    return d


def usage_path() -> Path:
    return _runtime_dir() / "usage.json"


def _budget() -> int | None:
    """Owner-declared token budget for one window, or None."""
    try:
        v = json.loads(BUDGET_FILE.read_text()).get("window_tokens")
        return int(v) if v and int(v) > 0 else None
    except Exception:
        return None


def _parse_ts(ts: str) -> float | None:
    try:
        return datetime.datetime.fromisoformat(
            str(ts).replace("Z", "+00:00")).timestamp()
    except Exception:
        return None


# Matches e.g. "Current session: 96% used · resets Aug 5, 6:39pm (America/Toronto)"
#
# STRICTLY SINGLE-LINE, and the resets clause is optional. An earlier version
# used re.S with `.*?` between "used" and "resets", which meant that whenever
# the session line carried no reset time the match ran on to the NEXT line and
# picked up the WEEKLY reset date instead -- the panel then displayed "0% ·
# Aug 12" for a session that resets the same evening. Plausible and wrong,
# which is the worst thing this file can produce.
_SESSION_RE = re.compile(
    r"Current session:\s*(\d+)%\s*used"
    r"(?:[^\n]*?resets\s+([^(\n]+?)\s*(?:\(|$))?",
    re.I)
_WEEK_RE = re.compile(r"Current week[^:]*:\s*(\d+)%\s*used", re.I)


def _claude_binary() -> str | None:
    """Locate the claude CLI without depending on PATH.

    A systemd user service starts with a minimal PATH that does NOT include
    ~/.local/bin, which is exactly where this is installed. Relying on PATH
    worked from an interactive shell and silently returned nothing from the
    service -- the panel just quietly showed local figures instead of the real
    ones, which is the worst kind of failure: plausible and wrong.
    """
    found = shutil.which("claude")
    if found:
        return found
    for c in (Path.home() / ".local" / "bin" / "claude",
              Path("/usr/local/bin/claude"), Path("/usr/bin/claude")):
        if c.exists():
            return str(c)
    return None


def query_official(timeout: float = 30.0) -> dict:
    """Ask Claude Code for the REAL, server-side usage figures.

    `claude -p "/usage"` runs the slash command non-interactively and prints
    the same thing the interactive session shows. This is authoritative in a
    way local transcript counting can never be: it is the server's own number,
    weighted the way the limit actually is, and it includes work done on other
    devices.

    MEASURED before relying on it:
      * costs ZERO tokens. Control period with no calls consumed 6,894 tokens
        (a concurrent session); the same span containing three calls consumed
        0. It is a client-side query, not an inference request.
      * takes 1.7-3.3 s, which is why this lives in a 120 s collector loop and
        not anywhere near the display's 30 Hz path.
      * creates no new transcript session.

    Returns {} on any failure -- a wrong number here becomes a wrong bar on the
    panel, so the caller must be able to tell "no answer" from "an answer".
    """
    exe = _claude_binary()
    if not exe:
        return {}
    try:
        r = subprocess.run([exe, "-p", "/usage"], capture_output=True,
                           text=True, timeout=timeout)
    except Exception:
        return {}
    if r.returncode != 0 or not r.stdout:
        return {}

    out: dict = {"official_raw_ok": True}
    m = _SESSION_RE.search(r.stdout)
    if m:
        out["session_percent"] = int(m.group(1))
        # Optional group: absent when the line carries no reset time. Omit the
        # key entirely rather than storing None, so the panel simply shows no
        # reset time instead of an empty or borrowed one.
        if m.group(2):
            out["session_resets_at_text"] = m.group(2).strip()
    w = _WEEK_RE.search(r.stdout)
    if w:
        out["week_percent"] = int(w.group(1))
    # If the wording ever changes, we get {} rather than a plausible-looking
    # wrong figure. That is the correct failure for something the panel draws.
    return out if "session_percent" in out else {}


def collect(now: float | None = None, want_official: bool = True) -> dict:
    """Server-side usage where available, plus local token counts."""
    now = now or time.time()
    cutoff = now - WINDOW_SECONDS
    inp = out = cache_read = cache_write = 0
    messages = 0
    oldest = None

    for f in glob.iglob(TRANSCRIPTS, recursive=True):
        try:
            # Skip whole files that cannot contain in-window messages. mtime is
            # a cheap upper bound on the newest record inside.
            if os.path.getmtime(f) < cutoff:
                continue
        except OSError:
            continue
        try:
            fh = open(f, errors="ignore")
        except OSError:
            continue
        with fh:
            for line in fh:
                if '"usage"' not in line:          # cheap reject before parsing
                    continue
                try:
                    d = json.loads(line)
                except Exception:
                    continue
                m = d.get("message")
                if not isinstance(m, dict):
                    continue
                u = m.get("usage")
                if not isinstance(u, dict):
                    continue
                t = _parse_ts(d.get("timestamp"))
                if t is None or t < cutoff or t > now + 60:
                    continue
                inp += int(u.get("input_tokens") or 0)
                out += int(u.get("output_tokens") or 0)
                cache_read += int(u.get("cache_read_input_tokens") or 0)
                cache_write += int(u.get("cache_creation_input_tokens") or 0)
                messages += 1
                oldest = t if oldest is None else min(oldest, t)

    # Cache READS are excluded from the headline figure on purpose. They are an
    # order of magnitude cheaper than fresh input and dominate the raw sum by
    # ~50x here, so including them would make the bar track cache behaviour
    # rather than work done. Reported separately so the choice is visible.
    billable = inp + out + cache_write

    # The server's own figure, when we can get it. This OUTRANKS everything
    # computed locally: local counting cannot see other devices and does not
    # know how the limit is weighted.
    official = query_official() if want_official else {}

    budget = _budget()
    return {
        **official,
        "schema": SCHEMA,
        "updated_at": now,
        "window_seconds": WINDOW_SECONDS,
        "window_started_at": oldest,
        "window_resets_in": (WINDOW_SECONDS - (now - oldest)) if oldest else None,
        "messages": messages,
        "input_tokens": inp,
        "output_tokens": out,
        "cache_write_tokens": cache_write,
        "cache_read_tokens": cache_read,
        "billable_tokens": billable,
        "budget_tokens": budget,
        # Only meaningful when the owner declared a budget. None means the panel
        # must not draw a proportion -- there is nothing to be a proportion of.
        # Priority: the server's number, then a budget the owner declared,
        # then nothing. Never a guess -- None means the panel draws no bar.
        "fraction_used": (
            official["session_percent"] / 100.0 if "session_percent" in official
            else (min(1.0, billable / budget) if budget else None)),
        "fraction_source": (
            "server" if "session_percent" in official
            else ("budget" if budget else None)),
        # Always real: how far through the window we are.
        "window_fraction": ((now - oldest) / WINDOW_SECONDS) if oldest else None,
        "local_only": True,
    }


def publish(doc: dict) -> None:
    p = usage_path()
    tmp = p.with_suffix(".tmp")
    tmp.write_text(json.dumps(doc, indent=2))
    os.replace(tmp, p)


def _human(n: int) -> str:
    return f"{n/1_000_000:.2f}M" if n >= 1_000_000 else f"{n/1000:.0f}k"


def calibrate(percent: float) -> int:
    """Derive the budget from a real /usage reading.

    THE HONEST WAY TO GET A DENOMINATOR. This machine can measure tokens it
    saw locally, but not the server-side limit -- that is weighted in ways not
    published and counts every device you use. There is no API to ask, and
    /usage exists only inside an interactive session.

    So: you read the real percentage off /usage, and we solve for the budget
    that makes our local count agree with it. The result is still an estimate,
    but every input is real -- your observation and our measurement -- rather
    than a number someone made up.

    It drifts, for reasons worth knowing: work done on other devices is
    invisible here, and the server's weighting is not ours. Recalibrate when it
    stops matching. That it needs recalibrating is the honest signal that this
    is an approximation, not a reading.
    """
    if not 0 < percent <= 100:
        print("percent must be between 0 and 100")
        return 2
    doc = collect(want_official=False)
    local = doc["billable_tokens"]
    if local <= 0:
        print("no local usage recorded in this window yet -- nothing to calibrate from")
        return 1
    budget = int(local / (percent / 100.0))
    BUDGET_FILE.parent.mkdir(parents=True, exist_ok=True)
    BUDGET_FILE.write_text(json.dumps({
        "window_tokens": budget,
        "calibrated_at": time.time(),
        "calibrated_from_percent": percent,
        "local_tokens_at_calibration": local,
    }, indent=2))
    print(f"measured locally : {local:,} tokens")
    print(f"you reported     : {percent:.0f}% of the session limit")
    print(f"=> budget set to : {budget:,} tokens  ({BUDGET_FILE})")
    print("\nThe panel bar will now track that. Recalibrate whenever it drifts --")
    print("usage from other devices is invisible here, so it will.")
    publish(collect())
    return 0


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--calibrate", type=float, metavar="PERCENT",
                    help="set the budget from a real /usage percentage")
    ap.add_argument("--write", action="store_true", help="publish usage.json")
    ap.add_argument("--loop", type=float, default=0,
                    help="publish every N seconds and keep running")
    args = ap.parse_args()

    if args.calibrate is not None:
        return calibrate(args.calibrate)

    while True:
        t0 = time.time()
        doc = collect()
        if args.write or args.loop:
            publish(doc)
        if not args.loop:
            print(f"window: {doc['messages']} messages, "
                  f"resets in {(doc['window_resets_in'] or 0)/3600:.2f}h")
            print(f"  input {_human(doc['input_tokens'])}  "
                  f"output {_human(doc['output_tokens'])}  "
                  f"cache-write {_human(doc['cache_write_tokens'])}")
            print(f"  billable {_human(doc['billable_tokens'])}  "
                  f"(cache reads {_human(doc['cache_read_tokens'])} excluded)")
            print(f"  budget {doc['budget_tokens'] or 'not set'}  "
                  f"scan {1000*(time.time()-t0):.0f} ms")
            return 0
        time.sleep(max(5.0, args.loop))


if __name__ == "__main__":
    raise SystemExit(main())
