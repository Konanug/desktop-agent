"""Resolves observed reality into exactly one screen state.

This is where the project's central rule lives: **the panel never invents
state.** Every value below traces to either a real Hermes event that arrived in
the state file, or something this process observed itself (systemd, clock).
There is no free-running animation pretending Hermes is busy.

Resolution order matters, and observation outranks assertion -- see
docs/STATE-CONTRACT.md.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from enum import Enum
from typing import Any

from .health import Health


class Screen(str, Enum):
    STARTUP = "startup"          # renderer up, no valid state yet
    IDLE = "idle"
    RECEIVING = "receiving"
    THINKING = "thinking"
    TOOL_USE = "tool_use"
    RESPONDING = "responding"
    IMAGE = "image"              # Phase 8
    TEXT_CARD = "text_card"      # Phase 8
    RECONNECTING = "reconnecting"
    HERMES_OFFLINE = "hermes_offline"
    AUTH_ERROR = "auth_error"
    STALLED = "stalled"
    FAILED = "failed"            # unit tripped its start limit -- needs a human
    SHUTDOWN = "shutdown"


# Activity strings the hook writes -> screens. Anything unrecognised falls
# through to IDLE rather than crashing, so a producer that adds a new activity
# degrades instead of breaking the panel.
_ACTIVITY = {
    "starting": Screen.STARTUP,
    "idle": Screen.IDLE,
    "receiving": Screen.RECEIVING,
    "thinking": Screen.THINKING,
    "tool_use": Screen.TOOL_USE,
    "responding": Screen.RESPONDING,
}

# Screens that must never be cut short by the dwell rule -- they are conditions,
# not momentary activity, and hiding them for even a moment would be dishonest.
_NO_DWELL = {
    Screen.HERMES_OFFLINE, Screen.FAILED, Screen.AUTH_ERROR,
    Screen.RECONNECTING, Screen.STALLED, Screen.SHUTDOWN,
}


@dataclass
class Resolved:
    screen: Screen
    tool: str | None = None
    iteration: int | None = None
    detail: str | None = None
    since: float = 0.0


class StateMachine:
    def __init__(
        self,
        stale_warn: float = 30.0,     # T1: heartbeat late -> RECONNECTING
        stale_offline: float = 90.0,  # T2: heartbeat gone -> HERMES_OFFLINE
        max_activity_age: float = 120.0,
        min_dwell: float = 0.4,
    ):
        self.stale_warn = stale_warn
        self.stale_offline = stale_offline
        self.max_activity_age = max_activity_age
        self.min_dwell = min_dwell
        # since=0.0, not now(): the initial STARTUP is a placeholder we have
        # not actually displayed yet. Stamping it with the current time makes
        # the dwell rule defer the FIRST real state by min_dwell, so every
        # start flashes STARTUP even when Hermes is already up and idle.
        self._current = Resolved(Screen.STARTUP, since=0.0)
        self._pending: Resolved | None = None

    @property
    def current(self) -> Resolved:
        return self._current

    def _raw(self, state: dict[str, Any] | None, health: Health, now: float) -> Resolved:
        # 1. A failed unit is terminal and outranks everything, including a
        #    state file that still claims all is well.
        if health.unit_failed:
            return Resolved(Screen.FAILED, detail="restart limit reached")

        # 2. Process gone -> offline, whatever the file says.
        if not health.unit_active:
            return Resolved(Screen.HERMES_OFFLINE, detail=health.unit_state)

        # 3. No readable state, but the unit is up: it is still starting, or
        #    the producer is broken. Either way we do not know.
        if not state:
            return Resolved(Screen.STARTUP, detail="awaiting state")

        age = now - float(state.get("updated_at") or 0)
        if age > self.stale_offline:
            return Resolved(Screen.HERMES_OFFLINE, detail=f"no heartbeat {int(age)}s")
        if age > self.stale_warn:
            return Resolved(Screen.RECONNECTING, detail=f"heartbeat {int(age)}s")

        if state.get("model_state") == "error":
            return Resolved(Screen.AUTH_ERROR, detail=str(state.get("model_detail") or "model error"))

        # Phase 8 overrides normal activity while a request is live.
        disp = state.get("display") or {}
        mode, expires = disp.get("mode"), disp.get("expires_at")
        if mode in ("image", "text") and (expires is None or now < float(expires)):
            return Resolved(
                Screen.IMAGE if mode == "image" else Screen.TEXT_CARD,
                detail=str(disp.get("image") or disp.get("text") or ""),
            )

        activity = str(state.get("activity") or "idle")
        screen = _ACTIVITY.get(activity, Screen.IDLE)

        # A lost agent:end would otherwise pin the panel on "thinking" forever.
        since = float(state.get("activity_since") or 0)
        if screen not in (Screen.IDLE, Screen.STARTUP) and since:
            if now - since > self.max_activity_age:
                return Resolved(Screen.STALLED, detail=f"{activity} {int(now-since)}s")

        tool = state.get("tool")
        return Resolved(
            screen,
            tool=str(tool)[:40] if tool else None,
            iteration=state.get("iteration") if isinstance(state.get("iteration"), int) else None,
        )

    def update(self, state: dict[str, Any] | None, health: Health, now: float | None = None) -> Resolved:
        """Fold new observations into the current screen, applying dwell."""
        now = now or time.time()
        target = self._raw(state, health, now)

        if target.screen == self._current.screen:
            # Same screen: refresh detail fields in place, keep `since` so
            # dwell and elapsed-time displays stay meaningful.
            self._current.tool = target.tool
            self._current.iteration = target.iteration
            self._current.detail = target.detail
            self._pending = None
            return self._current

        # Minimum dwell stops a fast turn (receiving -> thinking -> responding
        # in under a second) from strobing the panel. Fault states bypass it:
        # showing a stale-but-pretty screen during an outage is a lie.
        held = now - self._current.since
        if (
            held < self.min_dwell
            and target.screen not in _NO_DWELL
            and self._current.screen not in _NO_DWELL
        ):
            self._pending = target
            return self._current

        target.since = now
        self._current = target
        self._pending = None
        return self._current

    def tick(self, now: float | None = None) -> Resolved:
        """Promote a transition that was deferred by the dwell rule."""
        now = now or time.time()
        if self._pending and (now - self._current.since) >= self.min_dwell:
            self._pending.since = now
            self._current = self._pending
            self._pending = None
        return self._current
