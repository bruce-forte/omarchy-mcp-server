"""Re-reading the config file, and refusing to.

The reload is the one code path that can widen what an agent may run without
anybody restarting anything, so what it does with a file it does not
understand is as much of the specification as what it does with a good one.
"""

from __future__ import annotations

import json
import logging

import pytest
from mcp.server.mcpserver import MCPServer

from omarchy_mcp import (
    config as config_module,
    notify,
    permissions as permissions_module,
    reload as reload_module,
)
from omarchy_mcp.config import Config
from omarchy_mcp.permissions import Effect, Permissions
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


def doc(**lists) -> str:
    block = {
        effect: [{"kind": "route", "matcher": m} for m in matchers]
        for effect, matchers in lists.items()
    }
    return json.dumps({"permissions": block})


def permission_paths(tmp_path):
    """Never the real ones. `Reloader` defaults to the user's own files, which
    is right in the daemon and wrong in a test on the developer's machine."""
    return (tmp_path / "permissions.json", tmp_path / "permissions.local.json")


def build(tmp_path, text: str | None = None, permissions: str | None = None):
    """A settings holder, a live catalogue, and a reloader watching the files."""
    path = tmp_path / "config.toml"
    if text is not None:
        path.write_text(text)
    perms = permission_paths(tmp_path)
    if permissions is not None:
        perms[0].write_text(permissions)

    catalogue = Catalogue()
    # Started as the daemon starts: under whatever the file says right now.
    settings = Settings(config_module.load(path), permissions_module.load(perms))
    stats = Stats()
    generic.register(catalogue, settings, LOG, stats)
    desktop.register(catalogue, settings, LOG, stats)
    system.register(catalogue, settings, LOG, stats)
    feedback.register(catalogue, settings, LOG, stats)
    control.register(catalogue, settings, LOG, stats)

    mcp = MCPServer(name="t")
    catalogue.apply(mcp, settings.current)
    reloader = Reloader(settings, catalogue, mcp, LOG, path=path, permission_paths=perms)
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


def test_permissions_take_effect_without_a_restart(tmp_path):
    reloader, settings, _, _ = build(tmp_path, "")
    assert settings.permissions.rules == ()

    permission_paths(tmp_path)[0].write_text('{"permissions": {"deny": [{"kind": "route", "matcher": "omarchy theme set"}]}}')
    result = reloader.poll()

    assert result.permissions_changed
    matched = settings.permissions.matching("omarchy theme set")
    assert matched is not None and matched.effect is Effect.DENY


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
        tmp_path, '[tools]\ndisabled = ["omarchy_theme"]\n'
    )
    before = settings.current

    path.write_text("[tools\ndisabled = ")
    result = reloader.poll()

    assert result.rejected
    # The running config stands: the disabled tool does not come back on a
    # keystroke.
    assert settings.current is before
    assert "omarchy_theme" not in catalogue.present
    assert len(no_notifications) == 1


def test_a_file_left_broken_is_reported_once(tmp_path, no_notifications):
    reloader, _, _, path = build(tmp_path, "")

    path.write_text("[tools\n")
    reloader.poll()
    reloader.poll()
    reloader.poll()

    assert len(no_notifications) == 1, "a broken file must not toast every poll"


def test_fixing_the_file_applies_it_and_says_so(tmp_path, no_notifications):
    reloader, settings, catalogue, path = build(tmp_path, "")
    path.write_text("[tools\n")
    reloader.poll()

    path.write_text('[tools]\ndisabled = ["omarchy_theme"]\n')
    result = reloader.poll()

    assert not result.rejected
    assert "omarchy_theme" not in catalogue.present
    # The rejection and the recovery.
    assert len(no_notifications) == 2


