"""Tool schema for hermes_voice.

FLAT, not the OpenAI {"type":"function","function":{...}} envelope. Hermes'
register_tool wants name/description/parameters at the top level -- every other
plugin in this repo does it that way. Wrapped, the schema registered fine and
the model called the tool, but the DECLARED PARAMETERS WERE NEVER PASSED: the
handler received an empty args dict and only context kwargs, so every reply
became "nothing to say". It failed at the far end, silently, and looked like a
handler bug for two rounds of fixing.
"""

SPEAK_SCHEMA = {
    "name": "speak",
    "description": (
        "Say a short reply OUT LOUD through the speaker in the room. Use this "
        "whenever the user spoke to you by voice, so they hear the answer "
        "without looking at a screen. One or two sentences -- this is speech, "
        "not a document. The full written reply is delivered separately."
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
}
