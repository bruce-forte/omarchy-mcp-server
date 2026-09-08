"""How an agent talks to the person, rather than to the terminal.

The terminal is not where the user is looking. An agent that finishes a long
job, or needs to say something while the user is in another window, has no way
to reach them without these.
"""

from __future__ import annotations

import json

from mcp.server.mcpserver import Context
from mcp.types import ToolAnnotations

from ..settings import Settings
from ..stats import Stats
from ._shared import run_route
from .catalogue import Catalogue

#: The only three values `urgency` may take, checked below rather than trusted.
URGENCIES = ("low", "normal", "critical")


def register(tools: Catalogue, settings: Settings, log, stats: Stats) -> None:
    """Declare the two tools that put something in front of the person.

    Both are ``async def`` because they end in ``await run_route(...)``: they go
    through the same gate and the same executor as `omarchy_run`, which is what
    `_shared.run_route` is.
    """

    @tools.tool(
        name="omarchy_notify",
        title="Send a desktop notification",
        description=(
            "Show a desktop notification. Use this to reach the user when they are "
            "not looking at the terminal -- a long job finishing, something that "
            "needs a decision. `urgency` critical stays on screen until dismissed, "
            "so keep it for things that genuinely cannot wait."
        ),
        annotations=ToolAnnotations(
            read_only_hint=False, destructive_hint=False, idempotent_hint=False,
            open_world_hint=False,
        ),
    )
    async def omarchy_notify(
        headline: str,
        description: str = "",
        urgency: str = "normal",
        glyph: str = "",
        timeout_ms: int = 0,
        ctx: Context | None = None,
    ) -> str:
        """Raise a desktop notification through `omarchy notification send`."""
        if urgency not in URGENCIES:
            return json.dumps(
                {"error": f"urgency must be one of {', '.join(URGENCIES)}"}, indent=2
            )

        args = ["-u", urgency]
        if glyph:
            args += ["-g", glyph]
        if timeout_ms > 0:
            args += ["-t", str(timeout_ms)]
        # The headline goes last, so that a headline beginning with a dash
        # is still a headline.
        args.append(headline)
        if description:
            args.append(description)

        return await run_route(
            "omarchy notification send", args,
            config=settings.current, perms=settings.permissions,
            unreviewed=settings.unreviewed,
            stats=stats, log=log, tool="omarchy_notify", ctx=ctx,
        )

    @tools.tool(
        name="omarchy_osd",
        title="Show a transient on-screen display",
        description=(
            "Flash a message, icon, or progress bar over the screen and let it fade. "
            "Unlike a notification it leaves nothing in the notification history, so "
            "it suits progress and acknowledgements that are not worth keeping."
        ),
        annotations=ToolAnnotations(
            read_only_hint=False, destructive_hint=False, idempotent_hint=False,
            open_world_hint=False,
        ),
    )
    async def omarchy_osd(
        message: str = "",
        icon: str = "",
        progress: int = -1,
        duration_ms: int = 0,
        ctx: Context | None = None,
    ) -> str:
        """Flash an on-screen display through `omarchy osd`.

        Arguments are built up as flags only for the values actually given, so
        the command is asked for exactly what the caller asked for.
        """
        args: list[str] = []
        if message:
            args += ["-m", message]
        if icon:
            args += ["-i", icon]
        # ``-1`` is the default and means "no progress bar", so the range check
        # doubles as the presence check.
        if 0 <= progress <= 100:
            args += ["-p", str(progress)]
        if duration_ms > 0:
            args += ["-d", str(duration_ms)]
        if not args:
            return json.dumps(
                {"error": "give at least one of message, icon, or progress"}, indent=2
            )

        return await run_route(
            "omarchy osd", args,
            config=settings.current, perms=settings.permissions,
            unreviewed=settings.unreviewed,
            stats=stats, log=log, tool="omarchy_osd", ctx=ctx,
        )
