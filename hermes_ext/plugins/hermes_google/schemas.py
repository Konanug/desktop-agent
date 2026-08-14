"""Tool schemas for hermes_google. All read-only.

FLAT, not the OpenAI {"type":"function","function":{...}} envelope -- see
hermes_voice/schemas.py for what wrapping them silently breaks.
"""

GMAIL_UNREAD = {
    "name": "gmail_unread",
    "description": ("How many unread messages are in the owner's Gmail INBOX, "
                    "and who they are from. Read-only. For spam, trash or "
                    "anything else, use gmail_search."),
    "parameters": {
        "type": "object",
        "properties": {
            "max_results": {"type": "integer",
                            "description": "How many to list (1-25)."},
        },
    },
}

GMAIL_SEARCH = {
    "name": "gmail_search",
    "description": (
        "Search the owner's Gmail using Gmail's own query syntax. Searches "
        "EVERYWHERE including spam and trash. Read-only.\n"
        "Useful queries:\n"
        "  in:spam / in:trash / in:sent / in:anywhere\n"
        "  is:unread / is:starred / is:important\n"
        "  from:someone@x.com / to:me / subject:invoice\n"
        "  newer_than:2d / older_than:1m / after:2026/01/31\n"
        "  has:attachment / filename:pdf / larger:5M\n"
        "  category:promotions / category:social / category:updates\n"
        "Combine them: 'in:spam newer_than:7d'."),
    "parameters": {
        "type": "object",
        "properties": {
            "query": {"type": "string", "description": "Gmail search query."},
            "max_results": {"type": "integer"},
        },
        "required": ["query"],
    },
}

GMAIL_READ = {
    "name": "gmail_read",
    "description": (
        "Read the actual text of one email. Give a `query` to find it (the "
        "newest match is read, and it says which one it picked) or a "
        "`message_id` if you already have one. Searches spam and trash too. "
        "Read-only."),
    "parameters": {
        "type": "object",
        "properties": {
            "query": {"type": "string",
                      "description": "Gmail query, e.g. 'from:bank newer_than:3d'."},
            "message_id": {"type": "string"},
            "max_chars": {"type": "integer",
                          "description": "Body characters to return (200-8000)."},
        },
    },
}

CALENDAR_AGENDA = {
    "name": "calendar_agenda",
    "description": ("Upcoming events on the owner's primary Google Calendar, "
                    "starting now. Read-only."),
    "parameters": {
        "type": "object",
        "properties": {
            "days": {"type": "integer",
                     "description": "How far ahead to look (1-30). 1 = today."},
            "max_results": {"type": "integer"},
        },
    },
}
