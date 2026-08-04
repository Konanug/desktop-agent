"""Publish Hermes gateway activity to a state file for the hermes-pi display.

Runs in-process inside the Hermes gateway via its supported hook system
(~/.hermes/hooks/). Nothing here modifies agent behaviour -- it is a pure
observer.

THREE RULES, in priority order:

1. NEVER RAISE. Hermes catches hook exceptions (gateway/hooks.py emit()), but
   we do not lean on that. A display bug must never disturb Discord.

2. NEVER BLOCK. emit() *awaits* coroutine handlers inline, so a slow hook
   stalls the agent pipeline. Every write is a few hundred bytes to tmpfs:
   no fsync, no network, no locks.

3. NEVER WRITE CONTENT. The event context hands us `message`, `response`, and
   a raw `tools` list that can contain tool ARGUMENTS. None of it is recorded.
   Only enums, timestamps, counters, and tool *names* -- see
   docs/STATE-CONTRACT.md.

The file lives on tmpfs so restarts of either side are cheap and the SD card
sees no writes.
"""

from __future__ import annotations

import asyncio
import json
import os
import time
from pathlib import Path

SCHEMA = 1

# Refresh cadence for the heartbeat. The renderer treats a stale `updated_at`
# as evidence the gateway is wedged, so this must tick even when idle --
# otherwise a hung-but-alive gateway is indistinguishable from a healthy one.
HEARTBEAT_SECONDS = 10.0

# How long "responding" lingers before decaying to "idle". Long enough to read
# as a distinct state on the panel, short enough not to lie about being busy.
RESPONDING_SETTLE_SECONDS = 2.0


def _runtime_dir() -> Path:
    """User-owned tmpfs dir. No root, no systemd RuntimeDirectory= needed.

    XDG_RUNTIME_DIR is /run/user/1000 here: tmpfs, mode 0700, owned by us and
    already isolated from other users.
    """
    base = os.environ.get("XDG_RUNTIME_DIR") or f"/run/user/{os.getuid()}"
    return Path(base) / "hermes-display"


_STATE_PATH = _runtime_dir() / "state.json"

# Full document held in memory, so a write is never a read-modify-write off
# disk. Readers always see a self-consistent snapshot.
_STATE: dict = {
    "schema": SCHEMA,
    "updated_at": 0.0,
    "pid": os.getpid(),
    "started_at": time.time(),
    "activity": "starting",
    "activity_since": time.time(),
    "tool": None,
    "iteration": None,
    "model_state": "unknown",
    "model_detail": None,
    "link": {"platform": None, "last_event_at": None},
    "display": {"mode": "idle", "image": None, "text": None, "expires_at": None},
}

_heartbeat_task: asyncio.Task | None = None


def _write() -> None:
    """Atomically replace the state file. Silent on failure, by design."""
    try:
        _STATE["updated_at"] = time.time()
        d = _STATE_PATH.parent
        d.mkdir(mode=0o700, parents=True, exist_ok=True)
        # Same directory => same filesystem => rename(2) is atomic, so a reader
        # never observes a partially written file.
        tmp = d / f".state.json.{os.getpid()}.tmp"
        tmp.write_text(json.dumps(_STATE), encoding="utf-8")
        os.replace(tmp, _STATE_PATH)
    except Exception:
        pass


def _set_activity(activity: str, *, tool=None, iteration=None) -> None:
    """Update activity, stamping activity_since only on an actual change.

    Preserving activity_since across repeated same-state events is what lets
    the renderer enforce a minimum dwell (no flicker on fast turns) and a
    maximum age (never pinned on "thinking" if an agent:end goes missing).
    """
    if _STATE["activity"] != activity:
        _STATE["activity_since"] = time.time()
    _STATE["activity"] = activity
    _STATE["tool"] = tool
    _STATE["iteration"] = iteration


async def _heartbeat() -> None:
    """Refresh the file on a timer and decay `responding` into `idle`.

    Exists because hooks are edge-triggered: without a tick, an idle gateway
    and a hung one produce byte-identical files.
    """
    while True:
        try:
            await asyncio.sleep(HEARTBEAT_SECONDS)
            if (
                _STATE["activity"] == "responding"
                and time.time() - _STATE["activity_since"] >= RESPONDING_SETTLE_SECONDS
            ):
                _set_activity("idle")
            _write()
        except asyncio.CancelledError:
            raise
        except Exception:
            # Never let a transient error kill the heartbeat; a dead heartbeat
            # would make the renderer declare the gateway offline.
            pass


def _ensure_heartbeat() -> None:
    global _heartbeat_task
    try:
        if _heartbeat_task is None or _heartbeat_task.done():
            _heartbeat_task = asyncio.get_running_loop().create_task(_heartbeat())
    except Exception:
        pass


async def handle(event_type: str, context: dict) -> None:
    try:
        now = time.time()

        if event_type == "gateway:startup":
            platforms = context.get("platforms") or []
            _STATE["pid"] = os.getpid()
            _STATE["started_at"] = now
            _STATE["link"]["platform"] = platforms[0] if platforms else None
            _STATE["link"]["last_event_at"] = now
            _STATE["model_state"] = "ok"
            _set_activity("idle")
            _ensure_heartbeat()

        elif event_type == "agent:start":
            _STATE["link"]["last_event_at"] = now
            _set_activity("receiving")
            _ensure_heartbeat()

        elif event_type == "agent:step":
            _STATE["link"]["last_event_at"] = now
            # tool_names only. The sibling `tools` key holds full call dicts
            # including arguments -- deliberately not read.
            names = [n for n in (context.get("tool_names") or []) if n]
            iteration = context.get("iteration")
            if names:
                _set_activity("tool_use", tool=str(names[-1])[:40], iteration=iteration)
            else:
                _set_activity("thinking", iteration=iteration)

        elif event_type == "agent:end":
            # context also carries `response`; not read, not stored.
            _STATE["link"]["last_event_at"] = now
            _STATE["model_state"] = "ok"
            _set_activity("responding")

        elif event_type in ("session:start", "session:reset"):
            _STATE["link"]["last_event_at"] = now
            _set_activity("idle")

        elif event_type == "session:end":
            _STATE["link"]["last_event_at"] = now
            _set_activity("idle")

        _write()
    except Exception:
        # Absolutely never propagate. Discord matters more than the panel.
        pass
