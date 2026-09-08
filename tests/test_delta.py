"""What changed under the rules, and what that does to the next call.

The rules that must never regress:

- restrictions extend forward to new routes; grants do not
- the snapshot advances only on acknowledgement, never on startup -- except the
  very first run, which has nothing to compare against
- a fingerprint would not do: every question here needs the previous route set
"""

from __future__ import annotations

import dataclasses
import json

import pytest

from omarchy_mcp import delta
from omarchy_mcp.permissions import Effect, Permissions, Rule, evaluate
from omarchy_mcp.policy import base_tier
from omarchy_mcp.registry import Command


def rules(*specs: tuple[Effect, str]) -> Permissions:
    return Permissions(
        rules=tuple(
            Rule(effect=effect, matcher=matcher, source="permissions.json", index=i)
            for i, (effect, matcher) in enumerate(specs)
        )
    )


def without(commands, prefix: str) -> frozenset[str]:
    """A snapshot from before some routes existed."""
    return frozenset(r for r in commands if not r.startswith(prefix))


def _arrival(route: str) -> Command:
    """A command in a group the committed fixture does not contain.

    Built rather than taken from the fixture on purpose: an unclassified group
    is by definition one nobody has put in a list, and every group in the
    fixture is in one (`test_policy.test_every_group_omarchy_ships_is_classified`
    is the gate that keeps it that way).
    """
    return Command(
        route=route,
        binary="omarchy",
        group=route.split()[1],
        summary="",
        args="",
        examples=(),
        requires_sudo=False,
        hidden=False,
    )


class TestTheSnapshot:
    def test_a_first_run_reports_nothing(self, tmp_path, commands):
        """Every route is new on a fresh install. That is the catalogue this
        feature exists to avoid, not the delta."""
        review = delta.compute(delta.load_seen(tmp_path / "missing.json"), commands, Permissions())
        assert review.first_run
        assert not review
        assert review.arrivals == ()

    def test_it_round_trips(self, tmp_path, commands):
        path = tmp_path / "registry-seen.json"
        delta.save_seen(path, commands)
        assert delta.load_seen(path) == frozenset(commands)

    def test_a_snapshot_that_will_not_parse_is_one_we_do_not_have(self, tmp_path, commands):
        """An observation of the machine, not a decision of the user's. Losing
        it costs one silent re-baseline; refusing to start over it would be
        absurd."""
        path = tmp_path / "registry-seen.json"
        path.write_text("{ not json")
        assert delta.load_seen(path) is None

    def test_saving_is_atomic(self, tmp_path, commands):
        path = tmp_path / "registry-seen.json"
        delta.save_seen(path, commands)
        assert [p.name for p in tmp_path.iterdir()] == [path.name]

    def test_the_route_list_is_kept_not_a_fingerprint(self, tmp_path, commands):
        """A hash says something changed and cannot say what."""
        path = tmp_path / "registry-seen.json"
        delta.save_seen(path, commands)
        body = json.loads(path.read_text())
        assert sorted(body["routes"]) == sorted(commands)


