"""Ask the laptop to do something it already agreed to do.

WHAT THIS CAN AND CANNOT DO. It puts a NAME on a stream. That is all. It does
not open anything, press anything, or run anything -- the laptop is a separate
machine that this one cannot address, and it pulls from the Pi rather than
being pushed to.

So the blast radius is the same as a gesture's: a word that reaches whatever
fixed list the laptop's owner wrote in their own config, on their own disk. If
they have no binding for the name, nothing happens. Nothing said here can
create a new one.

That is deliberate and it is the reason this is a safe tool to give an agent
that anyone in the room can talk to.
"""

from __future__ import annotations

import json
import os
import re
import urllib.error
import urllib.request
from pathlib import Path

import sys as _sys
_sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from _argshim import arg as _arg      # noqa: E402

# Names are a closed shape, not free text: letters, digits and underscore. A
# name that could contain punctuation or whitespace would be a place to smuggle
# something into whatever the laptop does with it.
_NAME = re.compile(r"^[A-Za-z0-9_]{1,32}$")


def _token() -> str:
    try:
        return (Path.home() / ".config/hermes-pi/camera-stream.token"
                ).read_text().strip()
    except OSError:
        return ""


def laptop_do(args=None, **_kw) -> str:
    """Emit a named intent for the laptop client to act on."""
    name = str(_arg(args, _kw, "action", "")).strip()
    if not _NAME.match(name):
        return ("Refused: action must be a short plain name like GMAIL or "
                "YOUTUBE (letters, digits, underscore).")
    tok = _token()
    if not tok:
        return "The camera stream token is missing, so the laptop cannot be reached."
    port = os.environ.get("HERMES_CAMERA_STREAM_PORT", "8081")
    url = f"http://127.0.0.1:{port}/intent?k={tok}"
    body = json.dumps({"action": name.upper()}).encode()
    req = urllib.request.Request(url, data=body,
                                 headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=5) as r:
            doc = json.loads(r.read())
    except urllib.error.HTTPError as e:
        return f"Could not publish the intent: HTTP {e.code}."
    except Exception as e:
        return (f"Could not reach the camera service ({e.__class__.__name__}). "
                f"Is hermes-camera running?")
    if not doc.get("subscribers"):
        return (f"Published {name.upper()}, but NO LAPTOP IS LISTENING right "
                f"now, so nothing will happen. The owner needs "
                f"hermes_gesture.py running.")
    return (f"Asked the laptop to do {name.upper()}. Whether anything happens "
            f"depends on whether they have bound that name -- this cannot "
            f"create new actions.")
