"""The permissions document is the security boundary's other half, so this file
is its specification rather than a set of examples.

The rules that must never regress, in the order they matter:

- a command needing sudo is refused whatever the document says
- `deny` beats `ask` beats `allow`, and a narrower rule never reorders that
- a route whose argument is a command line can be asked about, never granted
- any defect at all refuses the document; there is no partial load
- a matcher that covers nothing loads and reads void, because a typo and an
  upstream rename are indistinguishable and only one of them is the user's fault
- a rule that never decides anything says so, and names the rule that got there
  first, because "you did not grant what you think you granted" is silent
  otherwise
- the panel's removal takes grants and nothing else, so no button on it widens
"""

from __future__ import annotations

import json

import pytest

from omarchy_mcp import permissions as perms
from omarchy_mcp.paths import PERMISSIONS_FILE, PERMISSIONS_LOCAL_FILE
from omarchy_mcp.permissions import (
    COVERED_SHOWN,
    DEFAULT_GUARDED,
    Effect,
    Permissions,
    PermissionsError,
    Rule,
    check,
    describe,
    errors,
    evaluate,
    GrantRefused,
    explain,
    grant,
    load,
    prunable,
    prune,
    parse,
    revoke,
    RevokeRefused,
    seed,
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

    def test_a_prefix_does_not_match_a_longer_token(self, commands):
        """The space is part of the rule, and this is not hypothetical.

        Omarchy ships `omarchy installed service dropbox` alongside
        `omarchy install app`. Upstream's `Bash(ls*)`-matches-`lsof` behaviour
        would have quietly folded the `installed` routes into every
        `omarchy install *` rule anybody wrote."""
        assert not rule("omarchy install *").matches("omarchy installer")
        assert "omarchy installed service dropbox" in commands
        assert not rule("omarchy install *").matches("omarchy installed service dropbox")
        assert rule("omarchy installed *").matches("omarchy installed service dropbox")

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
        assert outcome.rule is not None and outcome.rule.source == "permissions.json"

    def test_the_users_own_rule_is_the_one_named(self, tmp_path):
        """Same decision in both files: the explainer quotes theirs, not the
        daemon's copy, because theirs is the one they can edit."""
        mine = tmp_path / "permissions.json"
        theirs = tmp_path / "permissions.local.json"
        mine.write_text(doc(allow=["omarchy install app"]))
        theirs.write_text(doc(allow=["omarchy install app"]))

        outcome = evaluate("omarchy install app", Tier.GUARDED, load([mine, theirs]))
        assert outcome.rule is not None and outcome.rule.source == "permissions.json"

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
        assert options.guarded_default is not None
        perms = Permissions(rules=rules, guarded_default=options.guarded_default)
        assert check(perms, commands) == (), "the example must have no void or error rules"

    def test_it_demonstrates_all_three_lists(self, example):
        rules, _ = parse(example.read_text(), source=example.name)
        assert {r.effect for r in rules} == set(Effect)

    def test_it_points_at_the_schema(self, example):
        assert '"$schema"' in example.read_text()


class TestTheExplainer:
    """"Why can the agent do this?" is the question a person opens this to ask,
    and an answer that cannot name the rule and the file is not one."""

    def test_a_rule_that_decided_is_named_with_its_file(self, commands):
        row = describe(commands["omarchy install app"], pooled(allow=["omarchy install *"]))
        assert row["rule"] == "omarchy install *"
        assert row["source"] == "permissions.json"

    def test_nothing_is_named_when_nothing_matched(self, commands):
        """No rule may be credited with a decision it did not make."""
        row = describe(commands["omarchy install app"], Permissions())
        assert "rule" not in row and "source" not in row

    def test_a_rule_is_shown_as_what_it_covers(self, commands):
        report = explain(pooled(allow=["omarchy install *"]), commands)
        rule = report["rules"][0]
        expected = {
            c.route
            for c in commands.values()
            if c.route == "omarchy install" or c.route.startswith("omarchy install ")
        }
        assert rule["covers"] == len(expected)
        assert rule["routes"], "a rule with no expansion teaches nothing"
        # The listing is what a person checks the rule against, so it must not
        # quietly include the neighbouring `omarchy installed ...` routes.
        assert not any(r.startswith("omarchy installed") for r in rule["routes"])

    def test_a_long_expansion_is_cut_but_counted_exactly(self, commands):
        report = explain(pooled(allow=["omarchy *"]), commands)
        rule = report["rules"][0]
        assert rule["covers"] == len(commands), "the count is never approximate"
        assert len(rule["routes"]) == COVERED_SHOWN
        assert rule["more"] == len(commands) - COVERED_SHOWN

    def test_rules_come_back_in_precedence_order(self, commands):
        report = explain(
            pooled(allow=["omarchy theme *"], deny=["omarchy dev *"], ask=["omarchy install *"]),
            commands,
        )
        assert [r["effect"] for r in report["rules"]] == ["deny", "ask", "allow"]
        assert report["precedence"] == ["deny", "ask", "allow"]

    def test_a_dead_rule_says_so_here(self, commands):
        report = explain(pooled(deny=["omarchy nosuchthing *"]), commands)
        assert "void" in report["rules"][0]
        assert report["rules"][0]["covers"] == 0

    def test_sudo_routes_are_counted_not_listed(self, commands):
        """Over a hundred of them, refused whatever the document says. Listing
        them would bury the routes it actually governs."""
        report = explain(Permissions(), commands)
        listed = {r["route"] for r in report["routes"]}
        sudo = {c.route for c in commands.values() if c.requires_sudo}
        assert not (listed & sudo)
        assert report["counts"]["byEffect"]["deny"] >= len(sudo)

    def test_a_safe_route_nothing_touches_is_omitted(self, commands):
        report = explain(Permissions(), commands)
        listed = {r["route"] for r in report["routes"]}
        assert "omarchy theme list" not in listed

    def test_a_safe_route_a_rule_touches_is_listed(self, commands):
        report = explain(pooled(deny=["omarchy theme list"]), commands)
        row = next(r for r in report["routes"] if r["route"] == "omarchy theme list")
        assert row["effect"] == "deny"
        assert row["rule"] == "omarchy theme list"

    def test_every_guarded_route_is_accounted_for(self, commands):
        report = explain(Permissions(), commands)
        listed = {r["route"] for r in report["routes"]}
        guarded = {c.route for c in commands.values() if base_tier(c) is Tier.GUARDED}
        assert guarded <= listed, "a guarded route is exactly what this document governs"

    def test_the_never_granted_route_is_flagged_where_it_appears(self, commands):
        report = explain(pooled(allow=["omarchy *"]), commands)
        row = next(r for r in report["routes"] if r["route"] in NEVER_STORE)
        assert row["neverGranted"] is True
        assert row["effect"] == "ask", "an allow cannot grant it"
        assert report["neverGranted"] == sorted(NEVER_STORE)

    def test_the_counts_add_up(self, commands):
        report = explain(pooled(deny=["omarchy dev *"]), commands)
        assert sum(report["counts"]["byEffect"].values()) == len(commands)
        assert report["counts"]["commands"] == len(commands)
        assert report["counts"]["listed"] == len(report["routes"])

    def test_where_the_default_came_from_is_said(self, commands):
        assert "built-in" in explain(Permissions(), commands)["guardedDefaultSource"]
        report = explain(pooled(guardedDefault="deny"), commands)
        assert report["guardedDefaultSource"] == "permissions.json"
        assert report["guardedDefault"] == "deny"

    def test_it_serialises(self, commands):
        """It is published as JSON by a resource and by the CLI."""
        json.dumps(explain(pooled(allow=["omarchy theme *"]), commands))


class TestGranting:
    """What the daemon writes when somebody answers *always* at the desk.

    Every invariant is checked here rather than only where the button was drawn.
    A surface that hides the button is a UI; this is the security artifact, and
    the two are allowed to disagree only in the safe direction.
    """

    @pytest.fixture
    def local(self, tmp_path):
        return tmp_path / "permissions.local.json"

    def test_it_writes_an_exact_route_and_nothing_wider(self, local, commands):
        rule = grant(local, "omarchy install app", Permissions(), commands)
        assert rule.matcher == "omarchy install app"
        assert not rule.wild, "a click consents to what was on the screen"
        assert rule.effect is Effect.ALLOW

    def test_clicking_the_same_prefix_never_collapses_to_a_wildcard(self, local, commands):
        """Nine clicks under `omarchy install` stay nine exact rules. Inferring
        a prefix would grant routes nobody was shown."""
        perms = Permissions()
        for route in ("omarchy install app", "omarchy install font", "omarchy install browser"):
            grant(local, route, perms, commands)
            perms = load((local.with_name("permissions.json"), local))
        assert all(not r.wild for r in perms.rules)
        assert {r.matcher for r in perms.rules} == {
            "omarchy install app",
            "omarchy install font",
            "omarchy install browser",
        }

    def test_what_it_writes_loads_back(self, local, commands):
        """The daemon's own file must not be one the daemon then refuses to
        start on."""
        grant(local, "omarchy install app", Permissions(), commands)
        reloaded = load((local.with_name("permissions.json"), local))
        assert [r.matcher for r in reloaded.rules] == ["omarchy install app"]
        assert reloaded.rules[0].source == "permissions.local.json"
        assert check(reloaded, commands) == ()

    def test_the_file_is_not_world_readable(self, local, commands):
        grant(local, "omarchy install app", Permissions(), commands)
        assert local.stat().st_mode & 0o077 == 0
        assert local.parent.stat().st_mode & 0o077 == 0

    def test_it_leaves_no_temporary_file_behind(self, local, commands):
        grant(local, "omarchy install app", Permissions(), commands)
        assert [p.name for p in local.parent.iterdir()] == [local.name]

    def test_a_deny_in_the_pool_refuses_the_grant(self, local, commands):
        """The user's own file grew a matching deny while the prompt was up.
        That deny is newer than the question."""
        perms = pooled(deny=["omarchy install *"])
        with pytest.raises(GrantRefused) as exc:
            grant(local, "omarchy install app", perms, commands)
        assert "omarchy install *" in str(exc.value)
        assert not local.exists(), "nothing is written on a refusal"

    def test_an_ask_in_the_pool_refuses_the_grant(self, local, commands):
        """An allow shadowed by a broader ask is a rule with no effect, and
        writing one would tell the user they had done something they had not."""
        with pytest.raises(GrantRefused):
            grant(local, "omarchy install app", pooled(ask=["omarchy install *"]), commands)

    def test_a_sudo_route_is_never_granted(self, local, commands):
        sudo = next(c for c in commands.values() if c.requires_sudo)
        with pytest.raises(GrantRefused) as exc:
            grant(local, sudo.route, Permissions(), commands)
        assert "sudo" in str(exc.value)

    @pytest.mark.parametrize("route", sorted(NEVER_STORE))
    def test_a_never_granted_route_is_never_granted(self, local, route, commands):
        with pytest.raises(GrantRefused) as exc:
            grant(local, route, Permissions(), commands)
        assert "command line" in str(exc.value)

    def test_a_route_that_does_not_exist_is_refused(self, local, commands):
        with pytest.raises(GrantRefused):
            grant(local, "omarchy nosuchthing", Permissions(), commands)

    def test_granting_twice_is_refused_rather_than_duplicated(self, local, commands):
        grant(local, "omarchy install app", Permissions(), commands)
        perms = load((local.with_name("permissions.json"), local))
        with pytest.raises(GrantRefused) as exc:
            grant(local, "omarchy install app", perms, commands)
        assert "already allowed" in str(exc.value)

    def test_it_keeps_rules_it_did_not_write(self, local, commands):
        """The file is replaced, not appended to, so anything already in it has
        to survive the round trip -- including a hand-added one."""
        local.write_text(doc(allow=["omarchy theme set"], deny=["omarchy dev link"]))
        grant(local, "omarchy install app", Permissions(), commands)
        reloaded = load((local.with_name("permissions.json"), local))
        assert {(r.effect, r.matcher) for r in reloaded.rules} == {
            (Effect.DENY, "omarchy dev link"),
            (Effect.ALLOW, "omarchy theme set"),
            (Effect.ALLOW, "omarchy install app"),
        }

    def test_it_keeps_a_setting_it_did_not_write(self, local, commands):
        local.write_text(json.dumps({"permissions": {"askTimeoutSeconds": 120}}))
        grant(local, "omarchy install app", Permissions(), commands)
        assert load((local.with_name("permissions.json"), local)).ask_timeout_s == 120


class TestPruning:
    """N10 deliberately never prunes on its own: a rule whose route vanished is
    the delta's evidence, and a daemon that quietly edits a file is one the user
    cannot reason about. This is the other half — user-initiated, after being
    shown exactly what would go."""

    @pytest.fixture
    def local(self, tmp_path):
        return tmp_path / "permissions.local.json"

    def test_it_finds_rules_that_match_nothing(self, local, commands):
        local.write_text(doc(allow=["omarchy install app", "omarchy gone away"]))
        assert [r.matcher for r in prunable(local, commands)] == ["omarchy gone away"]

    def test_a_live_rule_is_never_prunable(self, local, commands):
        local.write_text(doc(allow=["omarchy install *"]))
        assert prunable(local, commands) == ()

    def test_pruning_removes_exactly_what_was_offered(self, local, commands):
        local.write_text(doc(allow=["omarchy install app", "omarchy gone away"]))
        offered = prunable(local, commands)
        gone = prune(local, commands)

        assert [r.matcher for r in gone] == [r.matcher for r in offered]
        left = load((local.with_name("permissions.json"), local))
        assert [r.matcher for r in left.rules] == ["omarchy install app"]

    def test_it_prunes_a_dead_deny_too(self, local, commands):
        """Somebody hand-added it, and it has stopped matching. It grants
        nothing either way, but it is worth seeing before it is tidied."""
        local.write_text(doc(deny=["omarchy vanished *"]))
        assert [r.effect for r in prunable(local, commands)] == [Effect.DENY]
        assert prune(local, commands)

    def test_it_is_idempotent(self, local, commands):
        local.write_text(doc(allow=["omarchy gone away"]))
        assert prune(local, commands)
        assert prune(local, commands) == ()

    def test_it_keeps_settings_it_did_not_write(self, local, commands):
        local.write_text(
            json.dumps(
                {
                    "permissions": {
                        "askTimeoutSeconds": 120,
                        "allow": [{"kind": "route", "matcher": "omarchy gone away"}],
                    }
                }
            )
        )
        prune(local, commands)
        assert load((local.with_name("permissions.json"), local)).ask_timeout_s == 120

    def test_what_it_leaves_still_loads(self, local, commands):
        local.write_text(doc(allow=["omarchy install app", "omarchy gone away"]))
        prune(local, commands)
        reloaded = load((local.with_name("permissions.json"), local))
        assert check(reloaded, commands) == ()

    def test_a_missing_file_prunes_nothing(self, local, commands):
        assert prunable(local, commands) == ()
        assert prune(local, commands) == ()

    def test_a_file_that_will_not_parse_is_not_one_to_offer_to_edit(self, local, commands):
        local.write_text("{")
        assert prunable(local, commands) == ()

    def test_nothing_is_written_when_nothing_is_dead(self, local, commands):
        local.write_text(doc(allow=["omarchy install app"]))
        before = local.read_text()
        assert prune(local, commands) == ()
        assert local.read_text() == before


class TestInertRules:
    """A rule can load, match commands, and still decide nothing. N15: the one
    defect nothing reported, and the reason it is a finding rather than a
    notification is that it can only ever fail safe."""

    def test_an_allow_under_a_broader_ask_is_shadowed(self, commands):
        found = check(
            pooled(ask=["omarchy install *"], allow=["omarchy install app"]), commands
        )
        assert [f.level for f in found] == ["shadowed"]
        assert errors(found) == (), "it decides nothing; it does not stop the daemon"

    def test_shadowed_names_the_rule_that_got_there_first(self, commands):
        """The two fixes are 'delete this' and 'narrow that', and neither can be
        chosen without knowing which rule is above."""
        found = check(
            pooled(deny=["omarchy install *"], allow=["omarchy install app"]), commands
        )
        assert found[0].by is not None
        assert found[0].by.matcher == "omarchy install *"
        assert found[0].by.effect is Effect.DENY
        assert "omarchy install *" in found[0].reason

    def test_the_same_effect_twice_is_redundant_rather_than_shadowed(self, commands):
        found = check(
            pooled(allow=["omarchy install *", "omarchy install app"]), commands
        )
        assert [f.level for f in found] == ["redundant"]
        assert found[0].by is not None and found[0].by.matcher == "omarchy install *"

    def test_partial_coverage_is_not_a_defect(self, commands):
        """`allow omarchy theme *` under a `deny omarchy theme set` is a good
        pair. Flagging it would make prefixes unwritable."""
        assert check(
            pooled(deny=["omarchy theme set"], allow=["omarchy theme *"]), commands
        ) == ()

    def test_a_deny_can_never_be_shadowed(self, commands):
        """`deny` is first in PRECEDENCE, so this only ever fires on ask and
        allow -- which is why it fails safe and never notifies."""
        found = check(
            pooled(deny=["omarchy install app"], ask=["omarchy install *"]), commands
        )
        assert all(f.rule.effect is not Effect.DENY for f in found)

    def test_precedence_is_what_counts_not_file_order(self, commands):
        """The allow is written first in the document and still shadowed: the
        pool is ordered deny, ask, allow before anything is compared."""
        found = check(
            pooled(allow=["omarchy install app"], deny=["omarchy install *"]), commands
        )
        assert [f.level for f in found] == ["shadowed"]

    def test_a_dead_rule_is_void_rather_than_inert(self, commands):
        """One finding per rule. Two lines about one broken sentence, saying
        different things, is worse than either alone."""
        found = check(
            pooled(allow=["omarchy install *", "omarchy gone away"]), commands
        )
        assert [f.level for f in found] == ["void"]

    def test_an_error_is_not_also_reported_inert(self, commands):
        sudo = next(c for c in commands.values() if c.requires_sudo)
        found = check(pooled(allow=["omarchy *", sudo.route]), commands)
        assert [f.level for f in found] == ["error"]

    def test_the_explainer_carries_the_rule_that_decided(self, commands):
        report = explain(
            pooled(ask=["omarchy install *"], allow=["omarchy install app"]), commands
        )
        row = next(r for r in report["rules"] if r["matcher"] == "omarchy install app")
        assert "shadowed" in row
        assert row["by"] == "omarchy install *"
        assert row["bySource"] == "permissions.json"


class TestRevoking:
    """Removing a grant from the panel. The property under test is one sentence:
    no button on that panel can widen what an agent may do."""

    @pytest.fixture
    def local(self, tmp_path):
        return tmp_path / "permissions.local.json"

    def test_it_removes_a_grant(self, local, commands):
        local.write_text(doc(allow=["omarchy install app", "omarchy theme set"]))
        gone = revoke(local, Effect.ALLOW, "omarchy install app")

        assert [r.matcher for r in gone] == ["omarchy install app"]
        left = load((local.with_name("permissions.json"), local))
        assert [r.matcher for r in left.rules] == ["omarchy theme set"]

    def test_a_deny_is_never_removed_here(self, local):
        """A restriction is a decision, and it is taken back in an editor."""
        local.write_text(doc(deny=["omarchy install app"]))
        before = local.read_text()
        with pytest.raises(RevokeRefused):
            revoke(local, Effect.DENY, "omarchy install app")
        assert local.read_text() == before

    def test_an_ask_is_never_removed_here(self, local):
        local.write_text(doc(ask=["omarchy install app"]))
        with pytest.raises(RevokeRefused):
            revoke(local, Effect.ASK, "omarchy install app")

    def test_a_deny_with_the_same_matcher_survives_the_grant_going(self, local):
        """The matcher names a rule together with its effect, never on its own."""
        local.write_text(doc(allow=["omarchy install app"], deny=["omarchy install app"]))
        revoke(local, Effect.ALLOW, "omarchy install app")
        left = load((local.with_name("permissions.json"), local))
        assert [(r.effect, r.matcher) for r in left.rules] == [
            (Effect.DENY, "omarchy install app")
        ]

    def test_it_takes_every_copy(self, local):
        """Nothing dedupes rules, and identical entries are indistinguishable in
        the display. Removing one of a pair is a button that visibly does
        nothing."""
        local.write_text(doc(allow=["omarchy install app", "omarchy install app"]))
        assert len(revoke(local, Effect.ALLOW, "omarchy install app")) == 2
        assert load((local.with_name("permissions.json"), local)).rules == ()

    def test_naming_a_rule_that_is_gone_is_not_an_error(self, local):
        """Somebody already removed it, which is the outcome that was wanted."""
        local.write_text(doc(allow=["omarchy theme set"]))
        before = local.read_text()
        assert revoke(local, Effect.ALLOW, "omarchy install app") == ()
        assert local.read_text() == before, "nothing is rewritten for a no-op"

    def test_it_is_idempotent(self, local):
        local.write_text(doc(allow=["omarchy install app"]))
        assert revoke(local, Effect.ALLOW, "omarchy install app")
        assert revoke(local, Effect.ALLOW, "omarchy install app") == ()

    def test_a_missing_file_revokes_nothing(self, local):
        assert revoke(local, Effect.ALLOW, "omarchy install app") == ()
        assert not local.exists()

    def test_a_file_that_will_not_parse_is_not_edited_blind(self, local):
        local.write_text("{")
        with pytest.raises(PermissionsError):
            revoke(local, Effect.ALLOW, "omarchy install app")

    def test_it_keeps_settings_and_rules_it_did_not_write(self, local):
        local.write_text(
            json.dumps(
                {
                    "permissions": {
                        "askTimeoutSeconds": 120,
                        "deny": [{"kind": "route", "matcher": "omarchy dev *"}],
                        "allow": [{"kind": "route", "matcher": "omarchy install app"}],
                    }
                }
            )
        )
        revoke(local, Effect.ALLOW, "omarchy install app")
        left = load((local.with_name("permissions.json"), local))
        assert left.ask_timeout_s == 120
        assert [r.matcher for r in left.rules] == ["omarchy dev *"]

    def test_what_it_leaves_still_loads(self, local, commands):
        local.write_text(doc(allow=["omarchy install app", "omarchy theme set"]))
        revoke(local, Effect.ALLOW, "omarchy install app")
        reloaded = load((local.with_name("permissions.json"), local))
        assert check(reloaded, commands) == ()


class TestSeeding:
    """The Edit button may create a file that is not there. It may not change
    one that is."""

    def test_it_creates_a_document_that_loads(self, tmp_path):
        path = tmp_path / "permissions.json"
        assert seed(path) is True
        assert load([path]).rules == ()

    def test_it_carries_the_schema_so_an_editor_validates_the_first_rule(self, tmp_path):
        path = tmp_path / "permissions.json"
        seed(path)
        body = json.loads(path.read_text())
        assert body["$schema"] == perms.SCHEMA_URL
        assert sorted(body["permissions"]) == ["allow", "ask", "deny"]

    def test_it_never_overwrites(self, tmp_path):
        """Property of the syscall, not of a check that could race an editor."""
        path = tmp_path / "permissions.json"
        path.write_text(doc(allow=["omarchy theme set"]))
        before = path.read_text()
        assert seed(path) is False
        assert path.read_text() == before

    def test_it_is_readable_only_by_its_owner(self, tmp_path):
        path = tmp_path / "permissions.json"
        seed(path)
        assert path.stat().st_mode & 0o777 == 0o600

    def test_it_creates_the_directory_when_there_is_none(self, tmp_path):
        path = tmp_path / "fresh" / "permissions.json"
        assert seed(path)
        assert path.exists()
