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

## Sending email: the owner's rule

> **Never send an email without direct permission. The permission must be
> TYPED, never spoken, and must be a specific phrase with correct syntax.**

**Today this is absolute, because sending is impossible.** The token carries
only `.readonly` scopes and Google refuses a send server-side — verified, not
assumed:

```
send REFUSED by Google: HTTP 403  "Request had insufficient authentication scopes"
```

No send tool exists in the plugin either. `tests/test_send_consent.py` asserts
both, so adding one by accident fails the suite.

If send is ever wanted, the rule is already enforced by three layers, weakest
last:

1. **Scope.** A readonly token cannot send. Adding send needs a new Google
   consent screen — itself a typed act by the owner at a Google login.
2. **Structure.** A send tool must live in its own toolset that is absent from
   `platform_toolsets.webhook`, so the **voice lane cannot see it at all**.
   This is what makes "typed, not spoken" real: a tool handler *cannot* tell
   which platform invoked it — the registry does not pass the platform down —
   so a runtime "refuse if spoken" check would be a guess dressed as a control.
   There is nothing to refuse because there is nothing to call.
3. **Phrase.** `hermes_ext/plugins/hermes_google/consent.py`. Exact syntax:

   ```
   CONFIRM SEND TO <recipient> <your phrase>
   ```

   The phrase lives in `~/.config/hermes-pi/send-consent-phrase`, which only
   you write. **Missing file = nothing can ever be sent** — it fails closed.

**The recipient is part of the phrase on purpose.** A bare passphrase can be
reused: once it appears in a conversation, every later send in that
conversation is already authorised, including to an address you never approved.
Binding it to the recipient makes consent authorise one delivery to one person.

`check()` raises rather than returning a boolean, because a caller who forgets
to test a return value must still be unable to send.

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