def test_a_bad_key_still_applies_the_rest(tmp_path):
    # A value that fails validation is a footnote, not a refusal: startup
    # ignores it with a reason, and so does a reload.
    reloader, settings, _, path = build(tmp_path, "")

    path.write_text('[server]\ntimeout_ms = "soon"\n\n[tools]\ndisabled = ["omarchy_theme"]\n')
    result = reloader.poll()

    assert not result.rejected
    assert settings.current.disabled_tools == ("omarchy_theme",)
    assert settings.current.timeout_ms == Config().timeout_ms


# -- what a missing file does ------------------------------------------------


def test_a_file_that_vanishes_for_one_poll_is_a_rename(tmp_path):
    reloader, settings, _, path = build(tmp_path, '[tools]\ndisabled = ["omarchy_theme"]\n')

    path.unlink()
    assert reloader.poll() is None, "an editor's write-and-rename is not a deletion"
    assert settings.current.disabled_tools == ("omarchy_theme",)


def test_a_file_that_stays_deleted_resets_to_defaults(tmp_path):
    reloader, settings, catalogue, path = build(
        tmp_path, '[tools]\ndisabled = ["omarchy_theme"]\n'
    )

    path.unlink()
    reloader.poll()
    result = reloader.poll()

    assert result.tools
    assert settings.current == Config()
    assert "omarchy_theme" in catalogue.present


def test_a_file_renamed_into_place_is_read(tmp_path):
    reloader, settings, _, path = build(tmp_path, "")

    path.unlink()
    reloader.poll()
    path.write_text('[tools]\ndisabled = ["omarchy_theme"]\n')
    reloader.poll()

    assert settings.current.disabled_tools == ("omarchy_theme",)


# -- the boundary ------------------------------------------------------------


def test_a_reload_cannot_promote_a_sudo_command(tmp_path):
    # `blocked` is computed from the command, not from the document, and nothing
    # about reloading it live changes that.
    from omarchy_mcp import registry
    from omarchy_mcp.permissions import decide
    from omarchy_mcp.policy import Tier

    reloader, settings, _, _ = build(tmp_path, "")
    sudo = next(c for c in registry.all_commands().values() if c.requires_sudo)

    permission_paths(tmp_path)[0].write_text(
        json.dumps(
            {"permissions": {"allow": [{"kind": "route", "matcher": "omarchy *"}]}}
        )
    )
    reloader.poll()

    outcome = decide(sudo, settings.permissions)
    assert outcome.tier is Tier.BLOCKED
    assert not outcome.allowed


# -- what a bad permissions document does ------------------------------------


def test_a_defective_document_leaves_the_running_one_in_force(tmp_path, no_notifications):
    """At startup any defect refuses to start. Here there is a known-good
    document -- the one the user last successfully wrote -- so it stands. An
    editor's mid-keystroke autosave must not drop every attached session."""
    reloader, settings, _, _ = build(
        tmp_path, "", json.dumps({"permissions": {"deny": [{"kind": "route", "matcher": "omarchy dev *"}]}})
    )
    before = settings.permissions
    assert before.rules

    permission_paths(tmp_path)[0].write_text('{"permissions": {"deny": [')
    result = reloader.poll()

    assert result.permissions_rejected
    assert not result.permissions_changed
    assert settings.permissions is before
    assert len(no_notifications) == 1


def test_a_document_left_broken_is_reported_once(tmp_path, no_notifications):
    reloader, _, _, _ = build(tmp_path, "")

    for i in range(3):
        # Different bytes each time, so this is three fresh saves rather than
        # three polls of one unchanged file.
        permission_paths(tmp_path)[0].write_text('{"permissions": {"deny": [' + " " * i)
        reloader.poll()

    assert len(no_notifications) == 1, "a broken document must not toast every poll"


def test_fixing_the_document_applies_it_and_says_so(tmp_path, no_notifications):
    reloader, settings, _, _ = build(tmp_path, "")
    permission_paths(tmp_path)[0].write_text("{")
    reloader.poll()

    permission_paths(tmp_path)[0].write_text(
        json.dumps({"permissions": {"deny": [{"kind": "route", "matcher": "omarchy dev *"}]}})
    )
    result = reloader.poll()

    assert not result.permissions_rejected
    assert settings.permissions.matching("omarchy dev link") is not None
    # The rejection, the change, and the recovery.
    assert len(no_notifications) == 3


