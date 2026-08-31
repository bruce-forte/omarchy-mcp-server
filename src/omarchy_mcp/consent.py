"""Asking the user, and what it means when nobody answers.

A consent mechanism is only worth having if every way it can fail to produce a
*yes* ends in a refusal. This daemon starts with the session and outlives
whoever walked away from the desk, so a prompt that grants on expiry grants to
an empty room.

Six outcomes, and exactly one of them runs the command:

    accepted     the user said yes, and said it in time
    declined     the user said no to this specific call
    cancelled    the user dismissed the prompt without deciding
    timed_out    nobody answered; assume nobody was there
    unsupported  the client cannot ask anyone -- never read as a yes
    unreachable  the client went away mid-question

They are distinct because they imply different next moves for the agent, and an
agent that cannot tell "the user said no" from "nobody was there" will either
give up on a call the user would have allowed or keep re-asking an empty room.

The mechanism -- MCP elicitation -- is N4's. This module knows only that
something can be awaited for an answer, which is what lets the rule be tested
before a client is involved.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from enum import Enum

import anyio

#: Long enough to walk back from the kettle, short enough that an agent is not
#: parked on a dead request. Overridable as `policy.ask_timeout_s`.
DEFAULT_TIMEOUT_S = 60


class Outcome(str, Enum):
    ACCEPTED = "accepted"
    DECLINED = "declined"
    CANCELLED = "cancelled"
    TIMED_OUT = "timed_out"
    UNSUPPORTED = "unsupported"
    UNREACHABLE = "unreachable"


@dataclass(frozen=True)
class Answer:
    """What came back, why, and anything the user typed while answering."""

    outcome: Outcome
    reason: str = ""
    #: The structured reply on an accept. Elicitation can ask *which* theme or
    #: *how many* minutes and get a typed answer inside the same call; a waiter
    #: that dropped it would throw away the reason for choosing elicitation.
    data: object | None = None

    @property
    def accepted(self) -> bool:
        return self.outcome is Outcome.ACCEPTED


def _reason(outcome: Outcome, *, timeout_s: float, what: str) -> str:
    """What the agent is told. Each one has to suggest a different next move."""
    return {
        Outcome.ACCEPTED: "",
        Outcome.DECLINED: f"The user refused this call ({what}).",
        Outcome.CANCELLED: (
            f"The user dismissed the request ({what}) without deciding. "
            f"Nothing ran. Ask them directly rather than repeating it."
        ),
        Outcome.TIMED_OUT: (
            f"Nobody answered within {timeout_s}s, so the request ({what}) was "
            f"refused. Assume nobody is at the desk; repeating it will not help."
        ),
        Outcome.UNSUPPORTED: (
            f"This client cannot ask the user anything, so the request ({what}) "
            f"was refused rather than assumed. Allow the route in "
            f"~/.config/omarchy/mcp/config.toml to run it without asking."
        ),
        Outcome.UNREACHABLE: (
            f"The client disconnected before answering ({what}). Nothing ran."
        ),
    }[outcome]


def supports_asking(capabilities) -> bool:
    """Whether this client can put a question in front of a person.

    `form` specifically, not `url`: url-mode elicitation sends the user to a
    browser tab to answer, and this project exists because the person is looking
    at a desktop. A client that declared no capabilities at all arrives as
    ``None``, which is not consent either.
    """
    elicitation = getattr(capabilities, "elicitation", None)
    return getattr(elicitation, "form", None) is not None


def unsupported(what: str, *, timeout_s: float = DEFAULT_TIMEOUT_S) -> Answer:
    """The refusal for a client that cannot be asked. Never a hang, never a yes."""
    return Answer(
        Outcome.UNSUPPORTED, _reason(Outcome.UNSUPPORTED, timeout_s=timeout_s, what=what)
    )


async def ask(
    asker: Callable[[], Awaitable[object]],
    *,
    what: str,
    timeout_s: float = DEFAULT_TIMEOUT_S,
    log=None,
) -> Answer:
    """Await an answer, and fail closed if one does not arrive.

    ``asker`` is anything that can be awaited for an elicitation result -- N4
    passes `ctx.elicit`, the tests pass a fake. `anyio` rather than `asyncio`
    because the SDK's transport is anyio, so this runs on whichever backend the
    host chose.

    At the deadline the awaitable is **cancelled and its result never read**. A
    click that lands a second late has nowhere to go: the agent has already been
    told the call was refused and may have done something else since, so acting
    on it would run a command minutes after the user's attention moved on.
    """
    result: object | None = None
    with anyio.move_on_after(timeout_s) as scope:
        try:
            result = await asker()
        except Exception as exc:  # noqa: BLE001 -- a broken prompt is an answer, not a crash
            if log is not None:
                log.info("consent unreachable (%s): %s", type(exc).__name__, exc)
            return Answer(
                Outcome.UNREACHABLE,
                _reason(Outcome.UNREACHABLE, timeout_s=timeout_s, what=what),
            )

    if scope.cancelled_caught:
        if log is not None:
            log.info("consent timed out after %ss: %s", timeout_s, what)
        return Answer(
            Outcome.TIMED_OUT, _reason(Outcome.TIMED_OUT, timeout_s=timeout_s, what=what)
        )

    return _interpret(result, timeout_s=timeout_s, what=what)


def _interpret(result: object, *, timeout_s: float, what: str) -> Answer:
    """Read the SDK's reply.

    Matched on `action` rather than on the class, because the SDK models these
    as three separate types and a fourth would otherwise arrive as an
    `isinstance` miss. Anything unrecognised is treated as no consent: only the
    word "accept" runs anything.
    """
    action = getattr(result, "action", None)
    if action == "accept":
        return Answer(Outcome.ACCEPTED, "", getattr(result, "data", None))
    if action == "decline":
        return Answer(
            Outcome.DECLINED, _reason(Outcome.DECLINED, timeout_s=timeout_s, what=what)
        )
    return Answer(
        Outcome.CANCELLED, _reason(Outcome.CANCELLED, timeout_s=timeout_s, what=what)
    )
