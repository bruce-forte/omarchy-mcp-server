"""What a defective permissions document does to a starting daemon.

`config.toml`'s rule is "report it and ignore it", and that is right for a file
where ignoring a key falls back to a safe default. It inverts here: ignoring a
`deny` is a loss of protection, "no rules" is not the safe floor, and at startup
there is no known-good document to keep. So the daemon does not start.

Which makes the exit code load-bearing. `Service.qml` retries a crash with a
backoff and, after three, advises a `rebuild` that deletes the virtualenv --
exactly the wrong advice for a missing comma, and it would retry a file that
cannot fix itself.
"""

from __future__ import annotations

import json
import logging

import pytest

from omarchy_mcp import __main__ as entry
from omarchy_mcp import notify, permissions as permissions_module

LOG = logging.getLogger("test")


@pytest.fixture(autouse=True)
def notifications(monkeypatch):
    """Nothing here reaches a real desktop; the calls are the assertion."""
    sent = []
    monkeypatch.setattr(notify, "send", lambda *a, **kw: sent.append((a, kw)))
    monkeypatch.setattr(entry.notify, "send", lambda *a, **kw: sent.append((a, kw)))
    return sent


@pytest.fixture(autouse=True)
def frames_out(monkeypatch):
    emitted = []
    monkeypatch.setattr(entry.frames, "emit", lambda state, **f: emitted.append((state, f)))
    return emitted


@pytest.fixture
def config_file(tmp_path, monkeypatch):
    path = tmp_path / "config.toml"
    monkeypatch.setattr(entry, "CONFIG_FILE", path)
    return path


@pytest.fixture
def spawned(monkeypatch):
    """What `--edit` handed to `execute.run`. Nothing reaches a real editor."""
    calls = []

    def fake(argv, **kwargs):
        calls.append((argv, kwargs))
        return entry.execute.Result(
            exit_code=None, stdout="", stderr="", timed_out=False, detached=True, pid=4321
        )

    monkeypatch.setattr(entry.execute, "run", fake)
    return calls


@pytest.fixture
def permission_files(tmp_path, monkeypatch):
    """The two real paths, redirected. Never the developer's own."""
    paths = (tmp_path / "permissions.json", tmp_path / "permissions.local.json")
    monkeypatch.setattr(entry, "PERMISSIONS_FILES", paths)
    monkeypatch.setattr(entry, "PERMISSIONS_FILE", paths[0])
    monkeypatch.setattr(entry, "PERMISSIONS_LOCAL_FILE", paths[1])
    return paths


def doc(**lists) -> str:
    block = {
        effect: [{"kind": "route", "matcher": m} for m in matchers]
        for effect, matchers in lists.items()
    }
    return json.dumps({"permissions": block})


class TestStartingUp:
    def test_no_file_at_all_starts(self, permission_files):
        """The ordinary state of a fresh install. Refusing here would make the
        plugin look broken on arrival."""
        loaded = entry._load_permissions(LOG)
        assert loaded is not None
        assert loaded.rules == ()

    def test_an_empty_document_starts(self, permission_files):
        permission_files[0].write_text('{"permissions": {}}')
        assert entry._load_permissions(LOG) is not None

    def test_a_good_document_starts_and_is_in_force(self, permission_files):
        permission_files[0].write_text(doc(deny=["omarchy dev *"]))
        loaded = entry._load_permissions(LOG)
        assert loaded is not None
        assert loaded.matching("omarchy dev link") is not None

    @pytest.mark.parametrize(
        "body",
        [
            "{",
            '{"permissions": {"allow": [{"kind": "route", "matcher": "omarchy install*"}]}}',
            '{"permission": {}}',
            '{"permissions": {"guardedDefault": "maybe"}}',
        ],
    )
    def test_any_defect_refuses_to_start(self, permission_files, body):
        permission_files[0].write_text(body)
        assert entry._load_permissions(LOG) is None

    def test_a_rule_the_system_will_never_honour_refuses_to_start(
        self, permission_files, commands
    ):
        """Well-formed, and still refused: believing you granted something you
        did not is worse than being told loudly."""
        sudo = next(c for c in commands.values() if c.requires_sudo)
        permission_files[0].write_text(doc(allow=[sudo.route]))
        assert entry._load_permissions(LOG) is None

    def test_a_dead_rule_does_not_refuse_to_start(self, permission_files):
        """A matcher matching nothing is indistinguishable from one for a route
        that has not shipped yet. Refusing would turn an upstream rename into a
        daemon that will not start."""
        permission_files[0].write_text(doc(deny=["omarchy nosuchthing *"]))
        assert entry._load_permissions(LOG) is not None


