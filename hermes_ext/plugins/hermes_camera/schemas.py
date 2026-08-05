"""Tool schemas for the camera.

Arguments are deliberately minimal. The model chooses WHY it is looking and how
much detail it needs -- never a path, a filename, a URL, or how long to keep
anything. Same discipline as hermes_display: the surface the model can steer is
the surface that has to be defended.

The descriptions do real work here. They are the only place the model learns
that looking is expensive and that a frame is permanent, so they are written to
discourage idle re-looking rather than merely describe the call.
"""

CAMERA_LOOK = {
    "name": "camera_look",
    "description": (
        "Look through the Raspberry Pi's camera and see the room right now. "
        "Returns one live frame that you can see natively. Use this when the "
        "user asks what you can see, what they are holding, what something "
        "looks like, or to read something they are showing you. "
        "The image stays in this conversation permanently and is re-sent every "
        "turn, so look once and answer from what you saw rather than looking "
        "repeatedly. Limited to 3 captures per turn."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "reason": {
                "type": "string",
                "description": (
                    "Short statement of why you are looking, e.g. 'counting "
                    "fingers'. Shown on the Pi's physical panel and written to "
                    "the system log so the owner can see why the camera was "
                    "used. Keep it under 48 characters."
                ),
            },
            "detail": {
                "type": "string",
                "enum": ["normal", "fine"],
                "description": (
                    "'normal' (768x432) is right for almost everything. Use "
                    "'fine' (1024x576) only when you need to read small text "
                    "or resolve fine detail -- it costs roughly twice the "
                    "context, permanently."
                ),
            },
        },
        "required": ["reason"],
    },
}

CAMERA_WATCH = {
    "name": "camera_watch",
    "description": (
        "See MOTION rather than a single instant. Returns several moments from "
        "the last few seconds arranged as one image, so you can tell what "
        "someone is doing rather than just what is in front of the camera. "
        "Use this for 'what am I doing', 'what did I just do', or anything "
        "involving movement. Costs the same as one camera_look. "
        "If the camera was already awake it can show the seconds BEFORE the "
        "user's message; if it was asleep it can only show the seconds after, "
        "and the result will say which."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "reason": {
                "type": "string",
                "description": (
                    "Short statement of why you are looking. Shown on the "
                    "panel and logged. Under 48 characters."
                ),
            },
            "detail": {
                "type": "string",
                "enum": ["normal", "fine"],
                "description": "'normal' unless the tiles need to be sharper.",
            },
        },
        "required": ["reason"],
    },
}
