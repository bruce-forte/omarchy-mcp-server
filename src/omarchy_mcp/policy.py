"""What an agent is allowed to run.

Three tiers, derived from the registry rather than hand-written, so the policy
does not rot when Omarchy adds commands:

``BLOCKED``
    Needs sudo. Refused unconditionally -- not as a judgement call, but because
    it cannot work: the daemon has no controlling tty, so ``sudo`` would hang on
    a password prompt nobody can see. Not promotable from configuration.

``GUARDED``
    Destructive but perfectly runnable. Refused unless the user has opted in
    per-route or per-group.

``SAFE``
    Everything else.

This module is the security boundary. It is pure, takes the registry as an
argument, and is tested against every route Omarchy ships.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum

from .config import Config
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
    }
)


class Tier(str, Enum):
    SAFE = "safe"
    GUARDED = "guarded"
    BLOCKED = "blocked"


@dataclass(frozen=True)
class Verdict:
    tier: Tier
    allowed: bool
    reason: str = ""
    #: Whether this refusal is one a person may overturn at call time.
    #:
    #: Not derivable from ``tier`` and ``allowed`` by the caller, which is the
    #: reason it is stated here. A ``policy.deny`` demotion also refuses at
    #: ``GUARDED``, and re-asking it would turn a decision the user already took
    #: into a question. ``BLOCKED`` is never askable: no answer makes a sudo
    #: command runnable -- decision 4.
    askable: bool = False


def base_tier(cmd: Command) -> Tier:
    """The tier before any user configuration is applied."""
    if cmd.requires_sudo:
        return Tier.BLOCKED
    if cmd.group in GUARDED_GROUPS or cmd.route in GUARDED_ROUTES:
        return Tier.GUARDED
    return Tier.SAFE


def decide(cmd: Command, config: Config) -> Verdict:
    """Whether ``cmd`` may run, and why not when it may not."""
    tier = base_tier(cmd)

    if tier is Tier.BLOCKED:
        return Verdict(
            tier,
            False,
            f"`{cmd.route}` requires sudo. The MCP server runs without a "
            f"controlling terminal, so a password prompt could never be "
            f"answered. Run it yourself in a terminal.",
        )

    # A demotion applies to commands that would otherwise be safe.
    if tier is Tier.SAFE and cmd.route in config.deny:
        return Verdict(
            Tier.GUARDED,
            False,
            f"`{cmd.route}` is listed in policy.deny in "
            f"~/.config/omarchy/mcp/config.toml.",
        )

    if tier is Tier.GUARDED:
        if cmd.route in config.allow or cmd.group in config.allow_groups:
            return Verdict(Tier.GUARDED, True, "")
        return Verdict(
            Tier.GUARDED,
            False,
            f"`{cmd.route}` is guarded because it can change the system in ways "
            f"that are hard to undo. To allow it, add it to policy.allow (or its "
            f'group "{cmd.group}" to policy.allow_groups) in '
            f"~/.config/omarchy/mcp/config.toml. To be asked at the time instead, "
            f"set policy.ask = true there.",
            askable=True,
        )

    return Verdict(Tier.SAFE, True, "")