class TestSayingWhy:
    def test_it_is_said_three_ways(self, permission_files, notifications, frames_out):
        """Each reaches a different person: stderr for the journal, a frame so
        the bar has the reason, and a notification because the only other client
        is a language model that never got to attach."""
        permission_files[0].write_text("{")
        entry._load_permissions(LOG)

        assert any(state == "failed" for state, _ in frames_out)
        assert len(notifications) == 1
        (headline, body), kwargs = notifications[0]
        assert "permissions" in headline.lower()
        assert kwargs["urgency"] == "critical"

    def test_the_notification_names_the_way_out(self, permission_files, notifications):
        permission_files[0].write_text("{")
        entry._load_permissions(LOG)
        body = notifications[0][0][1]
        assert "--check-permissions" in body
        assert "did not start" in body


class TestTheExitCode:
    def test_it_is_ex_config_and_not_a_crash(self):
        """`Service.qml` reads this exact number to stop respawning. A generic
        failure would be retried with a backoff and then blamed on the venv."""
        assert entry.EX_CONFIG == 78

    def test_main_exits_with_it(self, permission_files, monkeypatch):
        permission_files[0].write_text("{")
        # Nothing may bind a port or write a token on this path.
        monkeypatch.setattr(
            entry.token_module, "ensure", lambda: pytest.fail("started despite a bad document")
        )
        assert entry.main([]) == entry.EX_CONFIG

    def test_the_service_watches_for_that_number(self):
        """The two halves are in different languages and cannot share a
        constant, so the test is what keeps them equal."""
        import pathlib

        qml = pathlib.Path(__file__).resolve().parents[1] / "Service.qml"
        assert f"exitBadPermissions: {entry.EX_CONFIG}" in qml.read_text()


class TestCheckPermissions:
    """With the daemon down the bar panel is the only surface left, so it has to
    be able to say "valid now, press Start" rather than restarting to find out."""

    def test_a_good_document_exits_zero(self, permission_files, capsys):
        permission_files[0].write_text(doc(allow=["omarchy theme *"]))
        assert entry._check_permissions(LOG) == 0
        assert "would start" in capsys.readouterr().out

    def test_a_defective_document_exits_ex_config(self, permission_files, capsys):
        permission_files[0].write_text('{"permissions": {"allow": [{"kind": "route"}]}}')
        assert entry._check_permissions(LOG) == entry.EX_CONFIG
        assert "matcher" in capsys.readouterr().out

    def test_it_names_both_files_and_whether_they_are_there(self, permission_files, capsys):
        permission_files[0].write_text('{"permissions": {}}')
        entry._check_permissions(LOG)
        out = capsys.readouterr().out
        assert "permissions.json: found" in out
        assert "permissions.local.json: not present" in out

    def test_a_dead_rule_is_reported_without_failing(self, permission_files, capsys):
        permission_files[0].write_text(doc(deny=["omarchy nosuchthing *"]))
        assert entry._check_permissions(LOG) == 0
        assert "void" in capsys.readouterr().out

    def test_it_notifies_nobody(self, permission_files, notifications):
        """Run by a person who is looking at the output. A notification would be
        telling them what they can already read."""
        permission_files[0].write_text("{")
        entry._check_permissions(LOG)
        assert notifications == []


def test_the_document_reaches_the_settings_holder(permission_files, monkeypatch):
    """The wiring, end to end: what `_load_permissions` returns is what the
    gate reads on the next call."""
    permission_files[0].write_text(doc(deny=["omarchy dev *"]))
    loaded = entry._load_permissions(LOG)

    from omarchy_mcp.config import Config
    from omarchy_mcp.permissions import Effect, decide
    from omarchy_mcp.settings import Settings

    settings = Settings(Config(), loaded)
    from omarchy_mcp import registry

    outcome = decide(registry.all_commands()["omarchy dev link"], settings.permissions)
    assert outcome.effect is Effect.DENY


