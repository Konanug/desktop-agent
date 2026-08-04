"""Tool schemas -- what the model sees.

The `description` is the entire interface as far as the agent is concerned:
it decides WHEN to call these from this text alone. Each one therefore states
what the tool does, where it appears, and that the effect is temporary.
"""

DISPLAY_SHOW_IMAGE = {
    "name": "display_show_image",
    "description": (
        "Show an image on the small physical display attached to this Raspberry Pi. "
        "Use when the user asks you to display, show, or put an image on the screen. "
        "Pass the https URL of a Discord attachment or CDN image. The picture "
        "appears for a short time and then the display returns to its normal status "
        "view by itself. Only Discord-hosted images can be shown."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "url": {
                "type": "string",
                "description": "https URL of the image, from a Discord attachment or CDN.",
            },
            "seconds": {
                "type": "integer",
                "description": "How long to show it, 5-600. Defaults to 60.",
            },
        },
        "required": ["url"],
    },
}

DISPLAY_SHOW_TEXT = {
    "name": "display_show_text",
    "description": (
        "Show a short line of text on the small physical display attached to this "
        "Raspberry Pi. Use for a brief note, reminder, or status the user wants "
        "visible in the room. Keep it under about 100 characters -- the panel is "
        "small. It clears itself after a short time."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "text": {"type": "string", "description": "The line to display (<=120 chars)."},
            "seconds": {
                "type": "integer",
                "description": "How long to show it, 5-600. Defaults to 60.",
            },
        },
        "required": ["text"],
    },
}

DISPLAY_CLEAR = {
    "name": "display_clear",
    "description": (
        "Return the Pi's physical display to its normal status view immediately, "
        "cancelling any image or text currently shown."
    ),
    "parameters": {"type": "object", "properties": {}},
}
