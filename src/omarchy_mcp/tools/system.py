"""One call for the questions that otherwise take eight."""

from __future__ import annotations

import json

from mcp.types import ToolAnnotations

from ..config import Config
from ..settings import Settings
from ..stats import Stats
from ..status import gather
from ._shared import threaded

# Re-exported for tests and for the resource, which answer the same question.
__all__ = ["register"]


def register(mcp, settings: Settings, log, stats: Stats) -> None:
    if "omarchy_system_status" in set(settings.current.disabled_tools):
        return

    @mcp.tool(
        name="omarchy_system_status",
        title="How is this machine doing",
        description=(
            "Report CPU and memory, battery, network, current theme and background, "
            "idle state, font, and focused monitor, in a single call. Every field is "
            "also available individually through omarchy_run; this exists so that "
            "answering a question about the machine does not take eight round trips. "
            "A field is null when the machine has no such thing, such as battery on a "
            "desktop."
        ),
        annotations=ToolAnnotations(
            readOnlyHint=True, destructiveHint=False, idempotentHint=True, openWorldHint=False
        ),
    )
    @threaded
    def omarchy_system_status() -> str:
        with stats.call("omarchy_system_status"):
            return json.dumps(gather(), indent=2)