def test_permissions_module_is_the_one_that_decides():
    """A guard against the ladder quietly growing a second home."""
    assert hasattr(permissions_module, "decide")
    from omarchy_mcp import policy

    assert not hasattr(policy, "decide"), "the ladder lives in permissions.py"


class TestPrintPermissions:
    """The same report as `omarchy://permissions`, for a terminal -- including
    the terminal of somebody whose daemon is refusing to start."""

    def test_no_rules_says_so_rather_than_printing_nothing(self, permission_files, capsys):
        assert entry._print_permissions(LOG) == 0
        out = capsys.readouterr().out
        assert "No rules" in out
        assert "guardedDefault: ask" in out

    def test_a_rule_is_printed_with_its_file_and_what_it_covers(
        self, permission_files, capsys
    ):
        permission_files[0].write_text(doc(deny=["omarchy dev *"]))
        assert entry._print_permissions(LOG) == 0
        out = capsys.readouterr().out
        assert "omarchy dev *" in out
        assert "permissions.json" in out
        assert "omarchy dev link" in out, "the expansion is the point"

    def test_precedence_is_stated_not_implied(self, permission_files, capsys):
        entry._print_permissions(LOG)
        assert "deny -> ask -> allow" in capsys.readouterr().out

    def test_a_dead_rule_is_flagged(self, permission_files, capsys):
        permission_files[0].write_text(doc(deny=["omarchy nosuchthing *"]))
        entry._print_permissions(LOG)
        assert "[void]" in capsys.readouterr().out

    def test_json_is_the_same_report_the_resource_serves(self, permission_files, capsys):
        permission_files[0].write_text(doc(allow=["omarchy theme *"]))
        assert entry._print_permissions(LOG, as_json=True) == 0

        from omarchy_mcp import permissions as perms_module
        from omarchy_mcp import registry

        printed = json.loads(capsys.readouterr().out)
        expected = perms_module.explain(
            perms_module.load(permission_files), registry.all_commands()
        )
        assert printed == expected

    def test_a_defective_document_exits_ex_config(self, permission_files, capsys):
        permission_files[0].write_text("{")
        assert entry._print_permissions(LOG) == entry.EX_CONFIG
        assert "would not start" in capsys.readouterr().out

    def test_it_notifies_nobody(self, permission_files, notifications):
        permission_files[0].write_text("{")
        entry._print_permissions(LOG)
        assert notifications == []


class TestTheReviewAtStartup:
    @pytest.fixture
    def seen(self, tmp_path, monkeypatch):
        path = tmp_path / "registry-seen.json"
        monkeypatch.setattr(entry, "REGISTRY_SEEN_FILE", path)
        return path

    def test_a_first_run_baselines_silently(self, seen, notifications, commands):
        """Every route is new on a fresh install, and telling somebody about 400
        of them is the catalogue this feature exists to avoid."""
        from omarchy_mcp import delta

        review = entry._review(permissions_module.Permissions(), LOG)

        assert review.first_run
        assert notifications == []
        assert delta.load_seen(seen) == frozenset(commands)

    def test_nothing_changed_says_nothing(self, seen, notifications, commands):
        from omarchy_mcp import delta

        delta.save_seen(seen, commands)
        assert not entry._review(permissions_module.Permissions(), LOG)
        assert notifications == []

    def test_a_change_notifies_and_does_not_advance_the_snapshot(
        self, seen, notifications, commands
    ):
        """Otherwise the next boot overwrites the snapshot and the evidence is
        gone before anybody clicked."""
        from omarchy_mcp import delta

        before = frozenset(commands) - {"omarchy install app"}
        seen.write_text(json.dumps({"routes": sorted(before)}))

        review = entry._review(permissions_module.Permissions(), LOG)

        assert review
        assert len(notifications) == 1
        assert delta.load_seen(seen) == before, "startup must never advance it"

    def test_a_widened_rule_is_critical(self, seen, notifications, commands):
        from omarchy_mcp.permissions import Effect, Permissions, Rule

        before = frozenset(commands) - {"omarchy install app"}
        seen.write_text(json.dumps({"routes": sorted(before)}))
        perms = Permissions(
            rules=(
                Rule(
                    effect=Effect.ALLOW,
                    matcher="omarchy install *",
                    source="permissions.json",
                    index=0,
                ),
            )
        )

        entry._review(perms, LOG)
        assert notifications[0][1]["urgency"] == "critical"

    def test_new_guarded_routes_alone_are_not_critical(self, seen, notifications, commands):
        """They ask anyway."""
        before = frozenset(commands) - {"omarchy install app"}
        seen.write_text(json.dumps({"routes": sorted(before)}))
        entry._review(permissions_module.Permissions(), LOG)
        assert notifications[0][1]["urgency"] == "normal"

    def test_no_registry_is_not_a_reason_to_report_anything(self, seen, monkeypatch):
        monkeypatch.setattr(
            entry.registry,
            "all_commands",
            lambda: (_ for _ in ()).throw(entry.registry.RegistryError("no omarchy")),
        )
        assert not entry._review(permissions_module.Permissions(), LOG)
        assert not seen.exists()


