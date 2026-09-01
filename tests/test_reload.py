"""Re-reading the config file, and refusing to.

The reload is the one code path that can widen what an agent may run without
anybody restarting anything, so what it does with a file it does not
understand is as much of the specification as what it does with a good one.
"""

from __future__ import annotations

import logging

import pytest
from mcp.server.mcpserver import MCPServer

from omarchy_mcp import config as config_module, notify, reload as reload_module
from omarchy_mcp.config import Config
from omarchy_mcp.reload import Reloader
from omarchy_mcp.settings import Settings
from omarchy_mcp.stats import Stats
from omarchy_mcp.tools import control, desktop, feedback, generic, system
from omarchy_mcp.tools.catalogue import Catalogue

LOG = logging.getLogger("test")


@pytest.fixture(autouse=True)
def no_notifications(monkeypatch):
    """Nothing in this file may put a real notification on a real desktop."""
    sent = []
    monkeypatch.setattr(notify, "send", lambda *a, **kw: sent.append((a, kw)))
    monkeypatch.setattr(reload_module.notify, "send", lambda *a, **kw: sent.append((a, kw)))
    return sent


def build(tmp_path, text: str | None = None):
    """A settings holder, a live catalogue, and a reloader watching one file."""
    path = tmp_path / "config.toml"
    if text is not None:
        path.write_text(text)

    catalogue = Catalogue()
    # Started as the daemon starts: under whatever the file says right now.
    settings = Settings(config_module.load(path))
    stats = Stats()
    generic.register(catalogue, settings, LOG, stats)
    desktop.register(catalogue, settings, LOG, stats)
    system.register(catalogue, settings, LOG, stats)
    feedback.register(catalogue, settings, LOG, stats)
    control.register(catalogue, settings, LOG, stats)

    mcp = MCPServer(name="t")
    catalogue.apply(mcp, settings.current)
    reloader = Reloader(settings, catalogue, mcp, LOG, path=path)
    return reloader, settings, catalogue, path


# -- what a good edit does ---------------------------------------------------


def test_nothing_on_disk_changing_is_not_a_reload(tmp_path):
    reloader, _, _, _ = build(tmp_path, "[server]\ntimeout_ms = 5000\n")
    assert reloader.poll() is None


def test_a_tool_switched_off_is_taken_away(tmp_path):
    reloader, _, catalogue, path = build(tmp_path, "")
    assert "omarchy_screenshot" in catalogue.present

    path.write_text('[tools]\ndisabled = ["omarchy_screenshot"]\n')
    result = reloader.poll()

    assert result.tools.removed == ("omarchy_screenshot",)
    assert "omarchy_screenshot" not in catalogue.present


def test_a_tool_switched_back_on_returns(tmp_path):
    reloader, _, catalogue, path = build(tmp_path, '[tools]\ndisabled = ["omarchy_theme"]\n')
    assert "omarchy_theme" not in catalogue.present

    path.write_text("[tools]\ndisabled = []\n")
    result = reloader.poll()

    assert result.tools.added == ("omarchy_theme",)
    assert "omarchy_theme" in catalogue.present


def test_policy_takes_effect_without_a_restart(tmp_path):
    reloader, settings, _, path = build(tmp_path, "")
    assert settings.current.deny == ()

    path.write_text('[policy]\ndeny = ["omarchy theme set"]\nask = true\n')
    result = reloader.poll()

    assert result.policy_changed
    assert settings.current.deny == ("omarchy theme set",)
    assert settings.current.ask is True


def test_an_edit_nothing_can_tell_apart_is_not_announced(tmp_path, no_notifications):
    reloader, _, _, path = build(tmp_path, "[server]\ntimeout_ms = 5000\n")

    # A comment, and a value no rule depends on.
    path.write_text("# a note to self\n[server]\ntimeout_ms = 6000\n")
    result = reloader.poll()

    assert not result, "neither the tools nor the rules moved"
    assert no_notifications == []


# -- what a bad file does ----------------------------------------------------


def test_an_unparseable_file_changes_nothing(tmp_path, no_notifications):
    reloader, settings, catalogue, path = build(
        tmp_path, '[policy]\ndeny = ["omarchy theme set"]\n\n[tools]\ndisabled = ["omarchy_theme"]\n'
    )
    before = settings.current

    path.write_text("[policy\ndeny = ")
    result = reloader.poll()

    assert result.rejected
    # The running config stands: the deny list is not emptied and the disabled
    # tool does not come back on a keystroke.
    assert settings.current is before
    assert settings.current.deny == ("omarchy theme set",)
    assert "omarchy_theme" not in catalogue.present
    assert len(no_notifications) == 1


def test_a_file_left_broken_is_reported_once(tmp_path, no_notifications):
    reloader, _, _, path = build(tmp_path, "")

    path.write_text("[policy\n")
    reloader.poll()
    reloader.poll()
    reloader.poll()

    assert len(no_notifications) == 1, "a broken file must not toast every poll"


def test_fixing_the_file_applies_it_and_says_so(tmp_path, no_notifications):
    reloader, settings, _, path = build(tmp_path, "")
    path.write_text("[policy\n")
    reloader.poll()

    path.write_text('[policy]\ndeny = ["omarchy theme set"]\n')
    result = reloader.poll()

    assert not result.rejected
    assert settings.current.deny == ("omarchy theme set",)
    # The rejection, the policy change, and the recovery.
    assert len(no_notifications) == 3


def test_a_bad_key_still_applies_the_rest(tmp_path):
    # A value that fails validation is a footnote, not a refusal: startup
    # ignores it with a reason, and so does a reload.
    reloader, settings, _, path = build(tmp_path, "")

    path.write_text('[server]\ntimeout_ms = "soon"\n\n[policy]\ndeny = ["omarchy theme set"]\n')
    result = reloader.poll()

    assert not result.rejected
    assert settings.current.deny == ("omarchy theme set",)
    assert settings.current.timeout_ms == Config().timeout_ms


# -- what a missing file does ------------------------------------------------


def test_a_file_that_vanishes_for_one_poll_is_a_rename(tmp_path):
    reloader, settings, _, path = build(tmp_path, '[policy]\ndeny = ["omarchy theme set"]\n')

    path.unlink()
    assert reloader.poll() is None, "an editor's write-and-rename is not a deletion"
    assert settings.current.deny == ("omarchy theme set",)


def test_a_file_that_stays_deleted_resets_to_defaults(tmp_path):
    reloader, settings, _, path = build(tmp_path, '[policy]\ndeny = ["omarchy theme set"]\n')

    path.unlink()
    reloader.poll()
    result = reloader.poll()

    assert result.policy_changed
    assert settings.current == Config()


def test_a_file_renamed_into_place_is_read(tmp_path):
    reloader, settings, _, path = build(tmp_path, "")

    path.unlink()
    reloader.poll()
    path.write_text('[policy]\nallow = ["omarchy system reboot"]\n')
    reloader.poll()

    assert settings.current.allow == ("omarchy system reboot",)


# -- the boundary ------------------------------------------------------------


def test_a_reload_cannot_promote_a_sudo_command(tmp_path):
    # `blocked` is computed from the command, not from the config, and nothing
    # about reloading it live changes that.
    from omarchy_mcp import registry
    from omarchy_mcp.policy import Tier, decide

    reloader, settings, _, path = build(tmp_path, "")
    sudo = next(c for c in registry.all_commands().values() if c.requires_sudo)

    path.write_text(f'[policy]\nallow = ["{sudo.route}"]\n')
    reloader.poll()

    verdict = decide(sudo, settings.current)
    assert verdict.tier is Tier.BLOCKED
    assert not verdict.allowed
