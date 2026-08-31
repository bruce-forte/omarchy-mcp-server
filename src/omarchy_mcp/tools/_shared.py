"""Shared plumbing for tools that are a thin shape over an Omarchy command.

Every curated tool still goes through the same policy check and the same
executor as `omarchy_run`. A curated tool is a better-shaped door onto the same
room, never a way around the lock.
"""

from __future__ import annotations

import json

from .. import execute, registry
from ..config import Config
from ..policy import decide
from ..stats import Stats


def run_route(
    route: str,
    args: list[str],
    *,
    config: Config,
    stats: Stats,
    log,
    tool: str,
    detach: bool | None = None,
    timeout_ms: int | None = None,
) -> str:
    """Run an Omarchy route on behalf of a curated tool."""
    cmd = registry.get(route)
    if cmd is None:
        stats.record(tool, route=route, ok=False)
        return json.dumps(
            {
                "error": f"`{route}` is not a command on this Omarchy. "
                f"It may have been renamed; try omarchy_search_commands.",
            },
            indent=2,
        )

    verdict = decide(cmd, config)
    if not verdict.allowed:
        stats.record(tool, route=route, ok=False)
        return json.dumps({"error": verdict.reason, "tier": verdict.tier.value}, indent=2)

    if detach is None:
        detach = execute.should_detach(cmd.group, cmd.route)

    argv = [*cmd.argv_prefix, *args]
    result = execute.run(
        argv,
        timeout_ms=timeout_ms or config.timeout_ms,
        max_output_b=config.max_output_b,
        detach=detach,
    )
    stats.record(tool, route=route, ok=result.exit_code in (0, None))
    log.info("%s route=%r exit=%s", tool, route, result.exit_code)

    payload: dict[str, object] = {"command": execute.quote(argv), **result.as_dict()}
    if result.exit_code not in (0, None):
        payload["hint"] = "Run omarchy_search_commands for this route's accepted arguments."
    return json.dumps(payload, indent=2)


def enabled(config: Config, name: str) -> bool:
    return name not in set(config.disabled_tools)
