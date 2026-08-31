"""Resources: the reference material, addressable by URI.

Tools are how an agent acts. Resources are how a person reads. In Claude Code
they surface as `@` mentions the user types, so these are chosen for what
someone building an Omarchy plugin would actually want to pull into a
conversation -- above all `omarchy://shell/targets`, which is the shell's IPC
surface with full signatures and is documented nowhere upstream.

Four are concrete and appear in the `@` menu. Three are URI templates, which
between them cover every command and every IPC target without putting several
hundred entries in a listing. Templates resolve when read by URI; note that
Claude Code does not enumerate them, so the concrete four are the discoverable
set. See ROADMAP.md finding F8.
"""

from __future__ import annotations

import json

from . import desktop, gate, registry, shell
from .config import Config
from .policy import decide
from .status import gather


def register(mcp, config: Config, log) -> None:
    # ------------------------------------------------------------- concrete

    @mcp.resource(
        "omarchy://commands",
        name="Omarchy command registry",
        description=(
            "Every Omarchy command with its route, arguments, summary, examples, "
            "and whether the MCP server may run it."
        ),
        mime_type="application/json",
    )
    def commands_resource() -> str:
        return json.dumps(_annotated(registry.all_commands().values()), indent=2)

    @mcp.resource(
        "omarchy://shell/targets",
        name="omarchy-shell IPC targets",
        description=(
            "Every IPC target the running shell exposes, with the exact signature of "
            "each method. This is the interface plugins are driven through, and it is "
            "documented nowhere else."
        ),
        mime_type="application/json",
    )
    def targets_resource() -> str:
        try:
            found = shell.targets()
        except shell.ShellError as exc:
            return json.dumps({"error": str(exc)}, indent=2)
        return json.dumps(
            {"count": len(found), "targets": [shell.as_dict(t) for t in found.values()]},
            indent=2,
        )

    @mcp.resource(
        "omarchy://desktop/state",
        name="Desktop state",
        description="Monitors, workspaces, open windows, and which window is focused.",
        mime_type="application/json",
    )
    def desktop_resource() -> str:
        try:
            return json.dumps(desktop.state(), indent=2)
        except desktop.DesktopError as exc:
            return json.dumps({"error": str(exc)}, indent=2)

    @mcp.resource(
        "omarchy://system/status",
        name="System status",
        description="CPU, memory, battery, network, theme, background, idle, font, monitor.",
        mime_type="application/json",
    )
    def status_resource() -> str:
        return json.dumps(gather(), indent=2)

    # ------------------------------------------------------------- templates

    @mcp.resource(
        "omarchy://command/{route}",
        name="One Omarchy command",
        description=(
            "A single command's registry entry. The route may be given with or without "
            "the leading 'omarchy', for example 'theme set' or 'omarchy theme set'."
        ),
        mime_type="application/json",
    )
    def command_resource(route: str) -> str:
        wanted = route.strip()
        if not wanted.startswith("omarchy"):
            wanted = f"omarchy {wanted}"

        cmd = registry.get(wanted)
        if cmd is None:
            return json.dumps(
                {
                    "error": f"no such command: {route!r}",
                    "did_you_mean": [c.route for c in registry.suggest(wanted)],
                },
                indent=2,
            )
        return json.dumps(_annotated([cmd])["commands"][0], indent=2)

    @mcp.resource(
        "omarchy://commands/{group}",
        name="One Omarchy command group",
        description=(
            "Every command in a group, such as 'theme', 'audio', or 'capture'. "
            "Group names appear on each command in omarchy://commands."
        ),
        mime_type="application/json",
    )
    def group_resource(group: str) -> str:
        grouped = registry.groups()
        found = grouped.get(group.strip())
        if found is None:
            return json.dumps(
                {"error": f"no such group: {group!r}", "groups": sorted(grouped)}, indent=2
            )
        return json.dumps(_annotated(found), indent=2)

    @mcp.resource(
        "omarchy://shell/target/{name}",
        name="One omarchy-shell IPC target",
        description="A single IPC target's methods and signatures, such as 'media' or 'shell'.",
        mime_type="application/json",
    )
    def target_resource(name: str) -> str:
        try:
            found = shell.targets()
        except shell.ShellError as exc:
            return json.dumps({"error": str(exc)}, indent=2)

        one = found.get(name.strip())
        if one is None:
            return json.dumps(
                {"error": f"no such target: {name!r}", "targets": sorted(found)}, indent=2
            )
        return json.dumps(shell.as_dict(one), indent=2)

    def _annotated(commands) -> dict[str, object]:
        """Registry entries carrying this server's verdict on each one.

        Reading the raw registry would leave the reader to work out which
        commands an agent can actually run; that is the interesting half.
        """
        rows = []
        for cmd in sorted(commands, key=lambda c: c.route):
            verdict = decide(cmd, config)
            row = registry.as_dict(cmd)
            row["tier"] = verdict.tier.value
            # A route that will ask is runnable: reporting it as refused would
            # make a careful agent never call it, so the prompt would never
            # fire and the feature would be invisible to the only caller.
            asks = gate.asks(verdict, config)
            row["runnable"] = verdict.allowed or asks
            if asks:
                row["asks"] = True
                row["note"] = (
                    "This call pauses while the user is asked to approve it, and is "
                    "refused if they decline or do not answer."
                )
            rows.append(row)
        return {"count": len(rows), "commands": rows}
