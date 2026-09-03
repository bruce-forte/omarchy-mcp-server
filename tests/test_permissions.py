"""The permissions document is the security boundary's other half, so this file
is its specification rather than a set of examples.

The rules that must never regress, in the order they matter:

- a command needing sudo is refused whatever the document says
- `deny` beats `ask` beats `allow`, and a narrower rule never reorders that
- a route whose argument is a command line can be asked about, never granted
- any defect at all refuses the document; there is no partial load
- a matcher that covers nothing loads and reads void, because a typo and an
  upstream rename are indistinguishable and only one of them is the user's fault
"""

from __future__ import annotations

import json

import pytest

from omarchy_mcp import permissions as perms
from omarchy_mcp.paths import PERMISSIONS_FILE, PERMISSIONS_LOCAL_FILE
from omarchy_mcp.permissions import (
    DEFAULT_GUARDED,
    Effect,
    Permissions,
    PermissionsError,
    Rule,
    check,
    errors,
    evaluate,
    load,
    parse,
)
from omarchy_mcp.permissions import NEVER_STORE
from omarchy_mcp.policy import Tier, base_tier


def doc(**lists) -> str:
    """A permissions file from bare matchers, so a test reads as its subject."""
    block = {
        effect: [{"kind": "route", "matcher": m} for m in matchers]
        for effect, matchers in lists.items()
        if effect != "guardedDefault"
    }
    if "guardedDefault" in lists:
        block["guardedDefault"] = lists["guardedDefault"]
    return json.dumps({"permissions": block})


def pooled(**lists) -> Permissions:
    rules, options = parse(doc(**lists), source="permissions.json")
    return Permissions(
        rules=rules,
        guarded_default=options.guarded_default or DEFAULT_GUARDED,
        guarded_default_source="permissions.json" if options.guarded_default else "",
    )


def rule(matcher: str, effect: Effect = Effect.ALLOW) -> Rule:
    return Rule(effect=effect, matcher=matcher, source="permissions.json", index=0)


class TestMatcherShape:
    """Two forms, and nothing else. A matcher we cannot interpret is intent we
    do not know, and guessing at it is what this rejects."""

    @pytest.mark.parametrize(
        "matcher",
        ["omarchy install app", "omarchy install *", "omarchy *", "omarchy migrate"],
    )
    def test_the_two_legal_forms_are_accepted(self, matcher):
        rules, _options = parse(doc(allow=[matcher]), source="f")
        assert rules[0].matcher == matcher

    @pytest.mark.parametrize(
        "matcher",
        [
            "omarchy install*",  # no space: upstream's `Bash(ls*)`, meaningless here
            "omarchy * remove",  # mid-position wildcard
            "*",  # no prefix at all
            "omarchy ** ",
            " omarchy install",
            "omarchy  install",
            "",
        ],
    )
    def test_everything_else_is_refused(self, matcher):
        with pytest.raises(PermissionsError):
            parse(doc(allow=[matcher]), source="f")

    def test_the_error_names_the_matcher_and_both_legal_forms(self):
        with pytest.raises(PermissionsError) as exc:
            parse(doc(allow=["omarchy install*"]), source="permissions.json")
        message = str(exc.value)
        assert "permissions.json" in message
        assert "omarchy install*" in message
        assert "omarchy install app" in message  # the exact form
        assert "omarchy install *" in message  # the prefix form

    def test_the_error_names_which_rule(self):
        with pytest.raises(PermissionsError) as exc:
            parse(doc(allow=["omarchy theme *", "omarchy install*"]), source="f")
        assert "allow.1.matcher" in str(exc.value)


class TestMatching:
    def test_an_exact_matcher_matches_only_itself(self):
        assert rule("omarchy install app").matches("omarchy install app")
        assert not rule("omarchy install app").matches("omarchy install app extra")
        assert not rule("omarchy install").matches("omarchy install app")

    def test_a_prefix_matches_everything_under_it(self):
        r = rule("omarchy install *")
        assert r.matches("omarchy install app")
        assert r.matches("omarchy install chromium google account")
        assert not r.matches("omarchy theme set")

    def test_a_prefix_also_matches_the_bare_route(self, commands):
        """19 routes are two tokens long, so `omarchy migrate` is both a group
        and a route. Without this a group matcher misses the bare route, which
        is usually the most dangerous member of the group."""
        assert "omarchy migrate" in commands
        assert rule("omarchy migrate *").matches("omarchy migrate")

    def test_a_prefix_does_not_match_a_longer_token(self):
        """`omarchy install *` must not match a hypothetical `omarchy installer`.
        The space is part of the rule."""
        assert not rule("omarchy install *").matches("omarchy installer")

    def test_a_matcher_expands_to_what_it_covers(self, commands):
        covered = rule("omarchy install *").covers(commands.values())
        assert "omarchy install app" in covered
        assert all(c.startswith("omarchy install") for c in covered)
        assert covered == tuple(sorted(covered))

    def test_group_matchers_are_prefix_matchers(self, commands):
        """`group` is exactly the second token of every route Omarchy ships,
        which is why the document has no group concept at all."""
        for cmd in commands.values():
            assert cmd.route.split()[1] == cmd.group
        covered = set(rule("omarchy install *").covers(commands.values()))
        assert covered == {c.route for c in commands.values() if c.group == "install"}


