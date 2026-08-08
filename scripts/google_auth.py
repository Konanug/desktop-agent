#!/usr/bin/env python3
"""Connect a Google account, once. Read-only Gmail + Calendar.

    python3 scripts/google_auth.py

WHAT YOU HAVE TO DO FIRST (about five minutes, one time)

  1. https://console.cloud.google.com/  ->  create a project (any name).
  2. APIs & Services -> Library -> enable BOTH:
        Gmail API
        Google Calendar API
  3. APIs & Services -> OAuth consent screen:
        User type: External
        Fill the three required fields, Save
        Audience -> Test users -> ADD YOUR OWN GMAIL ADDRESS
        (without this the login is refused with "app not verified")
  4. APIs & Services -> Credentials -> Create credentials
        -> OAuth client ID -> Application type: **Desktop app**
        -> Download JSON
  5. Put that file on the Pi at:
        ~/.config/hermes-pi/google-client-secret.json

Then run this. It prints a URL; open it on ANY device, approve, and paste the
code back. No browser is needed on the Pi.

WHY DESKTOP-APP CREDENTIALS AND THE CONSOLE FLOW
A "Web application" client requires a redirect URI reachable from your browser,
which this headless box on a LAN does not have. A Desktop client supports the
out-of-band console flow, which is the same reason `hermes auth add
openai-codex --no-browser` was chosen over the app-server runtime (D6). No SSH
tunnel, no port forwarding.

SCOPES ARE READ-ONLY and that is enforced by Google, not by us: a token minted
for a readonly scope is refused for a write call. This cannot send mail or
change your calendar. If write access is ever wanted, it needs its own consent
screen so the decision is visible.
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, str(Path.home() / ".local/share/hermes-pi/google-libs"))

CONF = Path.home() / ".config" / "hermes-pi"
CLIENT_SECRET = CONF / "google-client-secret.json"
TOKEN = CONF / "google-token.json"
SCOPES = [
    "https://www.googleapis.com/auth/gmail.readonly",
    "https://www.googleapis.com/auth/calendar.readonly",
]


def main() -> int:
    if not CLIENT_SECRET.exists():
        print(__doc__)
        print(f"\nMISSING: {CLIENT_SECRET}")
        return 2
    try:
        from google_auth_oauthlib.flow import InstalledAppFlow
    except ImportError:
        print("Google libraries missing. Run: ./scripts/install-google.sh")
        return 2

    flow = InstalledAppFlow.from_client_secrets_file(
        str(CLIENT_SECRET), SCOPES,
        redirect_uri="urn:ietf:wg:oauth:2.0:oob")
    url, _ = flow.authorization_url(prompt="consent", access_type="offline")
    print("\nOpen this on any device and approve:\n")
    print(f"  {url}\n")
    code = input("Paste the code here: ").strip()
    if not code:
        print("nothing pasted")
        return 1
    flow.fetch_token(code=code)

    CONF.mkdir(mode=0o700, parents=True, exist_ok=True)
    TOKEN.write_text(flow.credentials.to_json())
    TOKEN.chmod(0o600)                     # a refresh token is a credential
    print(f"\nSaved to {TOKEN} (0600).")

    # Prove it works now rather than leaving it to fail later inside the agent.
    try:
        from googleapiclient.discovery import build
        svc = build("gmail", "v1", credentials=flow.credentials,
                    cache_discovery=False)
        prof = svc.users().getProfile(userId="me").execute()
        print(f"Connected as {prof.get('emailAddress')}, "
              f"{prof.get('messagesTotal')} messages total.")
    except Exception as e:
        print(f"Saved, but the test call failed: {e}")
        return 1

    print("\nNow restart the gateway:  systemctl --user restart hermes-gateway")
    print('Then ask: "how many unread emails do I have?"')
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        print()
        sys.exit(0)
