"""Gmail and Calendar tools. Read-only, and shaped for being READ ALOUD.

Every one of these can be triggered by voice, and voice replies are spoken.
So the summaries are short, ordered by what a person actually asks for, and
never dump a wall of message bodies. The agent can always ask for more.

NOTHING HERE TAKES A QUERY THAT REACHES THE ACCOUNT UNSANITISED beyond Gmail's
own search syntax, and nothing takes an address, a recipient, or a body --
these tools cannot send, delete or modify. See google_client.SCOPES.
"""

from __future__ import annotations

import datetime as _dt

from .google_client import NotConfigured, service

MAX_ITEMS = 25


def _err(e: Exception) -> str:
    if isinstance(e, NotConfigured):
        return str(e)
    return f"Google request failed: {e.__class__.__name__}: {e}"


def gmail_unread(max_results: int = 10, **_kw) -> str:
    """How many unread, and who they are from."""
    try:
        n = max(1, min(int(max_results or 10), MAX_ITEMS))
        svc = service("gmail", "v1")
        res = svc.users().messages().list(
            userId="me", q="is:unread in:inbox", maxResults=n).execute()
        ids = res.get("messages", []) or []
        total = res.get("resultSizeEstimate", len(ids))
        if not ids:
            return "No unread messages in the inbox."
        lines = []
        for m in ids[:n]:
            full = svc.users().messages().get(
                userId="me", id=m["id"], format="metadata",
                metadataHeaders=["From", "Subject"]).execute()
            h = {x["name"]: x["value"]
                 for x in full.get("payload", {}).get("headers", [])}
            frm = h.get("From", "unknown").split("<")[0].strip().strip('"')
            lines.append(f"- {frm}: {h.get('Subject', '(no subject)')[:80]}")
        more = f" (showing {len(lines)})" if total > len(lines) else ""
        return (f"{total} unread message{'s' if total != 1 else ''}"
                f"{more}:\n" + "\n".join(lines))
    except Exception as e:
        return _err(e)


def gmail_search(query: str = "", max_results: int = 10, **_kw) -> str:
    """Search the mailbox with Gmail's own query syntax."""
    query = str(query or "").strip()
    if not query:
        return "Give a search query, e.g. 'from:bank newer_than:7d'."
    try:
        n = max(1, min(int(max_results or 10), MAX_ITEMS))
        svc = service("gmail", "v1")
        res = svc.users().messages().list(
            userId="me", q=query, maxResults=n).execute()
        ids = res.get("messages", []) or []
        if not ids:
            return f"Nothing matched {query!r}."
        lines = []
        for m in ids:
            full = svc.users().messages().get(
                userId="me", id=m["id"], format="metadata",
                metadataHeaders=["From", "Subject", "Date"]).execute()
            h = {x["name"]: x["value"]
                 for x in full.get("payload", {}).get("headers", [])}
            frm = h.get("From", "unknown").split("<")[0].strip().strip('"')
            lines.append(f"- {frm}: {h.get('Subject', '(no subject)')[:80]}"
                         f"  [{h.get('Date', '')[:16]}]")
        return f"{len(lines)} match(es) for {query!r}:\n" + "\n".join(lines)
    except Exception as e:
        return _err(e)


def calendar_agenda(days: int = 1, max_results: int = 10, **_kw) -> str:
    """What is coming up, from now."""
    try:
        d = max(1, min(int(days or 1), 30))
        n = max(1, min(int(max_results or 10), MAX_ITEMS))
        now = _dt.datetime.now(_dt.timezone.utc)
        svc = service("calendar", "v3")
        res = svc.events().list(
            calendarId="primary",
            timeMin=now.isoformat(),
            timeMax=(now + _dt.timedelta(days=d)).isoformat(),
            singleEvents=True, orderBy="startTime",
            maxResults=n).execute()
        items = res.get("items", []) or []
        if not items:
            return (f"Nothing on the calendar for the next "
                    f"{d} day{'s' if d != 1 else ''}.")
        lines = []
        for ev in items:
            start = ev.get("start", {})
            when = start.get("dateTime") or start.get("date") or "?"
            # All-day events carry a bare date; say so rather than inventing
            # a time that was never set.
            when = when[:16].replace("T", " ") if "T" in when else f"{when} (all day)"
            lines.append(f"- {when}  {ev.get('summary', '(no title)')[:70]}")
        return (f"{len(lines)} event(s) in the next "
                f"{d} day{'s' if d != 1 else ''}:\n" + "\n".join(lines))
    except Exception as e:
        return _err(e)