def test_a_widening_and_a_narrowing_are_both_announced(tmp_path, no_notifications):
    """A widening matters most, but a narrowing explains a refusal that is about
    to happen and would otherwise look like a bug."""
    reloader, _, _, _ = build(tmp_path, "")

    permission_paths(tmp_path)[0].write_text(
        json.dumps({"permissions": {"allow": [{"kind": "route", "matcher": "omarchy install *"}]}})
    )
    reloader.poll()
    assert "+allow: omarchy install *" in no_notifications[-1][0][1]

    permission_paths(tmp_path)[0].write_text(json.dumps({"permissions": {}}))
    reloader.poll()
    assert "-allow: omarchy install *" in no_notifications[-1][0][1]


def test_the_second_file_is_pooled_with_the_first(tmp_path):
    """The daemon writes permissions.local.json; both are one document, and a
    change to either is one reload."""
    reloader, settings, _, _ = build(tmp_path, "")

    permission_paths(tmp_path)[1].write_text(
        json.dumps({"permissions": {"allow": [{"kind": "route", "matcher": "omarchy install app"}]}})
    )
    result = reloader.poll()

    assert result.permissions_changed
    matched = settings.permissions.matching("omarchy install app")
    assert matched is not None and matched.source == "permissions.local.json"


# -- acknowledging a review --------------------------------------------------


def _review_for(tmp_path, commands, *, missing="omarchy install app"):
    """A snapshot from before `missing` existed, and the review that produces."""
    from omarchy_mcp import delta

    seen_path = tmp_path / "registry-seen.json"
    seen_path.write_text(json.dumps({"routes": sorted(set(commands) - {missing})}))
    return seen_path, delta.compute(delta.load_seen(seen_path), commands, Permissions())


def test_acknowledging_advances_the_snapshot(tmp_path, commands, no_notifications):
    from omarchy_mcp import delta

    seen_path, review = _review_for(tmp_path, commands)
    reloader, settings, _, _ = build(tmp_path, "")
    reloader._seen_path = seen_path
    reloader._consent_dir = tmp_path / "consent"
    reloader._review = review
    (tmp_path / "consent").mkdir()

    (tmp_path / "consent" / f"ack-{review.token}").write_text(review.token)
    result = reloader.poll()

    assert result.acknowledged
    assert delta.load_seen(seen_path) == frozenset(commands)
    assert settings.unreviewed == frozenset()
    assert not reloader.review


def test_a_wrong_token_acknowledges_nothing(tmp_path, commands, no_notifications):
    from omarchy_mcp import delta

    seen_path, review = _review_for(tmp_path, commands)
    reloader, _, _, _ = build(tmp_path, "")
    reloader._seen_path = seen_path
    reloader._consent_dir = tmp_path / "consent"
    reloader._review = review
    (tmp_path / "consent").mkdir()

    (tmp_path / "consent" / f"ack-{review.token}").write_text(delta.new_token())
    assert reloader.poll() is None
    assert delta.load_seen(seen_path) != frozenset(commands)


def test_a_marker_is_spent_once(tmp_path, commands, no_notifications):
    """A leftover must never acknowledge a later review."""
    seen_path, review = _review_for(tmp_path, commands)
    reloader, _, _, _ = build(tmp_path, "")
    reloader._seen_path = seen_path
    reloader._consent_dir = tmp_path / "consent"
    reloader._review = review
    (tmp_path / "consent").mkdir()
    marker = tmp_path / "consent" / f"ack-{review.token}"
    marker.write_text(review.token)

    reloader.poll()
    assert not marker.exists()


