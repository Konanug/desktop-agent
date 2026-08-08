"""Authenticated Google API clients, and where the credentials live.

READ-ONLY BY DESIGN. The scopes below are `.readonly`, so this cannot send
mail, delete anything, or alter a calendar -- and that is not a policy note,
it is enforced by Google: a token minted for a readonly scope is refused for a
write call regardless of what the agent asks for.

That matters more here than in most places. The voice lane now has full tool
parity with Discord, so anything reachable by the agent is reachable by anyone
who can be heard in the room. "How many unread emails" being answerable by a
stranger is a small leak; "send an email as me" would not be. If write access
is ever wanted it should be a SEPARATE plugin with its own scopes, so the
decision is explicit and visible in the consent screen.

WHY THE LIBRARIES ARE NOT IN HERMES' VENV
`hermes update` can recreate that venv, which would silently remove them and
break these tools with a confusing ImportError. They are installed with
`pip --target` into the project's own directory and put on sys.path here.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

LIBS = Path.home() / ".local/share/hermes-pi/google-libs"
if str(LIBS) not in sys.path and LIBS.is_dir():
    sys.path.insert(0, str(LIBS))

# Under the OWNER's config, not ~/.hermes/ -- anything Hermes manages, Hermes
# can rewrite. Same placement as the camera token and the voice secret.
CONF = Path.home() / ".config" / "hermes-pi"
CLIENT_SECRET = CONF / "google-client-secret.json"
TOKEN = CONF / "google-token.json"

SCOPES = [
    "https://www.googleapis.com/auth/gmail.readonly",
    "https://www.googleapis.com/auth/calendar.readonly",
]


class NotConfigured(RuntimeError):
    """Raised with a message meant to be READ BY A HUMAN through the agent."""


def _creds():
    try:
        from google.auth.transport.requests import Request
        from google.oauth2.credentials import Credentials
    except ImportError as e:
        raise NotConfigured(
            f"Google libraries are not installed ({e}). Run "
            f"scripts/install-google.sh on the Pi.") from None

    if not TOKEN.exists():
        raise NotConfigured(
            "Google account is not connected yet. The owner needs to run "
            "`python3 scripts/google_auth.py` on the Pi once -- it prints a "
            "link to approve. Nothing can be read until then.")
    creds = Credentials.from_authorized_user_file(str(TOKEN), SCOPES)
    if creds and creds.expired and creds.refresh_token:
        creds.refresh(Request())
        TOKEN.write_text(creds.to_json())
        TOKEN.chmod(0o600)
    if not creds or not creds.valid:
        raise NotConfigured(
            "The stored Google token is no longer valid. The owner needs to "
            "re-run `python3 scripts/google_auth.py` on the Pi.")
    return creds


def service(name: str, version: str):
    from googleapiclient.discovery import build
    # cache_discovery=False: the default file cache writes into the process's
    # working directory and logs a warning on every call under a service
    # account-less setup. Nothing here benefits from it.
    return build(name, version, credentials=_creds(), cache_discovery=False)
