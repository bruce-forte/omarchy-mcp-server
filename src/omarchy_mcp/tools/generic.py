"""The four tools that make every other Omarchy capability reachable.

Omarchy ships hundreds of commands and the shell exposes a couple of dozen IPC
targets. One MCP tool per command would put tens of thousands of tokens of
schema into every client's context before it did any work, so discovery and
dispatch are separated instead: search to find out what exists, run to do it.

Everything the curated tools do can also be done here. The curated tools exist
where a round trip through search would be wasteful, or where the result is not
text.

How a tool module is shaped, since all five follow the same pattern: one
``register`` function holding the tool bodies as nested functions, each carrying
two decorators. ``@tools.tool(...)`` declares it in the catalogue -- the
``description`` there is what the model reads, which is why it is written as
prose. ``@threaded`` (from `_shared.py`) is for the sync bodies and moves them
onto a worker thread; a body that needs to ``await`` is written ``async def``
and skips it. The parameters of the function *are* the tool's schema: the SDK
reads their names, type hints and defaults, so renaming one changes the API.
"""

from __future__ import annotations

import json

from mcp.server.mcpserver import Context
from mcp.types import ToolAnnotations

from .. import execute, gate, registry, shell
from ..config import Config
from ..permissions import describe
from ..policy import shell_call_refusal
from ..settings import Settings
from ..stats import Stats
from ._shared import UNTRUSTED, offload, threaded
from .catalogue import Catalogue


