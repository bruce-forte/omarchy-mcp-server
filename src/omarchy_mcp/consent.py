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

The mechanism is not this module's. It knows only that something can be awaited
for an answer, which is what lets the rule be tested before a client is
involved -- and what saved N4 when elicitation turned out to be unreachable
over this transport (F23). There are two askers now, an MCP elicitation and a
desktop notification, and neither one changed a line of the rule.
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from enum import Enum
from typing import Any

import anyio

#: Long enough to walk back from the kettle, short enough that an agent is not
#: parked on a dead request. Overridable as `askTimeoutSeconds`.
DEFAULT_TIMEOUT_S = 60


class Outcome(str, Enum):
    """The six ways a question can end. Only ``ACCEPTED`` runs anything."""

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
        """The one question worth asking of an answer, in one place.

        ``is`` rather than ``==``: enum members are singletons, so identity is
        the exact comparison, and it cannot be satisfied by a bare string that
        happens to read "accepted".
        """
        return self.outcome is Outcome.ACCEPTED


def _reason(outcome: Outcome, *, timeout_s: float, what: str, clicked: bool = False) -> str:
    """What the agent is told. Each one has to suggest a different next move.

    ``clicked`` says the question went to the desktop, where the notification
    has exactly one action (F25). That surface can express *yes* but not *no*,
    so a non-answer there is genuinely ambiguous and the wording says so rather
    than asserting nobody was at the desk.
    """
    if outcome is Outcome.TIMED_OUT and clicked:
        return (
            f"The approval notification for ({what}) was not clicked within "
            f"{timeout_s}s. That may mean the user refused it or that they were "
            f"not there. Nothing ran. Ask them directly rather than repeating it."
        )
    # A dict used as a switch: build the mapping, then index it with the
    # outcome. Every member has an entry, so a missing one raises a KeyError
    # here rather than returning an empty explanation to the agent.
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
            f"was refused rather than assumed. Add an \"allow\" rule for the route "
            f"in ~/.config/omarchy/mcp/permissions.json to run it without asking."
        ),
        Outcome.UNREACHABLE: (
            f"The client disconnected before answering ({what}). Nothing ran."
        ),
    }[outcome]


def supports_asking(capabilities: Any) -> bool:
    """Whether this client can put a question in front of a person itself.

    Not `form` alone, which was the original rule and was wrong. Claude Code
    declares a bare ``elicitation: {}`` -- the object present, neither sub-mode
    named (F22) -- and checking ``form is not None`` refuses the client this
    project exists for. A client that names *only* `url` is still refused: url
    mode answers in a browser tab, and the person here is at a desktop.

    A client that declared no capabilities at all arrives as ``None``, which is
    not consent either. Note this says nothing about whether the *transport*
    can carry the question; see `gate.can_elicit`.
    """
    # ``getattr`` with a default all the way down, because every level of this
    # may be absent depending on what the client declared.
    elicitation = getattr(capabilities, "elicitation", None)
    if elicitation is None:
        return False
    return (
        getattr(elicitation, "url", None) is None
        or getattr(elicitation, "form", None) is not None
    )


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
    clicked: bool = False,
    log: logging.Logger | None = None,
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
    # ``move_on_after`` is a *cancel scope*: if the block inside has not
    # finished within the timeout, whatever it is awaiting is cancelled and
    # execution continues after the ``with``. The scope then reports that it
    # did so through ``cancelled_caught``, checked below.
    with anyio.move_on_after(timeout_s) as scope:
        try:
            # ``asker`` is a function that returns something awaitable, so it is
            # called *and* awaited: the brackets run it, the ``await`` waits.
            result = await asker()
        # Deliberately broad. ``noqa`` switches off the linter rule that
        # objects to it (BLE001, "blind except"), with the reason on the line.
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
            Outcome.TIMED_OUT,
            _reason(Outcome.TIMED_OUT, timeout_s=timeout_s, what=what, clicked=clicked),
        )

    return _interpret(result, timeout_s=timeout_s, what=what)


def _interpret(result: object, *, timeout_s: float, what: str) -> Answer:
    """Read the SDK's reply.

    Matched on `action` rather than on the class, because the SDK models these
    as three separate types and a fourth would otherwise arrive as an
    `isinstance` miss. Anything unrecognised is treated as no consent: only the
    word "accept" runs anything.
    """
    # ``result`` is typed ``object`` because it comes from the SDK and this
    # module deliberately knows nothing about its classes -- only about the one
    # attribute it reads.
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
