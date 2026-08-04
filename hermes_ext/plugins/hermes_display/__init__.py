"""hermes_display -- put things on the Pi's physical panel.

Registered through Hermes' supported plugin system (~/.hermes/plugins/), so
nothing in Hermes core is modified and `hermes update` cannot break it.

Design note: these are TOOLS, not message parsing. The agent decides to show
something as a deliberate action, which means it works for any phrasing, in any
language, and composes with the rest of its reasoning -- rather than depending
on us pattern-matching "show me ..." out of message text.
"""

from . import schemas, tools


def register(ctx) -> None:
    ctx.register_tool(
        name="display_show_image",
        toolset="hermes_display",
        schema=schemas.DISPLAY_SHOW_IMAGE,
        handler=tools.display_show_image,
        description="Show an image on the Pi's physical display",
        emoji="🖼",
    )
    ctx.register_tool(
        name="display_show_text",
        toolset="hermes_display",
        schema=schemas.DISPLAY_SHOW_TEXT,
        handler=tools.display_show_text,
        description="Show a short line of text on the Pi's physical display",
        emoji="📋",
    )
    ctx.register_tool(
        name="display_clear",
        toolset="hermes_display",
        schema=schemas.DISPLAY_CLEAR,
        handler=tools.display_clear,
        description="Return the Pi's display to its normal status view",
        emoji="🧹",
    )
