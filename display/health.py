"""Independent observation of system truth.

The state file says what Hermes *believes* it is doing. This module answers
what is *observably* true. When they disagree, observation wins -- a stale or
malicious state file must never let the panel claim Hermes is healthy.

Everything here is deliberately cheap and runs on a slow tick (default 5 s),
because each call is a subprocess.
"""

from __future__ import annotations

import subprocess
import time
from dataclasses import dataclass


def _run(cmd: list[str], timeout: float = 4.0) -> str:
    """Best-effort command capture. Never raises, never blocks for long."""
    try:
        p = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        return (p.stdout or "").strip()
    except Exception:
        return ""


@dataclass
class Health:
    unit_active: bool = False
    unit_failed: bool = False
    unit_state: str = "unknown"
    clock_synced: bool = False
    checked_at: float = 0.0


class HealthProbe:
    """Polls systemd and the clock on a slow timer, caching between ticks."""

    def __init__(self, unit: str = "hermes-gateway.service", interval: float = 5.0):
        self.unit = unit
        self.interval = interval
        self._cache = Health()

    def get(self, now: float | None = None) -> Health:
        now = now or time.time()
        if now - self._cache.checked_at < self.interval:
            return self._cache

        state = _run(["systemctl", "--user", "is-active", self.unit]) or "unknown"

        # `is-active` reports "failed" for a unit that tripped its start-limit,
        # which is the terminal "needs a human" condition the FAILED screen
        # exists for -- distinct from a transient restart.
        self._cache = Health(
            unit_active=(state == "active"),
            unit_failed=(state == "failed"),
            unit_state=state,
            # The Pi has no battery-backed RTC: for ~34 s after every boot the
            # clock is confidently wrong (see docs/DEFERRED.md D-2). Rendering
            # a wrong time is exactly the fabricated-state failure the design
            # forbids, so the clock is only trusted once this is true.
            clock_synced=(
                _run(["timedatectl", "show", "-p", "NTPSynchronized", "--value"]) == "yes"
            ),
            checked_at=now,
        )
        return self._cache