class TestRestrictionsExtendForwardAndGrantsDoNot:
    @pytest.mark.parametrize(
        "effect,expected",
        [(Effect.DENY, "deny"), (Effect.ASK, "ask")],
    )
    def test_a_restriction_covers_a_new_route_on_arrival(self, commands, effect, expected):
        seen = without(commands, "omarchy install ")
        review = delta.compute(seen, commands, rules((effect, "omarchy install *")))
        arrival = next(a for a in review.arrivals if a.route == "omarchy install app")
        assert arrival.effect == expected
        assert not arrival.quarantined, "protection propagating is the point"

    def test_a_grant_does_not(self, commands):
        seen = without(commands, "omarchy install ")
        review = delta.compute(seen, commands, rules((Effect.ALLOW, "omarchy install *")))
        arrival = next(a for a in review.arrivals if a.route == "omarchy install app")
        assert arrival.quarantined
        assert arrival.effect == "ask"
        assert arrival.rule == "omarchy install *"

    def test_the_quarantine_is_what_the_gate_reads(self, commands):
        """`evaluate` is where it takes effect; the review only names the set."""
        seen = without(commands, "omarchy install ")
        perms = rules((Effect.ALLOW, "omarchy install *"))
        review = delta.compute(seen, commands, perms)
        cmd = commands["omarchy install app"]

        assert evaluate(cmd.route, base_tier(cmd), perms).effect is Effect.ALLOW
        held = evaluate(cmd.route, base_tier(cmd), perms, unreviewed=review.quarantined)
        assert held.effect is Effect.ASK
        assert "new since you last reviewed" in held.reason

    def test_acknowledging_releases_it(self, commands):
        """Once the snapshot moves, the route is no longer new and the rule the
        user wrote applies as written."""
        perms = rules((Effect.ALLOW, "omarchy install *"))
        after = delta.compute(frozenset(commands), commands, perms)
        cmd = commands["omarchy install app"]
        assert after.quarantined == frozenset()
        assert evaluate(cmd.route, base_tier(cmd), perms, unreviewed=after.quarantined).allowed

    def test_a_sudo_route_is_never_quarantined(self, commands):
        """It is refused either way; calling it "held" would suggest a review
        could release it."""
        sudo = next(c for c in commands.values() if c.requires_sudo)
        seen = frozenset(commands) - {sudo.route}
        review = delta.compute(seen, commands, rules((Effect.ALLOW, "omarchy *")))
        arrival = next(a for a in review.arrivals if a.route == sudo.route)
        assert not arrival.quarantined
        assert arrival.effect == "deny"


class TestWidening:
    def test_a_rule_that_gained_routes_is_reported(self, commands):
        """You reviewed fifteen and consented to fifteen; an update put three
        more inside the same sentence."""
        gained = ["omarchy install app", "omarchy install font"]
        seen = frozenset(commands) - set(gained)
        review = delta.compute(seen, commands, rules((Effect.ALLOW, "omarchy install *")))

        assert len(review.widened) == 1
        assert review.widened[0].matcher == "omarchy install *"
        assert set(review.widened[0].routes) == set(gained)
        assert review.urgent, "a rule widening without being edited has to be seen"

    def test_a_rule_that_matched_nothing_before_is_not_widening(self, commands):
        """It started working. Its routes are already in `arrivals`, and
        reporting them twice would read as two problems."""
        seen = without(commands, "omarchy install")
        review = delta.compute(seen, commands, rules((Effect.ALLOW, "omarchy install *")))
        assert review.widened == ()
        assert review.arrivals

    def test_an_unchanged_registry_reports_nothing(self, commands):
        review = delta.compute(frozenset(commands), commands, rules((Effect.ALLOW, "omarchy *")))
        assert not review
        assert review.headline == "nothing to review"


class TestDeadRules:
    def test_a_rule_that_stopped_matching_is_reported(self, commands):
        """A `deny` that stopped matching looks exactly like one that works."""
        seen = frozenset(commands) | {"omarchy gone away"}
        review = delta.compute(seen, commands, rules((Effect.DENY, "omarchy gone *")))

        assert len(review.dead) == 1
        assert review.dead[0].matcher == "omarchy gone *"
        assert review.dead[0].effect == "deny"
        assert review.gone == ("omarchy gone away",)

    def test_a_rule_that_never_matched_is_not_newly_dead(self, commands):
        review = delta.compute(
            frozenset(commands), commands, rules((Effect.DENY, "omarchy nope *"))
        )
        assert review.dead == ()


