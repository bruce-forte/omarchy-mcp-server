"""One call for the questions that otherwise take eight."""

from __future__ import annotations

import json
import logging

from mcp.types import ToolAnnotations

from ..settings import Settings
from ..stats import Stats
from ..status import gather
from ._shared import threaded
from .catalogue import Catalogue

# Re-exported for tests and for the resource, which answer the same question.
__all__ = ["register"]


def register(
    tools: Catalogue, settings: Settings, log: logging.Logger, stats: Stats
) -> None:
    """Declare the one system tool in ``tools``."""

    @tools.tool(
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
            read_only_hint=True, destructive_hint=False, idempotent_hint=True, open_world_hint=False
        ),
    )
    @threaded
    def omarchy_system_status() -> str:
        """Every cheap read-only probe at once. See `status.gather`."""
        # ``with`` without ``as``: this tool learns nothing about itself worth
        # recording, so the record is opened for its timing and its count alone.
        with stats.call("omarchy_system_status"):
            return json.dumps(gather(), indent=2)
