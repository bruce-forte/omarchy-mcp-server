"""Whether a command runs, and who got to decide.

`policy.py` says what the tiers are. `resolve.py` says what an argument names.
`consent.py` says what an answer means. This is where those three meet and a
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
- **`policy.deny` is never asked about either.** That refusal is a decision the
  user already took, by hand, in their own config file. Re-asking it would turn
  their *no* into a question.
- **Only an accept runs anything.** Every other way of leaving the question --
  decline, dismissal, deadline, a client that cannot ask, a client that went
  away -- refuses.
"""

from __future__ import annotations

from dataclasses import dataclass

from pydantic import BaseModel

from . import consent, prompt, resolve
from .config import Config
from .policy import Tier, Verdict, decide, self_refusal
from .registry import Command


@dataclass(frozen=True)
class Allowed:
    """The call may run, and this is what it was understood to be acting on."""

    call: resolve.Call
    verdict: Verdict
    #: How the user answered, when they were asked at all. ``None`` means
    #: nothing was put to them -- the route was safe, or it was guarded and
    #: already in `policy.allow`. Not derivable by the caller from tier and
    #: config, and the activity log has to tell those two apart: one is a
    #: decision the user took just now, the other one they took months ago.
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


def asks(verdict: Verdict, config: Config) -> bool:
    """Whether a route would put a question to the user rather than refuse.

    One derivation, used by the gate and by both places that publish what a
    command can do -- `omarchy_search_commands` and the commands resource. They
    would otherwise be free to disagree with each other and with this.

    Deliberately not a function of the calling client. A client with no way to
    be asked still gets a truthful refusal when it calls, one call later, and
    the alternative is two answers to the same question depending on who is
    listening.
    """
    return verdict.askable and config.ask


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
    config: Config,
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

    verdict = decide(cmd, config)

    if verdict.allowed:
        try:
            call = await offload(resolve.resolve_call, cmd.route, args)
        except resolve.Unresolvable as exc:
            return Refused(exc.message, verdict.tier.value, payload=exc.as_dict())
        return Allowed(call, verdict)

    if not asks(verdict, config):
        # Includes every blocked route, and every route the user denied by hand.
        return Refused(verdict.reason, verdict.tier.value)

    # Resolution comes *before* the question, and only on this path. A prompt
    # reading "set theme Tokyo Night" is consent; one reading "run omarchy theme
    # set" is not, because the user cannot tell what it would do (N2). And a
    # question answered yes and then refused as unresolvable has spent the
    # user's attention for nothing. A refusal that will never ask still does not
    # pay for a resolver subprocess.
    try:
        call = await offload(resolve.resolve_call, cmd.route, args)
    except resolve.Unresolvable as exc:
        return Refused(exc.message, verdict.tier.value, payload=exc.as_dict())

    answer = await _ask(cmd, call, config=config, ctx=ctx, log=log, offload=offload)
    log.info("consent %s route=%r", answer.outcome.value, cmd.route)
    if not answer.accepted:
        return Refused(answer.reason, verdict.tier.value, outcome=answer.outcome.value)
    return Allowed(call, verdict, consent=answer.outcome.value)


async def _ask(cmd, call, *, config, ctx, log, offload) -> consent.Answer:
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
        async with prompt.pending(label, body, token=token, log=log, offload=offload):
            asker = (
                (lambda: ctx.elicit(body, Approval))
                if elicits
                else (lambda: prompt.desktop_ask(token))
            )
            return await consent.ask(
                asker,
                what=label,
                timeout_s=config.ask_timeout_s,
                clicked=not elicits,
                log=log,
            )
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
