"""Tool schema for hermes_voice."""

SPEAK_SCHEMA = {
    "type": "function",
    "function": {
        "name": "speak",
        "description": (
            "Say a short reply OUT LOUD through the speaker in the room. Use "
            "this when the user spoke to you by voice, so they can hear the "
            "answer without looking at a screen. Keep it to a sentence or two "
            "-- this is speech, not a document. Your full reply is delivered "
            "in writing separately, so do not repeat yourself at length."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "text": {
                    "type": "string",
                    "description": "What to say. One or two sentences.",
                },
            },
            "required": ["text"],
        },
    },
}
