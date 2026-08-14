"""Tool schema for hermes_laptop. FLAT -- see hermes_voice/schemas.py."""

LAPTOP_DO = {
    "name": "laptop_do",
    "description": (
        "Ask the owner's laptop to perform one of the actions THEY have "
        "already bound, by name -- for example GMAIL, YOUTUBE, MAXIMIZE. This "
        "only sends the name; the laptop decides what it means and ignores "
        "names it does not know, so you cannot open arbitrary things. Use it "
        "when the owner asks you to open or control something on their "
        "computer. It only works while their gesture client is running."),
    "parameters": {
        "type": "object",
        "properties": {
            "action": {"type": "string",
                       "description": "Short name, e.g. GMAIL or YOUTUBE."},
        },
        "required": ["action"],
    },
}
