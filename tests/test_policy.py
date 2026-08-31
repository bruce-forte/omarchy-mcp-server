"""Policy is the security boundary, so it is tested against every route Omarchy
ships rather than against a handful of examples."""

from __future__ import annotations

import pytest

from omarchy_mcp.config import Config
from omarchy_mcp.policy import GUARDED_GROUPS, GUARDED_ROUTES, Tier, base_tier, decide


def test_every_sudo_command_is_blocked(commands):
    sudo = [c for c in commands.values() if c.requires_sudo]
    assert sudo, "fixture should contain sudo commands"
    assert all(base_tier(c) is Tier.BLOCKED for c in sudo)


def test_blocked_cannot_be_promoted_by_config(commands):
    """A sudo command stays refused however generous the configuration is."""
    sudo = next(c for c in commands.values() if c.requires_sudo)
    permissive = Config(
        allow=(sudo.route,),
        allow_groups=tuple(sorted({c.group for c in commands.values()})),
    )
    verdict = decide(sudo, permissive)
    assert verdict.tier is Tier.BLOCKED
    assert verdict.allowed is False
    assert "sudo" in verdict.reason


def test_blocked_refusal_explains_why_not_just_that(commands):
    sudo = next(c for c in commands.values() if c.requires_sudo)
    reason = decide(sudo, Config()).reason
    assert "terminal" in reason  # names the actual cause, not a policy opinion


def test_guarded_groups_are_refused_by_default(commands):
    guarded = [c for c in commands.values() if not c.requires_sudo and c.group in GUARDED_GROUPS]
    assert guarded
    assert all(decide(c, Config()).allowed is False for c in guarded)


def test_guarded_route_allowed_when_listed(commands):
    route = "omarchy system reboot"
    cmd = commands[route]
    assert decide(cmd, Config()).allowed is False
    assert decide(cmd, Config(allow=(route,))).allowed is True


def test_guarded_group_allowed_when_listed(commands):
    cmd = next(c for c in commands.values() if not c.requires_sudo and c.group == "install")
    assert decide(cmd, Config()).allowed is False
    assert decide(cmd, Config(allow_groups=("install",))).allowed is True


def test_deny_demotes_a_safe_route(commands):
    cmd = commands["omarchy theme current"]
    assert decide(cmd, Config()).allowed is True
    verdict = decide(cmd, Config(deny=("omarchy theme current",)))
    assert verdict.allowed is False
    assert verdict.tier is Tier.GUARDED


def test_refusals_name_the_config_file(commands):
    cmd = next(c for c in commands.values() if base_tier(c) is Tier.GUARDED)
    assert "config.toml" in decide(cmd, Config()).reason


def test_ordinary_read_only_commands_are_safe(commands):
    for route in ("omarchy theme list", "omarchy battery status", "omarchy network status"):
        assert decide(commands[route], Config()).allowed is True


@pytest.mark.parametrize("route", sorted(GUARDED_ROUTES))
def test_named_guarded_routes_exist_and_are_guarded(route, commands):
    """Guards against a typo silently guarding nothing, and against Omarchy
    renaming a route out from under us."""
    cmd = commands.get(route)
    assert cmd is not None, f"{route} is no longer an Omarchy route"
    assert base_tier(cmd) in (Tier.GUARDED, Tier.BLOCKED)


def test_guarded_groups_still_exist(commands):
    live = {c.group for c in commands.values()}
    stale = GUARDED_GROUPS - live
    assert not stale, f"these guarded groups no longer exist in Omarchy: {sorted(stale)}"


def test_every_command_classifies(commands):
    """No route falls through the classifier."""
    for cmd in commands.values():
        assert isinstance(decide(cmd, Config()).tier, Tier)
