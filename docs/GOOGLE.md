# Gmail and Calendar

Read-only access to the owner's Gmail and Google Calendar, so "how many unread
emails do I have?" and "what's on today?" work from Discord or by voice.

**Not connected yet** — it needs a one-time OAuth consent that only you can do.
See `scripts/google_auth.py`, whose header is the checklist.

---

## Read-only, and enforced by Google

The OAuth scopes are `gmail.readonly` and `calendar.readonly`. That is not a
policy note: a token minted for a readonly scope is **refused server-side** for
a write call, whatever the agent asks. This cannot send mail, delete anything,
or alter a calendar.

That matters more here than it looks. The voice lane now has full tool parity
with Discord, so **anything the agent can reach, anyone audible in the room can
reach.** "How many unread emails" being answerable by a stranger is a small
leak. "Send an email as me" would not be. If write access is ever wanted it
belongs in a separate plugin with its own scopes, so the decision is explicit
and shows up on the consent screen rather than being inherited silently.

## Tools

| | |
|---|---|
| `gmail_unread` | count + senders + subjects of unread inbox mail |
| `gmail_search` | Gmail query syntax (`from:bank newer_than:7d`) |
| `calendar_agenda` | upcoming events, 1–30 days ahead |

Summaries are short and ordered, because these answers get **spoken**. The
agent can always ask for more; a wall of message bodies read aloud is useless.

## Setup

```bash
./scripts/install-google.sh      # libraries (already done)
python3 scripts/google_auth.py   # <- needs you; prints a link
systemctl --user restart hermes-gateway
```

The console flow prints a URL you open on **any** device and paste a code back.
No browser on the Pi, no SSH tunnel, no port forwarding — the same reasoning
that picked the device-code flow for the model provider (`docs/DECISIONS.md`
D6). It needs **Desktop app** credentials for that; a "Web application" client
requires a redirect URI this headless box does not have.

## Where things live

| | |
|---|---|
| `~/.config/hermes-pi/google-client-secret.json` | what you download from Google |
| `~/.config/hermes-pi/google-token.json` | the refresh token, `0600` |
| `~/.local/share/hermes-pi/google-libs/` | the API libraries, ~139 MB |

Both credential files are under the **owner's** config, not `~/.hermes/` —
anything Hermes manages, Hermes can rewrite. Same placement as the camera
token and the voice webhook secret. Neither is in this repo; `.gitignore`
already covers `*.json` credentials by name.

**The libraries are deliberately NOT in Hermes' venv.** `hermes update` can
recreate that venv, which would remove them and break these tools with a
confusing `ImportError` long after the update. They are installed with
`pip --target` and the plugin puts that directory on `sys.path` itself.

## When it is not connected

Every tool returns a plain sentence saying so, naming the script to run. It
does not raise, and it does not look like a bug — the agent reads the sentence
out and you know what to do.
