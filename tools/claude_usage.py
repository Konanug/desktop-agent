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

So the meter is drawn against a budget the OWNER sets. If no budget is set, the
panel shows the window's time progress instead, which is real, and the token
count as a plain number. Drawing a percentage against a denominator we invented
would be exactly the fake telemetry this project already refused to ship once
(see CLAUDE.md, trap 10).

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


def collect(now: float | None = None) -> dict:
    """Sum recorded token usage inside the rolling window."""
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

    budget = _budget()
    return {
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
        "fraction_used": (min(1.0, billable / budget) if budget else None),
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
    doc = collect()
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
