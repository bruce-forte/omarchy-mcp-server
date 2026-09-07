"""Whether a command runs, and who got to decide.

`policy.py` says what kind of command it is. `permissions.py` says what the
user's rules do with that. `resolve.py` says what an argument names.
`consent.py` says what an answer means. This is where those four meet and a
call either proceeds or does not, and it is the only place that decision is
made -- `_shared.run_route` and `omarchy_run` both come through here, because a
check that one path applies and the other skips is worse than no check.

Read `SECURITY.md` before changing it.

Three rules this must never lose:

- **`blocked` is never asked about.** A sudo command is refused before anything
  is resolved, before a notification is raised, and before a question exists.
  No answer makes it runnable -- decision 4. A call that would switch off this
  server's own supervision is refused on the same terms and in the same place,
  before the tier is even computed: see `policy.self_refusal`.
- **A `deny` rule is never asked about either.** That refusal is a decision the
  user already took, by hand, in their own permissions file. Re-asking it would
  turn their *no* into a question.
- **Only an accept runs anything.** Every other way of leaving the question --
  decline, dismissal, deadline, a client that cannot ask, a client that went
  away -- refuses.
"""

from __future__ import annotations

from dataclasses import dataclass

from pydantic import BaseModel

from . import activity, consent, frames, permissions, prompt, registry, resolve
from .paths import PERMISSIONS_LOCAL_FILE
from .permissions import Effect, Outcome, Permissions, decide
from .policy import Tier, self_refusal
from .registry import Command


@dataclass(frozen=True)
class Allowed:
    """The call may run, and this is what it was understood to be acting on."""

    call: resolve.Call
    outcome: Outcome
    #: How the user answered, when they were asked at all. ``None`` means
    #: nothing was put to them -- the route was safe, or an `allow` rule already
    #: covered it. Not derivable by the caller from tier and rules, and the
    #: activity log has to tell those two apart: one is a decision the user took
    #: just now, the other one they took months ago.
    consent: str | None = None


@dataclass(frozen=True)
class Refused:
    """The call may not run, and this is what the agent is told and why."""

    reason: str
    tier: str
    outcome: str | None = None
    #: Set when the refusal is a resolver's, whose payload names near misses.
    payload: dict | None = None

    def as_dict(self) -> dict:
        if self.payload is not None:
            return self.payload
        body: dict[str, object] = {"error": self.reason, "tier": self.tier}
        if self.outcome is not None:
            body["consent"] = self.outcome
        return body


def can_elicit(ctx) -> bool:
    """Whether the client can be asked *through MCP*, on this connection.

    Two separate questions, and both have to be yes. The client must declare
    elicitation, and the transport must be able to carry a server-initiated
    request at all -- which under protocol ``2026-07-28`` it cannot, by
    construction (F23). Asking the transport first is not an optimisation: the
    SDK raises `NoBackChannelError` from inside our own process, which would
    arrive as `unreachable` and tell the agent the client disconnected when
    nothing of the sort happened.
    """
    session = getattr(ctx, "session", None)
    if session is None or not getattr(session, "can_send_request", False):
        return False
    return consent.supports_asking(getattr(ctx, "client_capabilities", None))


#: One question at a time per client session: ``id(session) -> session``.
#:
#: An agent issues tool calls in parallel, so a batch of five guarded calls
#: would raise five dialogs and five notifications that never expire. Keyed on
#: the session rather than globally, because two *clients* asking at once is the
#: property decision 1 rests on -- a transport that gave each client its own
#: process would have to serialise approvals through the filesystem.
#:
#: The session object is the value, not just the key, so it cannot be collected
#: and have its `id` handed to something else while a question is still open.
_pending: dict[int, object] = {}


