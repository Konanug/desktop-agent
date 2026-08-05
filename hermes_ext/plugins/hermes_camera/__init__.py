"""hermes_camera -- let Hermes actually see through the Pi's camera.

Registered through Hermes' supported plugin system (~/.hermes/plugins/), so
nothing in Hermes core is modified and `hermes update` cannot break it.

WHY THIS WORKS AT ALL
The provider in use is openai-codex, which reaches the ChatGPT backend over
OAuth rather than an API key. That path carries images: the model reports
image among its input modalities, so Hermes routes pictures natively, and a
tool result may itself carry an image via the multimodal envelope. So the
camera can hand the model real pixels -- no separate vision model, no local
captioning, no describing the room in words first.

These are TOOLS, not a background feed. The agent decides to look, as a
deliberate act, which is what makes the panel indicator and the journal audit
line meaningful: there is always a reason attached to every capture.

`check_fn` matters here beyond tidiness -- when the owner disables the camera
the tool should stop being offered at all, rather than being offered and then
refusing.
"""

from . import schemas, tools


def register(ctx) -> None:
    ctx.register_tool(
        name="camera_look",
        toolset="hermes_camera",
        schema=schemas.CAMERA_LOOK,
        handler=tools.camera_look,
        check_fn=tools.camera_available,
        description="Look through the Pi's camera and see the room now",
        emoji="📷",
    )
    ctx.register_tool(
        name="camera_watch",
        toolset="hermes_camera",
        schema=schemas.CAMERA_WATCH,
        handler=tools.camera_watch,
        check_fn=tools.camera_available,
        description="See several recent moments at once, to judge motion",
        emoji="🎞",
    )