def test_an_ordinary_poll_does_not_advance_the_snapshot(tmp_path, commands, no_notifications):
    """Only acknowledgement moves it. A reload that quietly consumed the warning
    would be the bug this whole feature is built to avoid."""
    from omarchy_mcp import delta

    seen_path, review = _review_for(tmp_path, commands)
    reloader, _, _, path = build(tmp_path, "")
    reloader._seen_path = seen_path
    reloader._review = review

    path.write_text('[tools]\ndisabled = ["omarchy_theme"]\n')
    reloader.poll()

    assert delta.load_seen(seen_path) != frozenset(commands)
    assert reloader.review


def test_a_rule_change_recomputes_what_is_held(tmp_path, commands, no_notifications):
    """A new deny can release a route from quarantine by denying it outright,
    which is a different answer rather than the same one delayed."""
    from omarchy_mcp import delta

    seen_path = tmp_path / "registry-seen.json"
    seen_path.write_text(json.dumps({"routes": sorted(set(commands) - {"omarchy install app"})}))
    reloader, settings, _, _ = build(
        tmp_path, "", doc(allow=["omarchy install *"])
    )
    reloader._seen_path = seen_path
    reloader.recompute_review()
    assert settings.unreviewed == frozenset({"omarchy install app"})

    permission_paths(tmp_path)[0].write_text(
        doc(allow=["omarchy install *"], deny=["omarchy install app"])
    )
    reloader.poll()

    assert settings.unreviewed == frozenset(), "denied outright, not held"
    assert delta.load_seen(seen_path) != frozenset(commands), "and still not acknowledged"


def test_pruning_needs_the_token_the_daemon_published(tmp_path, commands, no_notifications):
    from omarchy_mcp import delta, permissions as perms

    local = permission_paths(tmp_path)[1]
    local.write_text(doc(allow=["omarchy install app", "omarchy gone away"]))
    reloader, _, _, _ = build(tmp_path, "")
    reloader._local_path = local
    reloader._consent_dir = tmp_path / "consent"
    (tmp_path / "consent").mkdir()

    token = delta.new_token()
    reloader.offer_prune(token)

    # A file naming the wrong token is not a press.
    (tmp_path / "consent" / f"prune-{token}").write_text(delta.new_token())
    assert reloader.poll() is None
    assert len(perms.prunable(local, commands)) == 1

    (tmp_path / "consent" / f"prune-{token}").write_text(token)
    result = reloader.poll()

    assert result.pruned == 1
    assert perms.prunable(local, commands) == ()
    assert reloader.prune_token == "", "an offer is taken once"


def test_pruning_records_what_it_removed(tmp_path, commands, no_notifications, monkeypatch):
    from omarchy_mcp import delta

    noted = []
    monkeypatch.setattr(reload_module.activity, "note", lambda name, **f: noted.append((name, f)))

    local = permission_paths(tmp_path)[1]
    local.write_text(doc(allow=["omarchy gone away"]))
    reloader, _, _, _ = build(tmp_path, "")
    reloader._local_path = local
    reloader._consent_dir = tmp_path / "consent"
    (tmp_path / "consent").mkdir()
    token = delta.new_token()
    reloader.offer_prune(token)
    (tmp_path / "consent" / f"prune-{token}").write_text(token)

    reloader.poll()

    assert noted == [
        ("permission", {"verb": "prune", "route": "omarchy gone away", "via": "panel"})
    ]


def test_nothing_prunes_without_an_offer(tmp_path, commands, no_notifications):
    local = permission_paths(tmp_path)[1]
    local.write_text(doc(allow=["omarchy gone away"]))
    reloader, _, _, _ = build(tmp_path, "")
    reloader._local_path = local
    reloader._consent_dir = tmp_path / "consent"
    (tmp_path / "consent").mkdir()

    # No `offer_prune`, so no token exists and no filename can name it.
    (tmp_path / "consent" / "prune-anything").write_text("anything")
    assert reloader.poll() is None
