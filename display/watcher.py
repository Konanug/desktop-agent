"""Reads the state file the Hermes hook publishes.

Polls `st_mtime_ns` rather than using inotify. A stat(2) is a few microseconds,
so 10 Hz costs nothing measurable, and it avoids either a third-party
dependency or a hand-rolled ctypes inotify binding. It also sidesteps the
classic inotify trap here: the producer writes via `rename(2)`, which replaces
the inode, so a watch on the *file* stops firing after the first update and you
must watch the directory instead. Polling has no such failure mode.

Reading is total: any error -- missing file, truncated JSON, wrong schema --
yields None, and the caller renders a degraded state rather than crashing.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

SCHEMA_MAJOR = 1


def default_state_path() -> Path:
    base = os.environ.get("XDG_RUNTIME_DIR") or f"/run/user/{os.getuid()}"
    return Path(base) / "hermes-display" / "state.json"


class StateWatcher:
    def __init__(self, path: Path | None = None):
        self.path = path or default_state_path()
        self._mtime_ns = -1
        self._cached: dict[str, Any] | None = None
        self._warned = False

    def poll(self) -> tuple[dict[str, Any] | None, bool]:
        """Return (state, changed). `state` is None when unreadable."""
        try:
            st = self.path.stat()
        except FileNotFoundError:
            # Normal before Hermes' first write, and after a gateway stop.
            if self._cached is not None or self._mtime_ns != -1:
                self._mtime_ns, self._cached = -1, None
                return None, True
            return None, False
        except Exception:
            return self._cached, False

        if st.st_mtime_ns == self._mtime_ns:
            return self._cached, False

        self._mtime_ns = st.st_mtime_ns
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
            if not isinstance(data, dict):
                raise ValueError("state root is not an object")
            if int(data.get("schema", 0)) != SCHEMA_MAJOR:
                # A future producer may have changed the contract; guessing at
                # a schema we do not understand is worse than showing unknown.
                if not self._warned:
                    print(f"[watcher] unsupported schema {data.get('schema')!r}", flush=True)
                    self._warned = True
                self._cached = None
                return None, True
            self._warned = False
            self._cached = data
            return data, True
        except Exception as e:
            # A torn read should be impossible (atomic rename), so this means
            # genuinely bad content. Warn once, not every poll.
            if not self._warned:
                print(f"[watcher] unreadable state: {e}", flush=True)
                self._warned = True
            self._cached = None
            return None, True
