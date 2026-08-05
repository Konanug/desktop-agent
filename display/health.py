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


def _sensor_power_file(sensor: str = "imx708") -> str | None:
    """Locate the camera sensor's runtime-PM status file, by device name.

    Resolved once and cached by the caller: an i2c bus walk per tick would be
    wasteful, and the path does not change while the machine is up.
    """
    import glob
    import os
    for d in glob.glob("/sys/bus/i2c/devices/*"):
        try:
            if open(f"{d}/name").read().strip() == sensor:
                p = f"{d}/power/runtime_status"
                return p if os.path.exists(p) else None
        except OSError:
            continue
    return None


@dataclass
class Health:
    unit_active: bool = False
    unit_failed: bool = False
    unit_state: str = "unknown"
    clock_synced: bool = False
    # None means "could not tell". The panel must treat that as CAMERA ON.
    camera_on: bool | None = None
    checked_at: float = 0.0


class HealthProbe:
    """Polls systemd and the clock on a slow timer, caching between ticks."""

    def __init__(self, unit: str = "hermes-gateway.service", interval: float = 5.0,
                 camera_interval: float = 0.25):
        self.unit = unit
        self.interval = interval
        self.camera_interval = camera_interval
        self._cache = Health()
        self._cam_file = _sensor_power_file()
        self._cam_checked = 0.0

    def _camera_on(self) -> bool | None:
        """Is the camera sensor POWERED? True / False / None = cannot tell.

        This reads the kernel's runtime power state for the sensor, which is a
        fact about the hardware. It is deliberately not a reading of anything
        the camera service publishes: if that service crashes with the sensor
        open, is replaced, or simply lies, the panel must still be right.

        Two things learned the hard way, both measured 2026-08-05:

        * Watching for an open file descriptor DOES NOT WORK here. libcamera
          never opens the capture node, and pipewire/wireplumber permanently
          hold every node it does open -- including the imx708 subdev -- from
          boot on a completely idle system. That indicator would read "camera
          in use" 24 hours a day, which is worse than no indicator at all.
        * The reading LAGS by up to 5 s after streaming stops (the sensor's
          autosuspend delay). That over-reports "on", which is the safe
          direction for a privacy light, so it is left alone.

        Cost is one small read from sysfs, on the existing 5 s tick.
        """
        if not self._cam_file:
            return None
        try:
            return open(self._cam_file).read().strip() == "active"
        except OSError:
            return None

    def get(self, now: float | None = None) -> Health:
        now = now or time.time()
        if now - self._cache.checked_at < self.interval:
            # The systemd/clock probes are subprocesses and stay on the slow
            # tick. The camera check is a single sysfs read, and it must NOT
            # wait for that tick: a 5 s delay before the light comes on is the
            # unsafe direction for a privacy indicator. Turning off late is
            # fine; turning on late is not.
            if now - self._cam_checked >= self.camera_interval:
                self._cam_checked = now
                self._cache.camera_on = self._camera_on()
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
            camera_on=self._camera_on(),
            checked_at=now,
        )
        return self._cache
