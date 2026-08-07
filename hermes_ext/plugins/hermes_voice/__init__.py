"""hermes_voice -- let the agent answer OUT LOUD in the room.

Registered through Hermes' supported plugin system (~/.hermes/plugins/), so
nothing in Hermes core is modified and `hermes update` cannot break it. Same
shape as hermes_display.

WHY A TOOL AND NOT A DELIVERY TARGET
The webhook adapter that carries voice into Hermes is fire-and-forget: the POST
returns 202 and the answer goes wherever `deliver:` names, with no synchronous
return and no HTTP callback. So the voice service cannot receive the reply it
caused. The alternative was scraping it from the gateway journal -- brittle
parsing of a line truncated at 200 characters.

It also DEGRADES WELL, which is the stronger argument: if the model does not
call this, the reply still reaches Discord and only the audio is missing.
"""

from . import schemas, tools


def register(ctx) -> None:
    ctx.register_tool(
        name="speak",
        toolset="hermes_voice",
        schema=schemas.SPEAK_SCHEMA,
        handler=tools.speak,
        description="Say a short reply out loud through the Pi's speaker",
        emoji="🔊",
    )


__all__ = ["register"]