class TestPrecedence:
    def test_deny_beats_ask_beats_allow(self):
        p = pooled(
            deny=["omarchy dev link"], ask=["omarchy dev link"], allow=["omarchy dev link"]
        )
        assert evaluate("omarchy dev link", Tier.GUARDED, p).effect is Effect.DENY

    def test_ask_beats_allow(self):
        p = pooled(ask=["omarchy install *"], allow=["omarchy install app"])
        assert evaluate("omarchy install app", Tier.GUARDED, p).effect is Effect.ASK

    def test_specificity_does_not_reorder(self):
        """A narrow allow never carves an exception out of a broad deny. Same
        machinery as upstream, and weakening it for `ask` would weaken the
        reasoning for `deny`."""
        p = pooled(deny=["omarchy install *"], allow=["omarchy install app"])
        assert evaluate("omarchy install app", Tier.GUARDED, p).effect is Effect.DENY

    def test_the_outcome_names_the_rule_that_decided(self):
        """The panel must not offer an 'always' that a rule would shadow, and
        this is how it knows."""
        p = pooled(ask=["omarchy install *"])
        outcome = evaluate("omarchy install app", Tier.GUARDED, p)
        assert outcome.rule is not None
        assert outcome.rule.matcher == "omarchy install *"
        assert outcome.rule.source == "permissions.json"
        assert "omarchy install *" in outcome.reason


class TestTheLadder:
    def test_sudo_is_refused_however_generous_the_document(self, commands):
        sudo = next(c for c in commands.values() if c.requires_sudo)
        p = pooled(allow=["omarchy *"])
        outcome = evaluate(sudo.route, Tier.BLOCKED, p)
        assert outcome.effect is Effect.DENY
        assert outcome.rule is None, "no rule may be credited with a decision it did not make"
        assert "sudo" in outcome.reason

    def test_a_safe_route_runs_with_no_rules(self):
        assert evaluate("omarchy theme list", Tier.SAFE, Permissions()).effect is Effect.ALLOW

    def test_an_unmatched_guarded_route_takes_the_default(self):
        assert evaluate("omarchy install app", Tier.GUARDED, Permissions()).effect is Effect.ASK

    def test_the_default_is_ask(self):
        """A document that fills itself through use never fills if nothing is
        ever asked."""
        assert DEFAULT_GUARDED is Effect.ASK
        assert Permissions().guarded_default is Effect.ASK

    def test_guarded_default_deny_restores_the_old_floor(self):
        p = pooled(guardedDefault="deny")
        assert evaluate("omarchy install app", Tier.GUARDED, p).effect is Effect.DENY
        # ...and it is the *default*, not a veto: a rule still decides.
        p = pooled(guardedDefault="deny", allow=["omarchy install app"])
        assert evaluate("omarchy install app", Tier.GUARDED, p).effect is Effect.ALLOW

    def test_a_deny_rule_demotes_a_safe_route(self):
        p = pooled(deny=["omarchy theme list"])
        assert evaluate("omarchy theme list", Tier.SAFE, p).effect is Effect.DENY


class TestNeverStorable:
    """`omarchy update lock run <command> [args...]` runs whatever it is handed,
    so one standing grant on it is a standing grant on everything."""

    def test_the_set_names_routes_that_exist_and_are_guarded(self, commands):
        """Guards against a typo protecting nothing, and against the clamp
        silently applying to a safe route."""
        for route in NEVER_STORE:
            assert route in commands, f"{route} is no longer an Omarchy route"
            assert base_tier(commands[route]) is Tier.GUARDED

    @pytest.mark.parametrize("route", sorted(NEVER_STORE))
    def test_a_wildcard_allow_asks_rather_than_grants(self, route):
        p = pooled(allow=["omarchy *"])
        assert evaluate(route, Tier.GUARDED, p).effect is Effect.ASK

    @pytest.mark.parametrize("route", sorted(NEVER_STORE))
    def test_the_default_still_reaches_it(self, route):
        assert evaluate(route, Tier.GUARDED, Permissions()).effect is Effect.ASK

    @pytest.mark.parametrize("route", sorted(NEVER_STORE))
    def test_a_deny_still_denies_it(self, route):
        """The clamp sits after the rules, so agreeing with it is not an error."""
        p = pooled(deny=["omarchy *"])
        assert evaluate(route, Tier.GUARDED, p).effect is Effect.DENY

    @pytest.mark.parametrize("route", sorted(NEVER_STORE))
    def test_naming_it_exactly_in_allow_is_a_load_error(self, route, commands):
        found = check(pooled(allow=[route]), commands)
        assert [f.level for f in found] == ["error"]
        assert "command line" in found[0].reason


