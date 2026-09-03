"""What kind of thing a command is.

Three tiers, derived from the registry rather than hand-written, so the
classification does not rot when Omarchy adds commands. Whether a guarded
command actually runs is `permissions.py`'s question; this module answers only
what sort of command it is, plus the one refusal that is about a call's
arguments rather than its route -- see `self_refusal`.

``BLOCKED``
    Refused unconditionally, and not promotable from configuration. Two things
    land here. A command needing sudo, because it cannot work: the daemon has no
    controlling tty, so ``sudo`` would hang on a password prompt nobody can see.
    And a call that would stop, remove or reconfigure this server's own
    supervision -- see `self_refusal`, which is about what the call *names*
    rather than what the route is, and so cannot be a tier.

``GUARDED``
    Destructive but perfectly runnable. What happens to it is decided by the
    permissions document: asked about by default, denied or allowed by a rule.

``SAFE``
    Everything else. Runs, unless a `deny` rule says otherwise.

This module is the security boundary. It is pure, takes the registry as an
argument, and is tested against every route Omarchy ships.
"""

from __future__ import annotations

from collections.abc import Sequence
from enum import Enum

from .paths import PLUGIN_ID
from .registry import Command

#: Groups whose commands install, remove, or rewrite the system.
GUARDED_GROUPS = frozenset(
    {
        "install",
        "remove",
        "reinstall",
        "migrate",
        "setup",
        "pkg",
        "upgrade",
        "update",
        "dev",
        "drive",
        "hibernation",
        "snapshot",
        "provision",
        "apply",
        "channel",
        # `plugin add <git-url> --enable --yes` clones a repository into the
        # shell and loads it: a package install by another name, and the shell
        # runs it in its own process. `disable` and `remove` are how an agent
        # would silence this plugin without ever touching the daemon.
        "plugin",
    }
)

#: Individually destructive routes in otherwise safe groups.
GUARDED_ROUTES = frozenset(
    {
        "omarchy system shutdown",
        "omarchy system reboot",
        "omarchy system logout",
        "omarchy theme remove",
        "omarchy hyprland window close all",
        "omarchy toggle hybrid gpu",
        "omarchy windows vm",
        # Kills omarchy-shell, and this daemon is its child. Every attached MCP
        # session drops and the activity log loses the session it was in the
        # middle of. Guarded rather than blocked: restarting the shell is a
        # thing a user legitimately asks for.
        "omarchy restart shell",
    }
)


class Tier(str, Enum):
    SAFE = "safe"
    GUARDED = "guarded"
    BLOCKED = "blocked"


#: The IPC verbs on this plugin's own target that an agent may call.
#:
#: Both are read-only and both answer a question an agent has a good reason to
#: ask -- "am I still connected", "what have I done". Everything else on that
#: target either changes the daemon's lifecycle or moves the bearer token onto
#: a surface (`copyClientConfig` writes it to the clipboard, which
#: `omarchy_clipboard_read` can then read back).
SELF_READ_VERBS = frozenset({"status", "recent"})

#: Routes that act on an installed plugin named by id in their arguments.
_PLUGIN_ID_ROUTES = frozenset(
    {
        "omarchy plugin disable",
        "omarchy plugin enable",
        "omarchy plugin remove",
        "omarchy plugin update",
        "omarchy plugin clone",
    }
)

#: The route that is `omarchy_shell_call` by another name.
_SHELL_ROUTE = "omarchy shell"

_PANEL_HINT = (
    "The MCP server panel in the bar has Start, Stop, Restart, Reload config "
    "and Copy client config. Ask the user to press the one they want."
)


def shell_call_refusal(target: str, method: str) -> str | None:
    """Why an IPC call on this plugin's own target is refused, or ``None``.

    The audit trail is the point. An agent that can call `stop` does not have to
    defeat anything to erase the record of what it did -- it asks the supervisor
    politely, through a tool this project ships. So the mutating verbs are
    refused outright rather than guarded: every one of them has a button in the
    panel, which is a surface the person is at, and no legitimate use is lost by
    routing the agent through them.
    """
    if target != PLUGIN_ID:
        return None
    if method in SELF_READ_VERBS:
        return None
    return (
        f"`{target}` is this MCP server's own supervisor, and {method or '<none>'!r} "
        f"would stop, restart or reconfigure it -- including the record of what "
        f"agents have done. Refused, and no configuration allows it. "
        f"{_PANEL_HINT} "
        f"Readable from here: {', '.join(sorted(SELF_READ_VERBS))}."
    )


def self_refusal(route: str, args: Sequence[str]) -> str | None:
    """Why running ``route`` with ``args`` would disable this server, or ``None``.

    Takes arguments, which is why it is not part of `decide` and not a tier: the
    same route is fine or refused depending on what it names. Three doors reach
    the same room, and closing one of them is closing none:

    - ``omarchy_shell_call`` on this plugin's target
    - ``omarchy shell <target> <method>``, the registry route that *is* that
      call, reachable through ``omarchy_run``
    - ``omarchy plugin disable|remove|...`` naming this plugin's id

    The `plugin` group is guarded as well, so an agent naming somebody else's
    plugin still has to be allowed or approved. This is the narrower rule on top:
    naming *ours* is refused however the guarded tier is configured.
    """
    args = list(args)

    if route == _SHELL_ROUTE:
        # `omarchy shell [-q] <target> <method> [args...]`. Flags can precede
        # either, so the first two non-flag arguments are the ones that matter.
        positional = [a for a in args if not a.startswith("-")]
        if not positional:
            return None
        return shell_call_refusal(positional[0], positional[1] if len(positional) > 1 else "")

    if route in _PLUGIN_ID_ROUTES and PLUGIN_ID in args:
        return (
            f"`{route}` names this MCP server's own plugin ({PLUGIN_ID}). Refused: "
            f"an agent must not be able to disable, remove or replace the thing "
            f"that records what it did. The user can do it themselves in a "
            f"terminal. {_PANEL_HINT}"
        )

    return None


def base_tier(cmd: Command) -> Tier:
    """The tier before any user configuration is applied."""
    if cmd.requires_sudo:
        return Tier.BLOCKED
    if cmd.group in GUARDED_GROUPS or cmd.route in GUARDED_ROUTES:
        return Tier.GUARDED
    return Tier.SAFE
