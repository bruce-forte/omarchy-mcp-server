"""Policy is the security boundary, so it is tested against every route Omarchy
ships rather than against a handful of examples.

What a tier *means* for a given call -- refused, asked about, allowed -- is
`permissions.py`'s question and is tested in `test_permissions.py`. This file is
about the classification itself, and about the one refusal that reads a call's
arguments rather than its route."""

from __future__ import annotations

import pytest

from omarchy_mcp.paths import PLUGIN_ID
from omarchy_mcp.policy import (
    GUARDED_GROUPS,
    GUARDED_ROUTES,
    SELF_READ_VERBS,
    Tier,
    base_tier,
    self_refusal,
    shell_call_refusal,
)


def test_every_sudo_command_is_blocked(commands):
    sudo = [c for c in commands.values() if c.requires_sudo]
    assert sudo, "fixture should contain sudo commands"
    assert all(base_tier(c) is Tier.BLOCKED for c in sudo)


def test_guarded_groups_classify_as_guarded(commands):
    guarded = [c for c in commands.values() if not c.requires_sudo and c.group in GUARDED_GROUPS]
    assert guarded
    assert all(base_tier(c) is Tier.GUARDED for c in guarded)


def test_ordinary_read_only_commands_are_safe(commands):
    for route in ("omarchy theme list", "omarchy battery status", "omarchy network status"):
        assert base_tier(commands[route]) is Tier.SAFE


def test_every_command_classifies(commands):
    """No route falls through the classifier."""
    for cmd in commands.values():
        assert isinstance(base_tier(cmd), Tier)


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


def test_self_refusal_reads_arguments_which_no_tier_does(commands):
    """No rule reaches it, because it is decided before the ladder runs and the
    ladder never sees the arguments that make the call what it is."""
    cmd = commands["omarchy shell"]
    # The route itself is safe and stays so...
    assert base_tier(cmd) is Tier.SAFE
    # ...and the call is refused anyway, on its arguments.
    assert self_refusal(cmd.route, [PLUGIN_ID, "stop"]) is not None
    assert self_refusal(cmd.route, ["omarchy.power", "toggle"]) is None
