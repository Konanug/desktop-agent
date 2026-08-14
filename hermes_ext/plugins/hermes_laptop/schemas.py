"""Tool schema for hermes_laptop. FLAT -- see hermes_voice/schemas.py."""

LAPTOP_DO = {
    "name": "laptop_do",
    "description": (
        "Control the owner's laptop by naming an action THEY have already "
        "bound. Use it whenever they ask you to play, pause, skip, open "
        "something, or change what is on their screen.\n"
        "Commonly bound names:\n"
        "  music    PLAY PAUSE NEXT PREVIOUS VOLUME_UP VOLUME_DOWN MUTE\n"
        "  open     SPOTIFY GMAIL YOUTUBE CALENDAR GITHUB\n"
        "  window   MAXIMIZE MINIMIZE DESKTOP LOCK SCREENSHOT NEXT_DESKTOP\n"
        "This only SENDS the name. The laptop decides what it means and "
        "ignores names it does not know, so you cannot open or run arbitrary "
        "things, and it only works while their client is running -- you are "
        "told if nobody is listening."),
    "parameters": {
        "type": "object",
        "properties": {
            "action": {"type": "string",
                       "description": "Short name, e.g. GMAIL or YOUTUBE."},
        },
        "required": ["action"],
    },
}
