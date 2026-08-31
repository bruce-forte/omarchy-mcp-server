"""The four tools that make every other Omarchy capability reachable.

Omarchy ships hundreds of commands and the shell exposes a couple of dozen IPC
targets. One MCP tool per command would put tens of thousands of tokens of
schema into every client's context before it did any work, so discovery and
dispatch are separated instead: search to find out what exists, run to do it.

Everything the curated tools do can also be done here. The curated tools exist
where a round trip through search would be wasteful, or where the result is not
text.
"""

from __future__ import annotations

import json

from mcp.types import ToolAnnotations

from .. import execute, registry, resolve, shell
from ..config import Config
from ..policy import decide
from ..stats import Stats
from ._shared import UNTRUSTED, offload, threaded


def register(mcp, config: Config, log, stats: Stats | None = None) -> None:
    stats = stats or Stats()
    @mcp.tool(
        name="omarchy_search_commands",
        title="Search Omarchy commands",
        description=(
            "Search Omarchy's command registry. Returns matching commands with their "
            "arguments, summary, examples, and whether they can be run by this server. "
            "Use this to discover what Omarchy can do before calling omarchy_run. "
            "An empty query lists commands from the start of the registry."
        ),
        annotations=ToolAnnotations(
            readOnlyHint=True, destructiveHint=False, idempotentHint=True, openWorldHint=False
        ),
    )
    @threaded
    def omarchy_search_commands(
        query: str = "",
        limit: int = 20,
        include_hidden: bool = False,
    ) -> str:
        stats.record("omarchy_search_commands")
        limit = max(1, min(limit, 100))
        hits = registry.search(query, limit=limit, include_hidden=include_hidden)
        rows = []
        for cmd in hits:
            verdict = decide(cmd, config)
            row = registry.as_dict(cmd)
            row["tier"] = verdict.tier.value
            row["runnable"] = verdict.allowed
            if not verdict.allowed:
                row["refusal"] = verdict.reason
            rows.append(row)
        return json.dumps({"query": query, "count": len(rows), "commands": rows}, indent=2)

    @mcp.tool(
        name="omarchy_run",
        title="Run an Omarchy command",
        description=(
            "Run a command from Omarchy's registry. `route` must be a full route as "
            "returned by omarchy_search_commands, for example 'omarchy theme set'. "
            "`args` is a list of arguments; they are passed directly to the program and "
            "are never interpreted by a shell. Commands that need sudo cannot be run. "
            "Commands that open a window or wait for the user are detached automatically "
            "and return immediately." + UNTRUSTED
        ),
        annotations=ToolAnnotations(
            readOnlyHint=False, destructiveHint=True, idempotentHint=False, openWorldHint=True
        ),
    )
    async def omarchy_run(
        route: str,
        args: list[str] | None = None,
        timeout_ms: int | None = None,
        detach: bool | None = None,
    ) -> str:
        args = list(args or [])
        cmd = registry.get(route.strip())
        stats.record("omarchy_run", route=route.strip(), ok=cmd is not None)
        if cmd is None:
            close = registry.suggest(route)
            return json.dumps(
                {
                    "error": f"no such route: {route!r}",
                    "did_you_mean": [c.route for c in close],
                },
                indent=2,
            )

        verdict = decide(cmd, config)
        if not verdict.allowed:
            return json.dumps({"error": verdict.reason, "tier": verdict.tier.value}, indent=2)

        # The same gate the curated tools pass through. Reaching a command by
        # its route must not skip a check that reaching it by a tool applies.
        try:
            call = await offload(resolve.resolve_call, cmd.route, args)
        except resolve.Unresolvable as exc:
            log.info("run route=%r unresolved=%s", cmd.route, exc.kind)
            return json.dumps(exc.as_dict(), indent=2)

        if detach is None:
            detach = execute.should_detach(cmd.group, cmd.route)

        argv = [*cmd.argv_prefix, *call.args]
        result = await offload(
            execute.run,
            argv,
            timeout_ms=timeout_ms or config.timeout_ms,
            max_output_b=config.max_output_b,
            detach=detach,
        )
        log.info(
            "run route=%r target=%r exit=%s detached=%s timed_out=%s",
            cmd.route,
            call.target.label if call.target else None,
            result.exit_code,
            result.detached,
            result.timed_out,
        )
        payload: dict[str, object] = {"command": execute.quote(argv), **result.as_dict()}
        if call.target is not None:
            payload["target"] = call.target.label
        return json.dumps(payload, indent=2)

    @mcp.tool(
        name="omarchy_shell_targets",
        title="List omarchy-shell IPC targets",
        description=(
            "List the IPC targets the running omarchy-shell exposes, with the exact "
            "signature of every method. These reach the live shell -- the bar, the OSD, "
            "notifications, media, and every loaded plugin -- which the command registry "
            "does not cover. Pass `target` to get just one."
        ),
        annotations=ToolAnnotations(
            readOnlyHint=True, destructiveHint=False, idempotentHint=True, openWorldHint=False
        ),
    )
    @threaded
    def omarchy_shell_targets(target: str = "", refresh: bool = False) -> str:
        stats.record("omarchy_shell_targets")
        try:
            found = shell.targets(refresh=refresh)
        except shell.ShellError as exc:
            return json.dumps({"error": str(exc)}, indent=2)

        if target:
            one = found.get(target)
            if one is None:
                return json.dumps(
                    {"error": f"no such target: {target!r}", "targets": sorted(found)}, indent=2
                )
            return json.dumps(shell.as_dict(one), indent=2)

        return json.dumps(
            {"count": len(found), "targets": [shell.as_dict(t) for t in found.values()]}, indent=2
        )

    @mcp.tool(
        name="omarchy_shell_call",
        title="Call an omarchy-shell IPC method",
        description=(
            "Call a method on a running omarchy-shell IPC target, as listed by "
            "omarchy_shell_targets. Arguments are strings; a method taking JSON expects "
            "it as a single string argument. Returns whatever the method returns."
        ),
        annotations=ToolAnnotations(
            readOnlyHint=False, destructiveHint=False, idempotentHint=False, openWorldHint=True
        ),
    )
    @threaded
    def omarchy_shell_call(
        target: str,
        method: str,
        args: list[str] | None = None,
        timeout_ms: int | None = None,
    ) -> str:
        args = list(args or [])
        stats.record("omarchy_shell_call", route=f"{target}.{method}")
        try:
            known = shell.targets()
        except shell.ShellError as exc:
            return json.dumps({"error": str(exc)}, indent=2)

        found = known.get(target)
        if found is None:
            return json.dumps(
                {"error": f"no such target: {target!r}", "targets": sorted(known)}, indent=2
            )
        if method not in {m.name for m in found.methods}:
            return json.dumps(
                {
                    "error": f"target {target!r} has no method {method!r}",
                    "methods": [m.signature for m in found.methods],
                },
                indent=2,
            )

        argv = shell.call_argv(target, method, args)
        result = execute.run(
            argv, timeout_ms=timeout_ms or config.timeout_ms, max_output_b=config.max_output_b
        )
        log.info("shell_call %s.%s exit=%s", target, method, result.exit_code)
        return json.dumps({"command": execute.quote(argv), **result.as_dict()}, indent=2)