def register(tools: Catalogue, settings: Settings, log, stats: Stats | None = None) -> None:
    """Declare the four generic tools in ``tools``."""
    stats = stats or Stats()

    @tools.tool(
        name="omarchy_search_commands",
        title="Search Omarchy commands",
        description=(
            "Search Omarchy's command registry. Returns matching commands with their "
            "arguments, summary, examples, and whether they can be run by this server. "
            "Use this to discover what Omarchy can do before calling omarchy_run. "
            "An empty query lists commands from the start of the registry."
        ),
        annotations=ToolAnnotations(
            read_only_hint=True, destructive_hint=False, idempotent_hint=True, open_world_hint=False
        ),
    )
    @threaded
    def omarchy_search_commands(
        query: str = "",
        limit: int = 20,
        include_hidden: bool = False,
    ) -> str:
        """Find commands matching ``query``, with this server's verdict on each."""
        with stats.call("omarchy_search_commands") as rec:
            # One snapshot per call: what this reports about a route -- its tier,
            # whether it is runnable -- is a claim about the config in force now.
            perms = settings.permissions
            unreviewed = settings.unreviewed
            rec.args = (query,) if query else ()
            # Clamped rather than validated: the caller is a model, and a limit
            # of 0 or 10000 is a slip worth correcting rather than refusing.
            limit = max(1, min(limit, 100))
            hits = registry.search(query, limit=limit, include_hidden=include_hidden)
            rows = []
            for cmd in hits:
                # One derivation, shared with the commands resource.
                rows.append(
                    registry.as_dict(cmd) | describe(cmd, perms, unreviewed=unreviewed)
                )
            return json.dumps({"query": query, "count": len(rows), "commands": rows}, indent=2)

    @tools.tool(
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
            read_only_hint=False, destructive_hint=True, idempotent_hint=False, open_world_hint=True
        ),
    )
    async def omarchy_run(
        route: str,
        args: list[str] | None = None,
        timeout_ms: int | None = None,
        detach: bool | None = None,
        # ``ctx`` is supplied by the SDK, not by the model: it is recognised by
        # its type hint and does not appear in the tool's schema. It is how the
        # gate reaches the client session to ask a question.
        ctx: Context | None = None,
    ) -> str:
        """Run one registry route, after the gate has decided it may."""
        # ``args or []`` covers both "not given" and "given as null"; ``list``
        # then copies it, so nothing here mutates the caller's list.
        args = list(args or [])
        with stats.call("omarchy_run") as rec:
            # The whole call -- gate, resolve, execute -- runs under one config.
            # A reload landing halfway through cannot authorize by one set of
            # rules and execute under another.
            config = settings.current
            rec.route = route.strip()
            rec.args = tuple(args)
            cmd = registry.get(route.strip())
            if cmd is None:
                rec.outcome = "error"
                close = registry.suggest(route)
                return json.dumps(
                    {
                        "error": f"no such route: {route!r}",
                        "did_you_mean": [c.route for c in close],
                    },
                    indent=2,
                )

            # The same gate the curated tools pass through. Reaching a command by
            # its route must not skip a check that reaching it by a tool applies --
            # including the one that asks the user.
            decision = await gate.authorize(
                cmd,
                args,
                perms=settings.permissions,
                unreviewed=settings.unreviewed,
                ctx=ctx,
                log=log,
                offload=offload,
            )
            # `gate.authorize` returns one of two types rather than raising, so
            # the refusal has to be checked for explicitly.
            if isinstance(decision, gate.Refused):
                rec.outcome = "refused"
                rec.tier = decision.tier
                rec.consent = decision.outcome
                log.info("run route=%r refused", cmd.route)
                return json.dumps(decision.as_dict(), indent=2)

            call = decision.call
            rec.tier = decision.outcome.tier.value
            rec.consent = decision.consent
            # The *resolved* arguments: what actually ran, not what was asked for.
            rec.args = tuple(call.args)
            if call.target is not None:
                rec.target = call.target.label

            # ``None`` means "decide for me"; ``False`` from the model means
            # "wait for it", which is why this is not a plain truth test.
            if detach is None:
                detach = execute.should_detach(cmd.group, cmd.route)

            argv = [*cmd.argv_prefix, *call.args]
            try:
                result = await offload(
                    execute.run,
                    argv,
                    timeout_ms=timeout_ms or config.timeout_ms,
                    max_output_b=config.max_output_b,
                    detach=detach,
                )
            except execute.NotInstalled as exc:
                rec.outcome = "not_installed"
                log.warning("run route=%r %s", cmd.route, exc)
                return json.dumps(exc.as_dict(), indent=2)

            rec.exit = result.exit_code
            rec.detached = result.detached
            rec.timed_out = result.timed_out
            log.info(
                "run route=%r target=%r exec=%s exit=%s detached=%s timed_out=%s",
                cmd.route,
                call.target.label if call.target else None,
                result.executable,
                result.exit_code,
                result.detached,
                result.timed_out,
            )
            # ``**result.as_dict()`` spreads the result's fields into the same
            # object as the command line, so the agent gets one flat answer.
            payload: dict[str, object] = {"command": execute.quote(argv), **result.as_dict()}
            if call.target is not None:
                payload["target"] = call.target.label
            return json.dumps(payload, indent=2)

    @tools.tool(
        name="omarchy_shell_targets",
        title="List omarchy-shell IPC targets",
        description=(
            "List the IPC targets the running omarchy-shell exposes, with the exact "
            "signature of every method. These reach the live shell -- the bar, the OSD, "
            "notifications, media, and every loaded plugin -- which the command registry "
            "does not cover. Pass `target` to get just one."
        ),
        annotations=ToolAnnotations(
            read_only_hint=True, destructive_hint=False, idempotent_hint=True, open_world_hint=False
        ),
    )
    @threaded
    def omarchy_shell_targets(target: str = "", refresh: bool = False) -> str:
        """Every IPC target the shell offers, or just the one named."""
        with stats.call("omarchy_shell_targets") as rec:
            rec.args = (target,) if target else ()
            try:
                found = shell.targets(refresh=refresh)
            except shell.ShellError as exc:
                return json.dumps({"error": str(exc)}, indent=2)

            if target:
                one = found.get(target)
                if one is None:
                    return json.dumps(
                        {"error": f"no such target: {target!r}", "targets": sorted(found)},
                        indent=2,
                    )
                return json.dumps(shell.as_dict(one), indent=2)

            return json.dumps(
                {"count": len(found), "targets": [shell.as_dict(t) for t in found.values()]},
                indent=2,
            )

    @tools.tool(
        name="omarchy_shell_call",
        title="Call an omarchy-shell IPC method",
        description=(
            "Call a method on a running omarchy-shell IPC target, as listed by "
            "omarchy_shell_targets. Arguments are strings; a method taking JSON expects "
            "it as a single string argument. Returns whatever the method returns. "
            "This server's own target answers `status` and `recent` only: it supervises "
            "this connection and the record of what it did, so an agent cannot stop it."
        ),
        annotations=ToolAnnotations(
            read_only_hint=False, destructive_hint=False, idempotent_hint=False,
            open_world_hint=True,
        ),
    )
    @threaded
    def omarchy_shell_call(
        target: str,
        method: str,
        args: list[str] | None = None,
        timeout_ms: int | None = None,
    ) -> str:
        """Call one method on one IPC target, having checked it exists."""
        args = list(args or [])
        with stats.call("omarchy_shell_call") as rec:
            config = settings.current
            rec.route = f"{target}.{method}"
            rec.args = tuple(args)
            try:
                known = shell.targets()
            except shell.ShellError as exc:
                return json.dumps({"error": str(exc)}, indent=2)

            found = known.get(target)
            if found is None:
                return json.dumps(
                    {"error": f"no such target: {target!r}", "targets": sorted(known)}, indent=2
                )
            # A set comprehension of the method names, so this is a membership
            # test rather than a scan.
            if method not in {m.name for m in found.methods}:
                return json.dumps(
                    {
                        "error": f"target {target!r} has no method {method!r}",
                        "methods": [m.signature for m in found.methods],
                    },
                    indent=2,
                )

            # `omarchy_run` reaches the same call through the `omarchy shell`
            # route and is stopped by `gate.authorize`. This path does not go
            # through the gate, so it asks the same question itself rather than
            # relying on the other door being the only one.
            refusal = shell_call_refusal(target, method)
            if refusal is not None:
                rec.outcome = "refused"
                rec.tier = "blocked"
                log.info("self-call refused target=%r method=%r", target, method)
                return json.dumps({"error": refusal, "tier": "blocked"}, indent=2)

            argv = shell.call_argv(target, method, args)
            result = execute.run(
                argv, timeout_ms=timeout_ms or config.timeout_ms, max_output_b=config.max_output_b
            )
            log.info("shell_call %s.%s exit=%s", target, method, result.exit_code)
            return json.dumps({"command": execute.quote(argv), **result.as_dict()}, indent=2)