class TestAnUnclassifiedGroup:
    """A group in neither `GUARDED_GROUPS` nor `SAFE_GROUPS`.

    This used to be a question about the snapshot -- is the group *new* since
    the last acknowledgement -- because an unclassified group derived `safe` and
    ran, so "new" was the only signal anything had happened. `SAFE_GROUPS` made
    it a static property: unclassified is guarded whether it arrived today or
    has been sitting unclassified for a year. The review still reports it,
    because only a person can decide which list it belongs in.
    """

    def test_a_group_nobody_classified_is_flagged_and_urgent(self, commands):
        arrived = dict(commands)
        arrived["omarchy backup wipe"] = _arrival("omarchy backup wipe")
        review = delta.compute(frozenset(commands), arrived, Permissions())
        flagged = [a for a in review.arrivals if a.unclassified]

        assert [a.route for a in flagged] == ["omarchy backup wipe"]
        assert all(a.effect == "ask" for a in flagged), "guarded, so nothing runs"
        assert all(a.tier == "guarded" for a in flagged)
        assert review.urgent

    def test_a_deny_rule_still_beats_it(self, commands):
        """Restrictions extend forward. Guarded is the floor, not the ceiling."""
        arrived = dict(commands)
        arrived["omarchy backup wipe"] = _arrival("omarchy backup wipe")
        review = delta.compute(
            frozenset(commands), arrived, rules((Effect.DENY, "omarchy backup *"))
        )
        (new,) = [a for a in review.arrivals if a.route == "omarchy backup wipe"]
        assert new.effect == "deny"
        assert new.unclassified, "still worth reporting: the rule is the user's, not ours"

    def test_a_new_route_in_a_classified_group_is_not_flagged(self, commands):
        """`theme` is named in `SAFE_GROUPS`, so a new route in it is somebody's
        decision already -- one new route, not one new group."""
        seen = frozenset(commands) - {"omarchy theme list"}
        review = delta.compute(seen, commands, Permissions())
        assert [a.unclassified for a in review.arrivals] == [False]
        assert not review.urgent

    def test_a_whole_classified_group_arriving_is_not_flagged(self, commands):
        """Every route in it is new and none of it is unclassified: the
        classification is what this asks about, not the snapshot."""
        review = delta.compute(without(commands, "omarchy theme"), commands, Permissions())
        assert review.arrivals
        assert not any(a.unclassified for a in review.arrivals)

    def test_a_handful_of_new_guarded_routes_is_not_urgent(self, commands):
        """They ask anyway. That can wait for the next time the panel opens."""
        seen = frozenset(commands) - {"omarchy install app"}
        review = delta.compute(seen, commands, Permissions())
        assert review
        assert not review.urgent


class TestTheToken:
    def test_one_is_minted_when_there_is_something_to_acknowledge(self, commands):
        seen = frozenset(commands) - {"omarchy install app"}
        assert delta.compute(seen, commands, Permissions()).token

    def test_none_is_minted_when_there_is_not(self, commands):
        assert delta.compute(frozenset(commands), commands, Permissions()).token == ""
        assert delta.compute(None, commands, Permissions()).token == ""

    def test_it_is_not_in_the_published_report(self, commands):
        """A report anybody can ask for must not hand out the ability to consume
        a warning. The token goes on the frame the shell reads and nowhere else."""
        seen = frozenset(commands) - {"omarchy install app"}
        review = delta.compute(seen, commands, Permissions())
        assert review.token
        assert review.token not in json.dumps(delta.as_dict(review))

    def test_two_reviews_do_not_share_one(self, commands):
        seen = frozenset(commands) - {"omarchy install app"}
        first = delta.compute(seen, commands, Permissions())
        second = delta.compute(seen, commands, Permissions())
        assert first.token != second.token
        # ...and the token is not what makes two reviews equal.
        assert dataclasses.replace(first, token="") == dataclasses.replace(second, token="")


class TestWhatAPersonIsTold:
    def test_the_message_names_the_rule_that_widened(self, commands):
        seen = frozenset(commands) - {"omarchy install app"}
        review = delta.compute(seen, commands, rules((Effect.ALLOW, "omarchy install *")))
        body = delta.message(review)
        assert "omarchy install *" in body
        assert "omarchy install app" in body
        assert "panel" in body

    def test_the_message_says_what_is_held(self, commands):
        seen = frozenset(commands) - {"omarchy install app"}
        review = delta.compute(seen, commands, rules((Effect.ALLOW, "omarchy install *")))
        assert "asked about once each" in delta.message(review)

    def test_the_report_serialises(self, commands):
        seen = without(commands, "omarchy install ")
        review = delta.compute(seen, commands, rules((Effect.ALLOW, "omarchy install *")))
        body = json.loads(json.dumps(delta.as_dict(review)))
        assert body["headline"] == review.headline
        assert len(body["arrivals"]) == len(review.arrivals)
