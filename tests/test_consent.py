"""No answer means denied.

The rule under test is that exactly one path grants: a user who said yes, in
time, through a client that could ask them. Everything else refuses, and each
refusal says something different, because "the user said no" and "nobody was
there" call for different next moves from an agent.

The mechanism (MCP elicitation) is N4's; these tests pass a fake asker, which is
the point of separating the two -- the rule is pinned before any client is
involved.
"""

from __future__ import annotations

import json

import logging

import anyio
import pytest

from omarchy_mcp import consent
from omarchy_mcp.permissions import (
    ASK_TIMEOUT_BOUNDS,
    DEFAULT_ASK_TIMEOUT_S,
    Permissions,
    PermissionsError,
    parse,
)


class Reply:
    """What the SDK hands back: three types sharing an `action` field."""

    def __init__(self, action: str, data: object | None = None) -> None:
        self.action = action
        if data is not None:
            self.data = data


def answered(action: str, data: object | None = None):
    async def asker():
        return Reply(action, data)

    return asker


class TestTheAnswer:
    @pytest.mark.anyio
    async def test_accept_is_the_only_thing_that_grants(self):
        answer = await consent.ask(answered("accept"), what="switch the theme")
        assert answer.accepted is True
        assert answer.reason == ""

    @pytest.mark.anyio
    async def test_an_accept_carries_what_the_user_typed(self):
        """Elicitation returns structured input; a waiter that dropped it would
        throw away the reason for choosing elicitation over a confirm dialog."""
        answer = await consent.ask(answered("accept", {"theme": "Tokyo Night"}), what="x")
        assert answer.data == {"theme": "Tokyo Night"}

    @pytest.mark.anyio
    async def test_decline_and_cancel_are_different_answers(self):
        declined = await consent.ask(answered("decline"), what="reboot")
        cancelled = await consent.ask(answered("cancel"), what="reboot")
        assert declined.outcome is consent.Outcome.DECLINED
        assert cancelled.outcome is consent.Outcome.CANCELLED
        assert declined.reason != cancelled.reason
        assert not declined.accepted and not cancelled.accepted

    @pytest.mark.anyio
    async def test_an_action_the_sdk_grows_later_does_not_grant(self):
        """Only the word "accept" runs anything. A fourth action added upstream
        must fail closed rather than fall through as consent."""
        answer = await consent.ask(answered("deferred"), what="reboot")
        assert answer.accepted is False


class TestNobodyAnswers:
    @pytest.mark.anyio
    async def test_a_prompt_nobody_answers_is_refused(self):
        async def never():
            await anyio.sleep(3600)

        with anyio.fail_after(5):  # the test itself must not hang if this breaks
            answer = await consent.ask(never, what="reboot", timeout_s=1)
        assert answer.outcome is consent.Outcome.TIMED_OUT
        assert "1s" in answer.reason

    @pytest.mark.anyio
    async def test_a_late_answer_changes_nothing(self):
        """The deadline is the decision. By the time this click lands the agent
        has been told it was refused and may have moved on."""
        ran = []

        async def slow():
            await anyio.sleep(1)
            ran.append("answered")
            return Reply("accept")

        answer = await consent.ask(slow, what="reboot", timeout_s=0.05)
        assert answer.accepted is False
        assert ran == [], "the awaitable must be cancelled, not merely ignored"

    @pytest.mark.anyio
    async def test_a_client_that_disappears_is_its_own_outcome(self):
        async def broken():
            raise ConnectionResetError("stream closed")

        answer = await consent.ask(
            broken, what="reboot", log=logging.getLogger("test")
        )
        assert answer.outcome is consent.Outcome.UNREACHABLE
        assert answer.accepted is False

    @pytest.mark.anyio
    async def test_a_broken_prompt_never_becomes_a_traceback(self):
        """A raised exception here would surface as a tool-call error and bury
        the reason the command did not run."""

        async def broken():
            raise RuntimeError("boom")

        assert (await consent.ask(broken, what="x")).accepted is False


class TestClientCapability:
    """An unsupported client is refused, never hung on and never assumed."""

    class Caps:
        def __init__(self, elicitation=None):
            self.elicitation = elicitation

    class Elicit:
        def __init__(self, form=None, url=None):
            self.form = form
            self.url = url

    def test_a_client_that_declared_nothing_cannot_be_asked(self):
        assert consent.supports_asking(None) is False

    def test_no_elicitation_capability_cannot_be_asked(self):
        assert consent.supports_asking(self.Caps()) is False

    def test_url_mode_alone_is_not_enough(self):
        """URL mode answers in a browser tab. This project exists because the
        person is looking at a desktop."""
        assert consent.supports_asking(self.Caps(self.Elicit(url=object()))) is False

    def test_form_mode_can_be_asked(self):
        assert consent.supports_asking(self.Caps(self.Elicit(form=object()))) is True

    def test_the_refusal_names_the_way_out(self):
        answer = consent.unsupported("reboot")
        assert answer.outcome is consent.Outcome.UNSUPPORTED
        assert not answer.accepted
        assert "permissions.json" in answer.reason


class TestTheTimeout:
    """It lives with the permissions now, because it is one of their terms: how
    long a question stands before silence answers it."""

    def test_the_default_is_sixty_seconds(self):
        assert Permissions().ask_timeout_s == DEFAULT_ASK_TIMEOUT_S == 60

    def test_a_value_in_range_is_taken(self):
        _, options = parse(
            json.dumps({"permissions": {"askTimeoutSeconds": 120}}), source="permissions.json"
        )
        assert options.ask_timeout_s == 120

    @pytest.mark.parametrize("value", [1, 3600, "soon"])
    def test_an_impossible_wait_refuses_the_document(self, value):
        """A second is not long enough to read the question; ten minutes is a
        request parked on a desk nobody is at. Unlike config.toml, an
        out-of-range value here is not clamped and warned about -- the whole
        document is refused, because a permissions file we cannot read as
        written is intent we do not know."""
        with pytest.raises(PermissionsError):
            parse(
                json.dumps({"permissions": {"askTimeoutSeconds": value}}),
                source="permissions.json",
            )

    def test_the_bounds_are_the_ones_the_message_promises(self):
        assert ASK_TIMEOUT_BOUNDS == (5, 600)