async def authorize(
    cmd: Command,
    args: list[str],
    *,
    perms: Permissions,
    ctx,
    log,
    offload,
) -> Allowed | Refused:
    """Decide whether ``cmd`` runs, asking the user if that is what is called for."""
    # Before the tier, because this is not one. `omarchy shell` and
    # `omarchy plugin remove` are ordinary routes whose *arguments* decide
    # whether the call would silence this daemon, and an agent that can do that
    # does not need to defeat anything else here.
    refusal = self_refusal(cmd.route, args)
    if refusal is not None:
        log.info("self-call refused route=%r", cmd.route)
        return Refused(refusal, Tier.BLOCKED.value)

    outcome = decide(cmd, perms)

    if outcome.effect is Effect.ALLOW:
        try:
            call = await offload(resolve.resolve_call, cmd.route, args)
        except resolve.Unresolvable as exc:
            return Refused(exc.message, outcome.tier.value, payload=exc.as_dict())
        return Allowed(call, outcome)

    if outcome.effect is Effect.DENY:
        # Every blocked route, every route a `deny` rule covers, and every
        # guarded one under `guardedDefault: "deny"`.
        return Refused(outcome.reason, outcome.tier.value)

    # Resolution comes *before* the question, and only on this path. A prompt
    # reading "set theme Tokyo Night" is consent; one reading "run omarchy theme
    # set" is not, because the user cannot tell what it would do (N2). And a
    # question answered yes and then refused as unresolvable has spent the
    # user's attention for nothing. A refusal that will never ask still does not
    # pay for a resolver subprocess.
    try:
        call = await offload(resolve.resolve_call, cmd.route, args)
    except resolve.Unresolvable as exc:
        return Refused(exc.message, outcome.tier.value, payload=exc.as_dict())

    answer = await _ask(cmd, call, perms=perms, ctx=ctx, log=log, offload=offload)
    log.info("consent %s route=%r", answer.outcome.value, cmd.route)
    if not answer.accepted:
        return Refused(answer.reason, outcome.tier.value, outcome=answer.outcome.value)

    if _wants_always(answer):
        try:
            await offload(_write_grant, cmd.route, perms, log)
        except permissions.GrantRefused as exc:
            # The pool is the authority at the moment of execution. If the user's
            # own file grew a matching `deny` while the prompt was up, that deny
            # is newer than the question, and running the command because a click
            # was in flight is indefensible.
            log.warning("always refused route=%r: %s", cmd.route, exc)
            return Refused(
                f"{exc} The call was refused rather than run.",
                outcome.tier.value,
                outcome=answer.outcome.value,
            )

    return Allowed(call, outcome, consent=answer.outcome.value)


def _wants_always(answer: consent.Answer) -> bool:
    """Whether the accept carried *and stop asking*.

    Read defensively: `data` is whatever the asker handed back, and an eliciting
    client's is a model the user filled in. Anything that is not exactly the flag
    this daemon's own panel sets is a plain accept, which is the safe reading.
    """
    data = answer.data
    return isinstance(data, dict) and data.get("always") is True


def _write_grant(route: str, perms: Permissions, log) -> None:
    """Append the `allow` rule, and say so where it will be kept.

    Blocking: it reads a file, writes a temp file, fsyncs and renames. Called
    through `offload` for the same reason every other filesystem touch here is.
    """
    rule = permissions.grant(
        PERMISSIONS_LOCAL_FILE, route, perms, registry.all_commands()
    )
    log.info("granted route=%r via=panel", route)
    activity.note("permission", verb="allow", route=rule.matcher, via="panel")


async def _ask(cmd, call, *, perms, ctx, log, offload) -> consent.Answer:
    label = f"{cmd.route}" + (f" — {call.target.label}" if call.target else "")
    # A ctx with no session still gets a slot of its own, so two such calls
    # cannot be mistaken for one client asking twice.
    holder = getattr(ctx, "session", None) or ctx
    key = id(holder)

    if key in _pending:
        return consent.Answer(
            consent.Outcome.DECLINED,
            f"An approval is already pending in this session; the request "
            f"({label}) was refused rather than raising a second prompt. "
            f"Answer the first one, then try again.",
        )

    elicits = can_elicit(ctx)
    token = None if elicits else prompt.new_token()
    body = prompt.message(cmd.route, list(call.args), call.target.label if call.target else None)

    _pending[key] = holder
    try:
        async with prompt.pending(label, body, token=token, log=log, offload=offload) as marker:
            # The panel is the other surface the question is on, and it learns
            # about it here rather than from a file it polls. Only when there is
            # a token: an eliciting client answers in its own UI, and a panel
            # showing a question it cannot answer is worse than showing none.
            if token is not None:
                frames.asking(
                    token,
                    cmd.route,
                    list(call.args),
                    call.target.label if call.target else None,
                    marker,
                )
            asker = (
                (lambda: ctx.elicit(body, Approval))
                if elicits
                else (lambda: prompt.desktop_ask(token))
            )
            answer = await consent.ask(
                asker,
                what=label,
                timeout_s=perms.ask_timeout_s,
                clicked=not elicits,
                log=log,
            )
            if token is not None:
                # A bar surface exists once per screen. Whichever panel answered
                # spent the token; this is what takes the question off the
                # others rather than leaving them showing a dead prompt.
                frames.answered(marker, answer.outcome.value)
            return answer
    finally:
        _pending.pop(key, None)


class Approval(BaseModel):
    """What an eliciting client is asked for.

    One optional field rather than none. An empty schema invites a client to
    accept with no content at all, and the SDK raises `ValueError` on that
    (`elicitation.py`: "Received an accepted elicitation with no content"),
    which would arrive here as a transport failure and report a genuine *yes*
    to the agent as a disconnected client.
    """

    note: str = ""
