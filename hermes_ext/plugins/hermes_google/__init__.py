"""hermes_google -- read-only Gmail and Google Calendar.

Registered through Hermes' supported plugin system, so nothing in Hermes core
is modified and `hermes update` cannot break it.

READ-ONLY, enforced by Google rather than by us: the OAuth scopes are
`.readonly`, so a write call is refused server-side no matter what the agent
asks for. That is deliberate given the voice lane now has full tool parity --
anything reachable by the agent is reachable by anyone audible in the room.
"""

from . import schemas, tools


def register(ctx) -> None:
    ctx.register_tool(
        name="gmail_unread", toolset="hermes_google",
        schema=schemas.GMAIL_UNREAD, handler=tools.gmail_unread,
        description="Unread Gmail count and senders", emoji="📬")
    ctx.register_tool(
        name="gmail_search", toolset="hermes_google",
        schema=schemas.GMAIL_SEARCH, handler=tools.gmail_search,
        description="Search Gmail (read-only)", emoji="🔎")
    ctx.register_tool(
        name="calendar_agenda", toolset="hermes_google",
        schema=schemas.CALENDAR_AGENDA, handler=tools.calendar_agenda,
        description="Upcoming Google Calendar events", emoji="📅")


__all__ = ["register"]
