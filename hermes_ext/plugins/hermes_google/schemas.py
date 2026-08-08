"""Tool schemas for hermes_google. All read-only."""

GMAIL_UNREAD = {
    "type": "function",
    "function": {
        "name": "gmail_unread",
        "description": ("How many unread messages are in the owner's Gmail "
                        "inbox, and who they are from. Read-only."),
        "parameters": {
            "type": "object",
            "properties": {
                "max_results": {"type": "integer",
                                "description": "How many to list (1-25)."},
            },
        },
    },
}

GMAIL_SEARCH = {
    "type": "function",
    "function": {
        "name": "gmail_search",
        "description": ("Search the owner's Gmail using Gmail query syntax, "
                        "e.g. 'from:bank newer_than:7d' or 'has:attachment'. "
                        "Read-only: this cannot send, delete or modify."),
        "parameters": {
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "Gmail search query."},
                "max_results": {"type": "integer"},
            },
            "required": ["query"],
        },
    },
}

CALENDAR_AGENDA = {
    "type": "function",
    "function": {
        "name": "calendar_agenda",
        "description": ("Upcoming events on the owner's primary Google "
                        "Calendar, starting now. Read-only."),
        "parameters": {
            "type": "object",
            "properties": {
                "days": {"type": "integer",
                         "description": "How far ahead to look (1-30). 1 = today."},
                "max_results": {"type": "integer"},
            },
        },
    },
}
