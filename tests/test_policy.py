"""Policy is the security boundary, so it is tested against every route Omarchy
ships rather than against a handful of examples."""

from __future__ import annotations

import pytest

from omarchy_mcp.config import Config
from omarchy_mcp.paths import PLUGIN_ID
from omarchy_mcp.policy import (
    GUARDED_GROUPS,
    GUARDED_ROUTES,
    SELF_READ_VERBS,
    Tier,
    base_tier,
    decide,
    self_refusal,
    shell_call_refusal,
)


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


# --- N12: an agent must not be able to switch off its own supervisor ---------


def test_plugin_group_is_guarded(commands):
    """`plugin add <git-url> --enable --yes` clones a repository into the shell
    and loads it. That is a package install, and it was `safe`."""
    plugin = [c for c in commands.values() if c.group == "plugin"]
    assert plugin
    assert all(base_tier(c) is Tier.GUARDED for c in plugin if not c.requires_sudo)


def test_restart_shell_is_guarded(commands):
    """The daemon is a child of omarchy-shell; restarting it takes the daemon
    down with every attached session."""
    assert base_tier(commands["omarchy restart shell"]) is Tier.GUARDED


@pytest.mark.parametrize("method", ["stop", "start", "restart", "rebuild", "reloadConfig"])
def test_own_ipc_target_refuses_mutating_verbs(method):
    assert shell_call_refusal(PLUGIN_ID, method) is not None


@pytest.mark.parametrize("method", ["clientConfig", "copyClientConfig"])
def test_own_ipc_target_refuses_the_token_verbs(method):
    """`copyClientConfig` puts the bearer token on the clipboard, which
    `omarchy_clipboard_read` reads back."""
    assert shell_call_refusal(PLUGIN_ID, method) is not None


@pytest.mark.parametrize("method", sorted(SELF_READ_VERBS))
def test_own_ipc_target_still_answers_the_read_verbs(method):
    assert shell_call_refusal(PLUGIN_ID, method) is None


def test_other_targets_are_untouched():
    for target in ("omarchy.power", "omarchy.media", "omarchy.network"):
        assert shell_call_refusal(target, "toggle") is None


def test_self_refusal_names_the_plugin_and_a_way_forward():
    reason = shell_call_refusal(PLUGIN_ID, "stop")
    assert PLUGIN_ID in reason
    assert "panel" in reason
    # An agent told only "no" tries the next spelling.
    assert "status" in reason and "recent" in reason


def test_shell_route_is_the_same_door(commands):
    """`omarchy shell <target> <method>` is `omarchy_shell_call` by another
    name, and it is reachable through `omarchy_run`."""
    assert "omarchy shell" in commands
    assert self_refusal("omarchy shell", [PLUGIN_ID, "stop"]) is not None
    assert self_refusal("omarchy shell", [PLUGIN_ID, "status"]) is None
    assert self_refusal("omarchy shell", ["omarchy.power", "toggle"]) is None


def test_shell_route_flags_do_not_hide_the_target():
    assert self_refusal("omarchy shell", ["-q", PLUGIN_ID, "stop"]) is not None
    assert self_refusal("omarchy shell", [PLUGIN_ID, "stop", "--json"]) is not None


def test_shell_route_with_no_method_is_still_refused():
    assert self_refusal("omarchy shell", [PLUGIN_ID]) is not None


@pytest.mark.parametrize(
    "route",
    ["omarchy plugin disable", "omarchy plugin enable", "omarchy plugin remove",
     "omarchy plugin update", "omarchy plugin clone"],
)
def test_plugin_routes_naming_us_are_refused(route, commands):
    assert route in commands, f"{route} is no longer an Omarchy route"
    assert self_refusal(route, [PLUGIN_ID, "--yes"]) is not None
    assert self_refusal(route, ["some.other.plugin", "--yes"]) is None


def test_self_refusal_ignores_unrelated_routes():
    assert self_refusal("omarchy theme list", [PLUGIN_ID]) is None
    assert self_refusal("omarchy plugin list", [PLUGIN_ID]) is None


def test_self_refusal_is_not_promotable_by_config(commands):
    """No configuration reaches it: it is decided before `decide` is called,
    and `decide` never sees the arguments that make the call what it is."""
    cmd = commands["omarchy shell"]
    permissive = Config(
        allow=(cmd.route,),
        allow_groups=tuple(sorted({c.group for c in commands.values()})),
    )
    # The route itself is safe and stays so...
    assert decide(cmd, permissive).allowed is True
    # ...and the call is refused anyway, on its arguments.
    assert self_refusal(cmd.route, [PLUGIN_ID, "stop"]) is not None
