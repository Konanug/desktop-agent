"""Delivering a transcript to Hermes, and the limits on doing so.

THE TRANSCRIPT IS DATA, NOT INSTRUCTIONS, and this is the whole security story
of the voice lane. Everything a microphone hears becomes text that goes into an
agent's prompt: a podcast, a television, a guest, a video call playing on a
speaker the mic can reach. None of them are the owner, and none of them are
covered by the Discord allowlist.

Three things are done about that, in descending order of how much they help:

  1. THE LANE IS NARROWED. platform_toolsets.webhook strips the agent down to
     clarify/memory/vision/web plus this project's own camera and display
     plugins. VERIFIED, not assumed -- terminal and code_execution are absent
     from the resolved toolset, and the control case (explicitly adding
     terminal back) proves the resolver is consulted rather than ignored.
     See docs/SECURITY.md.

  2. THE TRANSCRIPT IS FENCED. The route prompt wraps it in explicit delimiters
     and tells the agent to treat the contents as data. This is a real
     mitigation and it is also the weakest of the three, because it is a
     request to a language model rather than a mechanism. It is written down
     here so nobody later mistakes it for a boundary.

  3. THE RATE IS BOUNDED. Sliding windows, so they cannot wedge (trap 19): a
     minimum gap, a per-minute cap and a per-hour cap. A television talking to
     itself all evening reaches the hour cap and stops.

WHY HMAC ON A LOOPBACK SOCKET
The listener binds to 127.0.0.1, so the signature is not what keeps strangers
out -- the bind does. It keeps out anything ELSE already on this box that can
open a socket, which is a different and smaller claim, and it costs one line.
V2 binds a timestamp so a captured request cannot be replayed.
"""

from __future__ import annotations

import collections
import hashlib
import hmac
import json
import time
import urllib.error
import urllib.request

from . import protocol


class RateLimit:
    """Sliding windows only. A limit that can reach a state it never leaves is
    worse than no limit, because it fails closed and silently -- trap 19, where
    the camera's per-turn counter never reset and refused permanently."""

    def __init__(self, min_gap: float = protocol.MIN_GAP,
                 per_min: int = protocol.MAX_PER_MIN,
                 per_hour: int = protocol.MAX_PER_HOUR):
        self.min_gap, self.per_min, self.per_hour = min_gap, per_min, per_hour
        self._recent: collections.deque = collections.deque()
        # None, not 0.0: time.monotonic()'s origin is undefined, so a zero
        # sentinel means "just fired" on a machine whose clock starts near zero
        # and swallows the first utterance of the session (trap 28).
        self._last: float | None = None

    def check(self, mono: float | None = None) -> str | None:
        """None if allowed, else a short reason."""
        mono = time.monotonic() if mono is None else mono
        while self._recent and mono - self._recent[0] > 3600.0:
            self._recent.popleft()
        if self._last is not None and mono - self._last < self.min_gap:
            return f"min gap {self.min_gap:.0f}s"
        if sum(1 for t in self._recent if mono - t <= 60.0) >= self.per_min:
            return f"{self.per_min}/min"
        if len(self._recent) >= self.per_hour:
            return f"{self.per_hour}/hour"
        return None

    def record(self, mono: float | None = None) -> None:
        mono = time.monotonic() if mono is None else mono
        self._recent.append(mono)
        self._last = mono

    @property
    def in_last_hour(self) -> int:
        return len(self._recent)


def load_secret() -> str | None:
    try:
        s = protocol.secret_path().read_text().strip()
        return s or None
    except OSError:
        return None


def post(text: str, url: str = protocol.WEBHOOK_URL,
         secret: str | None = None, timeout: float = 10.0) -> tuple[bool, str]:
    """Signed POST of one transcript. Returns (ok, detail).

    Returns rather than raises: a delivery failure must not take down a service
    whose main job is to keep listening. The gateway restarting is a normal
    event and this will simply fail for a few seconds and carry on.
    """
    secret = load_secret() if secret is None else secret
    if not secret:
        return False, f"no secret at {protocol.secret_path()}"
    body = json.dumps({"type": "voice", "text": text}).encode()
    ts = str(int(time.time()))
    sig = hmac.new(secret.encode(), ts.encode() + b"." + body,
                   hashlib.sha256).hexdigest()
    req = urllib.request.Request(url, data=body, headers={
        "Content-Type": "application/json",
        "X-Webhook-Timestamp": ts,
        "X-Webhook-Signature-V2": sig,
    })
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return True, f"HTTP {r.status}"
    except urllib.error.HTTPError as e:
        return False, f"HTTP {e.code} {e.read()[:120].decode(errors='replace')}"
    except Exception as e:
        return False, f"{e.__class__.__name__}: {e}"