class TestPrintReview:
    @pytest.fixture
    def seen(self, tmp_path, monkeypatch):
        path = tmp_path / "registry-seen.json"
        monkeypatch.setattr(entry, "REGISTRY_SEEN_FILE", path)
        return path

    def test_a_first_run_says_so(self, seen, permission_files, capsys):
        assert entry._print_review(LOG) == 0
        assert "No snapshot yet" in capsys.readouterr().out

    def test_nothing_changed_says_so(self, seen, permission_files, commands, capsys):
        from omarchy_mcp import delta

        delta.save_seen(seen, commands)
        assert entry._print_review(LOG) == 0
        assert "Nothing has changed" in capsys.readouterr().out

    def test_it_names_what_is_held_and_why(self, seen, permission_files, commands, capsys):
        seen.write_text(json.dumps({"routes": sorted(set(commands) - {"omarchy install app"})}))
        permission_files[0].write_text(doc(allow=["omarchy install *"]))

        assert entry._print_review(LOG) == 0
        out = capsys.readouterr().out
        assert "Held at ask until acknowledged" in out
        assert "omarchy install app" in out
        assert "omarchy install *" in out

    def test_json_carries_no_token(self, seen, permission_files, commands, capsys):
        """It is read-only. Acknowledging is a separate action on a surface a
        person is at."""
        seen.write_text(json.dumps({"routes": sorted(set(commands) - {"omarchy install app"})}))
        assert entry._print_review(LOG, as_json=True) == 0
        body = json.loads(capsys.readouterr().out)
        assert "token" not in body
        assert body["arrivals"][0]["route"] == "omarchy install app"

    def test_it_reads_nothing_it_cannot_read(self, seen, permission_files, capsys):
        permission_files[0].write_text("{")
        assert entry._print_review(LOG) == entry.EX_CONFIG
        assert "cannot be read" in capsys.readouterr().out


class TestPrunableAtStartup:
    @pytest.fixture
    def local(self, permission_files):
        return permission_files[1]

    def test_dead_rules_are_found(self, local, commands):
        local.write_text(doc(allow=["omarchy gone away"]))
        assert [r.matcher for r in entry._prunable(LOG)] == ["omarchy gone away"]

    def test_a_clean_file_offers_nothing(self, local):
        local.write_text(doc(allow=["omarchy install *"]))
        assert entry._prunable(LOG) == ()

    def test_it_is_never_notified_about(self, local, notifications, seen_baseline):
        """Housekeeping, not a warning: a rule that matches nothing grants
        nothing. It is offered in the panel and nowhere else."""
        local.write_text(doc(allow=["omarchy gone away"]))
        entry._prunable(LOG)
        assert notifications == []

    def test_the_report_previews_what_would_go(self, local, permission_files, capsys):
        local.write_text(doc(allow=["omarchy gone away"]))
        entry._print_review(LOG, as_json=True)
        body = json.loads(capsys.readouterr().out)
        assert body["prunable"] == [
            {"effect": "allow", "matcher": "omarchy gone away", "source": "permissions.local.json"}
        ]

    def test_the_text_report_names_them(self, local, permission_files, capsys):
        local.write_text(doc(allow=["omarchy gone away"]))
        entry._print_review(LOG)
        out = capsys.readouterr().out
        assert "match nothing" in out
        assert "omarchy gone away" in out

    @pytest.fixture
    def seen_baseline(self, tmp_path, monkeypatch, commands):
        from omarchy_mcp import delta

        path = tmp_path / "registry-seen.json"
        monkeypatch.setattr(entry, "REGISTRY_SEEN_FILE", path)
        delta.save_seen(path, commands)
        return path


