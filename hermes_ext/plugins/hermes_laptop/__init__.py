"""hermes_laptop -- name an action for the owner's laptop to perform.

The Pi NAMES; the laptop DECIDES. Same argument that put the gesture mapping on
the laptop: this machine cannot address that one, so the worst it can do is say
a word that still only reaches a fixed list the owner wrote themselves.
"""

from . import schemas, tools


def register(ctx) -> None:
    ctx.register_tool(
        name="laptop_do", toolset="hermes_laptop",
        schema=schemas.LAPTOP_DO, handler=tools.laptop_do,
        description="Ask the laptop to do a bound action by name", emoji="💻")


__all__ = ["register"]
