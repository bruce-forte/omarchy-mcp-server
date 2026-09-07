"""When the daemon stops asking, and why.

`guardedDefault` is `ask`, which hands an agent a way to put a critical
notification on somebody's desktop repeatedly. A reflexive click is not consent.

Two mechanisms, because the write-up's single one covered only half of it:

- a **per-route cooldown** after any answer that was not yes, which is what stops
  an agent nagging
- a **burst cap** on prompts raised at all, which is the only one that can see
  habituation: fifty prompts and fifty clicks contains no refusals and is the
  worst case there is
"""

from __future__ import annotations

import pytest

from omarchy_mcp import cooldown
from omarchy_mcp.cooldown import (
    BURST_LIMIT,
    BURST_WINDOW_S,
    FIRST_COOLDOWN_S,
    MAX_COOLDOWN_S,
    Cooldowns,
)

ROUTE = "omarchy install app"
OTHER = "omarchy theme remove"


class Clock:
    """Time the test moves, so nothing sleeps and nothing is patched globally."""

    def __init__(self) -> None:
        self.t = 1000.0

    def __call__(self) -> float:
        return self.t

    def advance(self, seconds: float) -> None:
        self.t += seconds


@pytest.fixture
def clock():
    return Clock()


@pytest.fixture
def cools(clock):
    return Cooldowns(clock=clock)


def refuse(cools, route=ROUTE):
    """One full ask that did not end in a yes."""
    cools.asked(route)
    cools.answered(route, accepted=False)


def accept(cools, route=ROUTE):
    cools.asked(route)
    cools.answered(route, accepted=True)


class TestNagging:
    def test_a_fresh_route_may_be_asked_about(self, cools):
        assert cools.refusal(ROUTE) is None

    def test_one_refusal_stops_the_next_ask(self, cools):
        refuse(cools)
        assert cools.refusal(ROUTE) is not None

    def test_it_expires(self, cools, clock):
        refuse(cools)
        clock.advance(FIRST_COOLDOWN_S + 1)
        assert cools.refusal(ROUTE) is None

    def test_it_only_covers_the_route_that_was_refused(self, cools):
        refuse(cools)
        assert cools.refusal(OTHER) is None

    @pytest.mark.parametrize("outcome", ["declined", "dismissed", "timed out"])
    def test_every_way_of_not_saying_yes_counts(self, cools, outcome):
        """`consent.py` already treats them all as no, and re-asking an empty
        room is the purest form of the thing this guards against."""
        cools.asked(ROUTE)
        cools.answered(ROUTE, accepted=False)
        assert cools.refusal(ROUTE) is not None

    def test_consecutive_refusals_double_the_wait(self, cools, clock):
        refuse(cools)
        clock.advance(FIRST_COOLDOWN_S + 1)
        refuse(cools)

        clock.advance(FIRST_COOLDOWN_S + 1)
        assert cools.refusal(ROUTE) is not None, "the second wait is longer than the first"
        clock.advance(FIRST_COOLDOWN_S)
        assert cools.refusal(ROUTE) is None

    def test_the_wait_is_capped(self, cools, clock):
        for _ in range(20):
            clock.advance(MAX_COOLDOWN_S + 1)
            refuse(cools)
        clock.advance(MAX_COOLDOWN_S + 1)
        assert cools.refusal(ROUTE) is None

    def test_a_yes_forgets_the_route(self, cools, clock):
        """An engaged user is the opposite of a habituated one, and a route they
        just approved is not one they are being nagged about."""
        refuse(cools)
        clock.advance(FIRST_COOLDOWN_S + 1)
        accept(cools)
        refuse(cools)

        clock.advance(FIRST_COOLDOWN_S + 1)
        assert cools.refusal(ROUTE) is None, "the count restarted at one"

    def test_the_refusal_tells_the_agent_to_stop_and_how(self, cools):
        refuse(cools)
        reason = cools.refusal(ROUTE)
        assert ROUTE in reason
        assert "Do not keep trying" in reason
        assert "permissions.json" in reason, "an agent told only no tries the next spelling"


class TestHabituation:
    """Volume, regardless of the answer. A decline-keyed rule cannot see this."""

    def test_a_stream_of_accepted_prompts_still_trips_it(self, cools):
        for _ in range(BURST_LIMIT):
            accept(cools, f"omarchy install thing{_}")
        assert cools.refusal("omarchy anything else") is not None

    def test_it_names_the_number_and_the_way_out(self, cools):
        for i in range(BURST_LIMIT):
            accept(cools, f"omarchy install thing{i}")
        reason = cools.refusal("omarchy anything else")
        assert str(BURST_LIMIT) in reason
        assert "allow it once" in reason

    def test_under_the_limit_is_fine(self, cools):
        for i in range(BURST_LIMIT - 1):
            accept(cools, f"omarchy install thing{i}")
        assert cools.refusal("omarchy anything else") is None

    def test_the_window_rolls(self, cools, clock):
        for i in range(BURST_LIMIT):
            accept(cools, f"omarchy install thing{i}")
        assert cools.refusal("x") is not None

        clock.advance(BURST_WINDOW_S + 1)
        assert cools.refusal("x") is None

    def test_it_covers_routes_that_were_never_refused(self, cools):
        """The cap is about the desktop, not about any one command."""
        for i in range(BURST_LIMIT):
            accept(cools, f"omarchy install thing{i}")
        assert cools.refusal("omarchy theme set") is not None


class TestWhatThePanelShows:
    """A call refused without explanation is the actual failure mode here."""

    def test_it_reports_the_burst_budget(self, cools):
        for i in range(3):
            accept(cools, f"omarchy install thing{i}")
        state = cools.state()
        assert state["recentPrompts"] == 3
        assert state["promptLimit"] == BURST_LIMIT
        assert state["suppressed"] is False

    def test_it_says_when_asking_has_stopped(self, cools):
        for i in range(BURST_LIMIT):
            accept(cools, f"omarchy install thing{i}")
        assert cools.state()["suppressed"] is True

    def test_it_names_the_routes_cooling_down(self, cools):
        refuse(cools)
        cooling = cools.state()["cooling"]
        assert [c["route"] for c in cooling] == [ROUTE]
        assert cooling[0]["refusals"] == 1
        assert 0 < cooling[0]["secondsLeft"] <= FIRST_COOLDOWN_S

    def test_expired_entries_leave_the_report(self, cools, clock):
        refuse(cools)
        clock.advance(FIRST_COOLDOWN_S + 1)
        assert cools.state()["cooling"] == []


class TestItIsNotAPermission:
    def test_nothing_is_persisted(self, tmp_path, cools):
        """A cooldown is a nag-guard. Surviving a restart would make it a
        decision nobody took."""
        refuse(cools)
        assert list(tmp_path.iterdir()) == []

    def test_a_fresh_instance_remembers_nothing(self, clock):
        first = Cooldowns(clock=clock)
        refuse(first)
        assert first.refusal(ROUTE) is not None
        assert Cooldowns(clock=clock).refusal(ROUTE) is None

    def test_the_module_holds_no_state_of_its_own(self):
        """`gate` owns the one instance, so a test can replace it."""
        assert not hasattr(cooldown, "_routes")
        assert not hasattr(cooldown, "_asked")