class TestEditing:
    """The Edit button's other half. It opens a file, and the only thing it may
    write is one that is not there yet."""

    def test_it_opens_the_users_own_file(self, permission_files, spawned):
        assert entry._edit("permissions", LOG) == 0
        argv, kwargs = spawned[0]
        assert argv == ["omarchy", "launch", "editor", str(permission_files[0])]
        assert kwargs["detach"] is True, "an editor outlives the daemon that opened it"

    def test_it_opens_the_daemons_own_file(self, permission_files, spawned):
        entry._edit("local", LOG)
        assert spawned[0][0][-1] == str(permission_files[1])

    def test_it_opens_the_config(self, config_file, spawned):
        entry._edit("config", LOG)
        assert spawned[0][0][-1] == str(config_file)

    def test_a_missing_permissions_file_is_seeded_first(self, permission_files, spawned):
        """An empty buffer has no $schema line, and that line is what validates
        a matcher before this daemon ever sees it."""
        entry._edit("permissions", LOG)
        body = json.loads(permission_files[0].read_text())
        assert body["$schema"] == permissions_module.SCHEMA_URL
        assert permissions_module.load([permission_files[0]]).rules == ()

    def test_an_existing_file_is_never_rewritten(self, permission_files, spawned):
        permission_files[0].write_text(doc(allow=["omarchy theme set"]))
        before = permission_files[0].read_text()
        entry._edit("permissions", LOG)
        assert permission_files[0].read_text() == before

    def test_the_config_is_never_seeded_here(self, config_file, spawned):
        """`bin/omarchy-mcpd` owns that template, documented knobs and all. A
        second copy in Python would be a second thing to keep current."""
        entry._edit("config", LOG)
        assert not config_file.exists()

    def test_it_opens_nothing_else(self):
        """Fixed names, no path argument: the verb is reachable from a bar
        button."""
        assert set(entry.EDITABLE) == {"config", "local", "permissions"}
        with pytest.raises(KeyError):
            entry._editable("/etc/shadow")

    def test_the_paths_are_read_when_asked_for_rather_than_at_import(
        self, permission_files
    ):
        """`conftest.WRITTEN_PATHS` redirects this module's bindings. A table
        built at import would hold the real paths through all of it."""
        assert entry._editable("permissions") == permission_files[0]

    def test_no_editor_is_reported_rather_than_raised(self, permission_files, monkeypatch):
        def missing(argv, **kwargs):
            raise entry.execute.NotInstalled("omarchy")

        monkeypatch.setattr(entry.execute, "run", missing)
        assert entry._edit("permissions", LOG) == 1

    def test_it_notifies_nobody(self, permission_files, spawned, notifications):
        entry._edit("permissions", LOG)
        assert notifications == []


class TestFlagsInTheReport:
    def test_a_shadowed_rule_is_flagged(self, permission_files, capsys):
        permission_files[0].write_text(
            doc(ask=["omarchy install *"], allow=["omarchy install app"])
        )
        assert entry._print_permissions(LOG) == 0
        out = capsys.readouterr().out
        assert "[shadowed]" in out
        assert "omarchy install *" in out

    def test_a_redundant_rule_is_flagged(self, permission_files, capsys):
        permission_files[0].write_text(
            doc(allow=["omarchy install *", "omarchy install app"])
        )
        entry._print_permissions(LOG)
        assert "[redundant]" in capsys.readouterr().out

    def test_a_shadowed_rule_does_not_stop_the_daemon(self, permission_files):
        permission_files[0].write_text(
            doc(deny=["omarchy install *"], allow=["omarchy install app"])
        )
        assert entry._load_permissions(LOG) is not None

    def test_check_names_it_and_still_exits_zero(self, permission_files, capsys):
        permission_files[0].write_text(
            doc(deny=["omarchy install *"], allow=["omarchy install app"])
        )
        assert entry._check_permissions(LOG) == 0
        assert "shadowed" in capsys.readouterr().out
