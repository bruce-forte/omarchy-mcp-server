"""How an agent talks to the person, rather than to the terminal.

The terminal is not where the user is looking. An agent that finishes a long
job, or needs to say something while the user is in another window, has no way
to reach them without these.
"""

from __future__ import annotations

import json

from mcp.types import ToolAnnotations

from ..config import Config
from ..stats import Stats
from ._shared import enabled, run_route

URGENCIES = ("low", "normal", "critical")


def register(mcp, config: Config, log, stats: Stats) -> None:
    if enabled(config, "omarchy_notify"):

        @mcp.tool(
            name="omarchy_notify",
            title="Send a desktop notification",
            description=(
                "Show a desktop notification. Use this to reach the user when they are "
                "not looking at the terminal -- a long job finishing, something that "
                "needs a decision. `urgency` critical stays on screen until dismissed, "
                "so keep it for things that genuinely cannot wait."
            ),
            annotations=ToolAnnotations(
                readOnlyHint=False, destructiveHint=False, idempotentHint=False,
                openWorldHint=False,
            ),
        )
        async def omarchy_notify(
            headline: str,
            description: str = "",
            urgency: str = "normal",
            glyph: str = "",
            timeout_ms: int = 0,
        ) -> str:
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
                config=config, stats=stats, log=log, tool="omarchy_notify",
            )

    if enabled(config, "omarchy_osd"):

        @mcp.tool(
            name="omarchy_osd",
            title="Show a transient on-screen display",
            description=(
                "Flash a message, icon, or progress bar over the screen and let it fade. "
                "Unlike a notification it leaves nothing in the notification history, so "
                "it suits progress and acknowledgements that are not worth keeping."
            ),
            annotations=ToolAnnotations(
                readOnlyHint=False, destructiveHint=False, idempotentHint=False,
                openWorldHint=False,
            ),
        )
        async def omarchy_osd(
            message: str = "",
            icon: str = "",
            progress: int = -1,
            duration_ms: int = 0,
        ) -> str:
            args: list[str] = []
            if message:
                args += ["-m", message]
            if icon:
                args += ["-i", icon]
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
                config=config, stats=stats, log=log, tool="omarchy_osd",
            )