class TestCheck:
    def test_a_matcher_covering_nothing_is_void_not_an_error(self, commands):
        """Indistinguishable from a route that has not shipped yet. Refusing it
        would turn an upstream rename into a daemon that will not start."""
        found = check(pooled(deny=["omarchy instal *"]), commands)
        assert [f.level for f in found] == ["void"]
        assert errors(found) == ()

    def test_allowing_a_sudo_route_by_name_is_an_error(self, commands):
        sudo = next(c for c in commands.values() if c.requires_sudo)
        found = check(pooled(allow=[sudo.route]), commands)
        assert [f.level for f in found] == ["error"]
        assert "sudo" in found[0].reason

    def test_asking_about_a_sudo_route_by_name_is_an_error(self, commands):
        sudo = next(c for c in commands.values() if c.requires_sudo)
        assert errors(check(pooled(ask=[sudo.route]), commands))

    def test_denying_a_sudo_route_is_not_an_error(self, commands):
        """The user agreeing with the derivation. A redundant restriction is
        never wrong."""
        sudo = next(c for c in commands.values() if c.requires_sudo)
        assert check(pooled(deny=[sudo.route]), commands) == ()

    def test_a_wildcard_covering_a_sudo_route_is_not_an_error(self, commands):
        """`omarchy update *` is a reasonable thing to write, and several of its
        members need sudo. Only an exact matcher is an assertion about one
        route."""
        assert any(c.requires_sudo for c in commands.values() if c.group == "update")
        assert check(pooled(allow=["omarchy update *"]), commands) == ()

    def test_a_clean_document_has_no_findings(self, commands):
        assert check(pooled(allow=["omarchy theme *"], deny=["omarchy dev *"]), commands) == ()


class TestLoading:
    def test_no_file_at_all_is_valid(self, tmp_path):
        """The overwhelmingly common state, and a fresh install must work."""
        loaded = load([tmp_path / "permissions.json"])
        assert loaded.rules == ()
        assert loaded.guarded_default is DEFAULT_GUARDED

    def test_an_empty_permissions_object_is_valid(self, tmp_path):
        path = tmp_path / "permissions.json"
        path.write_text('{"permissions": {}}')
        assert load([path]).rules == ()

    def test_an_empty_document_is_valid(self, tmp_path):
        path = tmp_path / "permissions.json"
        path.write_text("{}")
        assert load([path]).rules == ()

    def test_a_schema_key_is_accepted(self, tmp_path):
        path = tmp_path / "permissions.json"
        path.write_text('{"$schema": "https://example/permissions.schema.json", "permissions": {}}')
        assert load([path]).rules == ()

    @pytest.mark.parametrize(
        "body",
        [
            "",
            "{",
            "[]",
            '{"permission": {}}',  # misspelled
            '{"permissions": {"allow": [{"kind": "route"}]}}',  # no matcher
            '{"permissions": {"allow": [{"matcher": "omarchy x"}]}}',  # no kind
            '{"permissions": {"allow": [{"kind": "tool", "matcher": "x"}]}}',  # unknown kind
            '{"permissions": {"allow": [{"kind": "route", "matcher": "x", "note": "hi"}]}}',
            '{"permissions": {"allow": ["omarchy theme set"]}}',  # a bare string
            '{"permissions": {"guardedDefault": "maybe"}}',
        ],
    )
    def test_any_defect_at_all_refuses_the_document(self, tmp_path, body):
        """No partial load. A file that parses halfway is a file whose intent is
        a guess, and ignoring a `deny` is a loss of protection."""
        path = tmp_path / "permissions.json"
        path.write_text(body)
        with pytest.raises(PermissionsError):
            load([path])

    def test_both_files_are_pooled(self, tmp_path):
        mine = tmp_path / "permissions.json"
        theirs = tmp_path / "permissions.local.json"
        mine.write_text(doc(deny=["omarchy dev *"]))
        theirs.write_text(doc(allow=["omarchy install app"]))

        loaded = load([mine, theirs])
        assert {r.matcher for r in loaded.rules} == {"omarchy dev *", "omarchy install app"}
        assert {r.source for r in loaded.rules} == {"permissions.json", "permissions.local.json"}

    def test_pooling_is_by_effect_not_by_file(self, tmp_path):
        """A daemon-written allow cannot outrank a hand-written ask, whichever
        file is read first."""
        mine = tmp_path / "permissions.json"
        theirs = tmp_path / "permissions.local.json"
        mine.write_text(doc(ask=["omarchy install *"]))
        theirs.write_text(doc(allow=["omarchy install app"]))

        loaded = load([mine, theirs])
        outcome = evaluate("omarchy install app", Tier.GUARDED, loaded)
        assert outcome.effect is Effect.ASK
        assert outcome.rule.source == "permissions.json"

    def test_the_users_own_rule_is_the_one_named(self, tmp_path):
        """Same decision in both files: the explainer quotes theirs, not the
        daemon's copy, because theirs is the one they can edit."""
        mine = tmp_path / "permissions.json"
        theirs = tmp_path / "permissions.local.json"
        mine.write_text(doc(allow=["omarchy install app"]))
        theirs.write_text(doc(allow=["omarchy install app"]))

        outcome = evaluate("omarchy install app", Tier.GUARDED, load([mine, theirs]))
        assert outcome.rule.source == "permissions.json"

    def test_guarded_default_may_be_set_in_one_file_only(self, tmp_path):
        """It decides one thing, so it belongs in one place."""
        mine = tmp_path / "permissions.json"
        theirs = tmp_path / "permissions.local.json"
        mine.write_text(doc(guardedDefault="deny"))
        theirs.write_text(doc(guardedDefault="ask"))

        with pytest.raises(PermissionsError) as exc:
            load([mine, theirs])
        assert "permissions.json" in str(exc.value)
        assert "permissions.local.json" in str(exc.value)

    def test_guarded_default_records_where_it_came_from(self, tmp_path):
        path = tmp_path / "permissions.json"
        path.write_text(doc(guardedDefault="deny"))
        loaded = load([path])
        assert loaded.guarded_default is Effect.DENY
        assert loaded.guarded_default_source == "permissions.json"

    def test_nobody_setting_it_leaves_no_source(self, tmp_path):
        path = tmp_path / "permissions.json"
        path.write_text(doc())
        assert load([path]).guarded_default_source == ""

    def test_the_error_names_the_file_it_came_from(self, tmp_path):
        path = tmp_path / "permissions.local.json"
        path.write_text('{"permissions": {"allow": [{"kind": "route", "matcher": "a*b"}]}}')
        with pytest.raises(PermissionsError) as exc:
            load([path])
        assert "permissions.local.json" in str(exc.value)


class TestPaths:
    def test_both_files_live_beside_the_config(self):
        """A person opening this folder sees everything that decides what an
        agent may do. Splitting the halves across two directories is how
        somebody reads half their permissions and believes it is all of them."""
        assert PERMISSIONS_FILE.parent == PERMISSIONS_LOCAL_FILE.parent
        assert PERMISSIONS_FILE.name == "permissions.json"
        assert PERMISSIONS_LOCAL_FILE.name == "permissions.local.json"

    def test_the_users_file_is_read_first(self):
        from omarchy_mcp.paths import PERMISSIONS_FILES

        assert PERMISSIONS_FILES == (PERMISSIONS_FILE, PERMISSIONS_LOCAL_FILE)


class TestPurity:
    """The module is handed paths and text. What to do about a document that
    will not load -- refuse to start, keep the last good one -- is the caller's,
    and the two answers are different."""

    def test_the_module_reads_nothing_by_itself(self):
        """Pure: it is handed paths and text, and holds no default of its own
        that would read the user's real file in a test."""
        assert not hasattr(perms, "PERMISSIONS_FILE")


class TestTheShippedExample:
    """`permissions.example.json` is the first thing anyone copies. A broken one
    teaches the wrong syntax and, since a defective document now stops the
    daemon, breaks their install on the way."""

    @pytest.fixture
    def example(self):
        import pathlib

        return pathlib.Path(__file__).resolve().parents[1] / "permissions.example.json"

    def test_it_loads(self, example):
        rules, options = parse(example.read_text(), source=example.name)
        assert rules, "an example with no rules teaches nothing"
        assert options.guarded_default is not None

    def test_every_rule_in_it_does_something(self, example, commands):
        rules, options = parse(example.read_text(), source=example.name)
        perms = Permissions(rules=rules, guarded_default=options.guarded_default)
        assert check(perms, commands) == (), "the example must have no void or error rules"

    def test_it_demonstrates_all_three_lists(self, example):
        rules, _ = parse(example.read_text(), source=example.name)
        assert {r.effect for r in rules} == set(Effect)

    def test_it_points_at_the_schema(self, example):
        assert '"$schema"' in example.read_text()
