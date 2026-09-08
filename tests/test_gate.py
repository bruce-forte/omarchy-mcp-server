"""The gate is where a command either runs or does not, so this file is its
specification rather than a set of examples.

The rules that must never regress, in the order they matter:

- a `blocked` route is refused without anyone being asked anything
- a route a `deny` rule covers is refused without anyone being asked anything
- only an accept runs; every other way of leaving the question refuses
- a click cannot be forged by a file merely existing, or replayed
- the notification comes down on every exit path, cancellation included
"""

from __future__ import annotations

import json
import logging

import anyio
import pytest

from omarchy_mcp import consent, gate, permissions, prompt
from omarchy_mcp.paths import PLUGIN_ID
from omarchy_mcp.permissions import Effect, Permissions, Rule, decide
from omarchy_mcp.policy import Tier, base_tier

LOG = logging.getLogger("test")


async def offload(fn, *args, **kwargs):
    """The gate's thread hop, run inline. Threads add nothing to these tests."""
    return fn(*args, **kwargs)


class Session:
    """A client connection. Identity is all the gate wants from it."""

    def __init__(self, can_send_request=False):
        self.can_send_request = can_send_request


class Elicitation:
    def __init__(self, form=None, url=None):
        self.form = form
        self.url = url


class Caps:
    def __init__(self, elicitation=None):
        self.elicitation = elicitation


class Ctx:
    """A tool call's context, as much of it as the gate touches."""

    def __init__(self, *, can_send_request=False, caps=None, reply=None):
        self.session = Session(can_send_request)
        self.client_capabilities = caps
        self.elicited: list[str] = []
        self._reply = reply

    async def elicit(self, message, schema):
        self.elicited.append(message)
        if self._reply is None:
            await anyio.sleep(3600)
        return self._reply


class Reply:
    def __init__(self, action, data=None):
        self.action = action
        self.data = data


@pytest.fixture
def quiet_notifications(monkeypatch):
    """Nothing in the suite may raise a real desktop notification."""
    sent: list[dict] = []
    dismissed: list[str] = []

    def fake_send(headline, body, *, token=None):
        sent.append({"headline": headline, "body": body, "token": token})

    monkeypatch.setattr(prompt, "send", fake_send)
    monkeypatch.setattr(prompt, "dismiss", dismissed.append)
    return sent, dismissed


@pytest.fixture(autouse=True)
def _fresh_cooldowns(monkeypatch):
    """Each test gets a daemon that has not asked anybody anything yet.

    `gate._cooldowns` is module-level, like `_pending`, because it is a property
    of this daemon and this desktop rather than of a session. That makes it
    shared between tests, and most of this file asks about one route repeatedly.
    """
    from omarchy_mcp import cooldown

    monkeypatch.setattr(gate, "_cooldowns", cooldown.Cooldowns())


@pytest.fixture
def consent_dir(tmp_path, monkeypatch):
    monkeypatch.setattr(prompt, "CONSENT_DIR", tmp_path / "consent")
    return tmp_path / "consent"


def guarded(commands):
    """A guarded route that is harmless if it ever does run.

    Not a reboot. These tests raise real prompts through mocked senders, and one
    behaviour change was all it took for a mocked prompt to become a real one --
    see `conftest.py`. The example is now safe on its own terms as well as
    guarded by the fixtures."""
    return commands["omarchy channel current"]


def sudo(commands):
    return next(c for c in commands.values() if c.requires_sudo)


#: Short on purpose. `config.load` bounds this key to 5-600s so that neither
#: extreme can reach a running daemon; the dataclass does not, and a test that
#: waits out a real deadline would add five seconds to the suite for nothing.
QUICK = 0.2


def rules(*specs: tuple[Effect, str]) -> Permissions:
    """A permissions document from (effect, matcher) pairs, in one line."""
    return Permissions(
        rules=tuple(
            Rule(effect=effect, matcher=matcher, source="permissions.json", index=i)
            for i, (effect, matcher) in enumerate(specs)
        ),
        ask_timeout_s=QUICK,
    )


def asking() -> Permissions:
    """The default document. A guarded route asks; nothing else is configured."""
    return Permissions(ask_timeout_s=QUICK)


def allowing(route: str) -> Permissions:
    return rules((Effect.ALLOW, route))


class TestWhatIsNeverAsked:
    """The two refusals no answer may overturn."""

    @pytest.mark.anyio
    async def test_a_sudo_route_is_refused_without_asking_anyone(self, commands, quiet_notifications):
        sent, _ = quiet_notifications
        cmd = sudo(commands)
        assert base_tier(cmd) is Tier.BLOCKED
        ctx = Ctx(reply=Reply("accept"))

        decision = await gate.authorize(
            cmd, [], perms=asking(), ctx=ctx, log=LOG, offload=offload
        )

        assert isinstance(decision, gate.Refused)
        assert "sudo" in decision.reason
        assert ctx.elicited == [], "a blocked route must never reach a person"
        assert sent == [], "and must never raise a notification either"

    @pytest.mark.anyio
    async def test_a_denied_route_is_refused_without_asking_anyone(
        self, commands, quiet_notifications
    ):
        """A `deny` rule is a decision the user already took by hand.

        Asking about it would turn their no into a question, and the answer
        they already gave into a default.
        """
        sent, _ = quiet_notifications
        cmd = commands["omarchy theme current"]
        perms = rules((Effect.DENY, "omarchy theme current"))
        ctx = Ctx(reply=Reply("accept"))

        decision = await gate.authorize(
            cmd, [], perms=perms, ctx=ctx, log=LOG, offload=offload
        )

        assert isinstance(decision, gate.Refused)
        assert "deny" in decision.reason
        assert "permissions.json" in decision.reason
        assert ctx.elicited == []
        assert sent == []

    @pytest.mark.anyio
    async def test_a_call_that_would_stop_this_daemon_is_refused_without_asking(
        self, commands, quiet_notifications
    ):
        """N12. `omarchy shell` is a `safe` route, so the tier says yes and the
        arguments say no. Nobody is asked: an approval prompt for "may I switch
        off the thing that records what I do" is a question that should not
        exist."""
        sent, _ = quiet_notifications
        cmd = commands["omarchy shell"]
        assert base_tier(cmd) is Tier.SAFE
        ctx = Ctx(reply=Reply("accept"))

        decision = await gate.authorize(
            cmd,
            [PLUGIN_ID, "stop"],
            perms=asking(),
            ctx=ctx,
            log=LOG,
            offload=offload,
        )

        assert isinstance(decision, gate.Refused)
        assert decision.tier == Tier.BLOCKED.value
        assert PLUGIN_ID in decision.reason
        assert ctx.elicited == []
        assert sent == []

    @pytest.mark.anyio
    async def test_the_same_route_still_reaches_other_targets(self, commands):
        """The refusal is about what the call names, not about the route."""
        decision = await gate.authorize(
            commands["omarchy shell"],
            ["omarchy.power", "toggle"],
            perms=asking(),
            ctx=Ctx(),
            log=LOG,
            offload=offload,
        )
        assert isinstance(decision, gate.Allowed)

    @pytest.mark.anyio
    async def test_removing_this_plugin_is_refused_before_the_guarded_tier(
        self, commands, quiet_notifications
    ):
        """`plugin` is guarded now, so an allow rule would otherwise run this."""
        sent, _ = quiet_notifications
        cmd = commands["omarchy plugin remove"]
        perms = rules((Effect.ALLOW, "omarchy plugin *"))

        decision = await gate.authorize(
            cmd, [PLUGIN_ID, "--yes"], perms=perms, ctx=Ctx(reply=Reply("accept")),
            log=LOG, offload=offload,
        )

        assert isinstance(decision, gate.Refused)
        assert decision.tier == Tier.BLOCKED.value
        assert sent == []


class TestTheGuardedDefault:
    @pytest.mark.anyio
    async def test_guarded_default_deny_refuses_without_asking(
        self, commands, quiet_notifications
    ):
        """The escape hatch for anyone who wants the old floor back."""
        sent, _ = quiet_notifications
        ctx = Ctx(reply=Reply("accept"))
        perms = Permissions(guarded_default=Effect.DENY, ask_timeout_s=QUICK)

        decision = await gate.authorize(
            guarded(commands), [], perms=perms, ctx=ctx, log=LOG, offload=offload
        )

        assert isinstance(decision, gate.Refused)
        assert ctx.elicited == []
        assert sent == []

    def test_the_refusal_names_the_file_and_the_rule_to_write(self, commands):
        """Otherwise nobody discovers the feature. An agent reads this and can
        tell the user exactly what to add."""
        reason = decide(guarded(commands), Permissions(guarded_default=Effect.DENY)).reason
        assert "permissions.json" in reason
        assert '"allow"' in reason
        assert guarded(commands).route in reason

    def test_a_route_that_will_ask_is_published_as_runnable(self, commands):
        """A careful agent does not call something reported as not runnable, so
        publishing the refusal would mean the prompt never fires."""
        outcome = decide(guarded(commands), asking())
        assert outcome.allowed is False
        assert outcome.asks is True
        assert decide(guarded(commands), Permissions(guarded_default=Effect.DENY)).asks is False


class TestEveryAnswer:
    """Four ways to leave the question, four different things the agent is told."""

    def _ctx(self, action):
        return Ctx(
            can_send_request=True,
            caps=Caps(Elicitation(form=object())),
            reply=Reply(action) if action else None,
        )

    @pytest.mark.anyio
    async def test_accept_runs_it(self, commands, quiet_notifications):
        ctx = self._ctx("accept")
        decision = await gate.authorize(
            guarded(commands), [], perms=asking(), ctx=ctx, log=LOG, offload=offload
        )
        assert isinstance(decision, gate.Allowed)
        assert len(ctx.elicited) == 1
        # The activity log has to tell an approval apart from a pre-allowed
        # route, and cannot derive it: both are guarded, and both ran.
        assert decision.consent == "accepted"

    @pytest.mark.anyio
    async def test_a_pre_allowed_route_records_no_consent(self, commands):
        """Nobody was asked, so nothing was answered."""
        cmd = guarded(commands)
        decision = await gate.authorize(
            cmd, [], perms=allowing(cmd.route), ctx=self._ctx(None), log=LOG, offload=offload
        )
        assert isinstance(decision, gate.Allowed)
        assert decision.consent is None

    @pytest.mark.anyio
    async def test_decline_refuses_and_says_the_user_refused(self, commands, quiet_notifications):
        decision = await gate.authorize(
            guarded(commands), [], perms=asking(), ctx=self._ctx("decline"), log=LOG,
            offload=offload,
        )
        assert isinstance(decision, gate.Refused)
        assert decision.outcome == "declined"
        assert "refused" in decision.reason

    @pytest.mark.anyio
    async def test_a_dismissal_is_not_a_refusal(self, commands, quiet_notifications):
        decision = await gate.authorize(
            guarded(commands), [], perms=asking(), ctx=self._ctx("cancel"), log=LOG,
            offload=offload,
        )
        assert isinstance(decision, gate.Refused)
        assert decision.outcome == "cancelled"
        assert "without deciding" in decision.reason

    @pytest.mark.anyio
    async def test_an_unknown_action_does_not_grant(self, commands, quiet_notifications):
        """Only the word accept runs anything, so an action added upstream
        fails closed instead of falling through."""
        decision = await gate.authorize(
            guarded(commands), [], perms=asking(), ctx=self._ctx("approve-ish"), log=LOG,
            offload=offload,
        )
        assert isinstance(decision, gate.Refused)

    @pytest.mark.anyio
    async def test_nobody_answering_refuses_rather_than_hanging(
        self, commands, quiet_notifications
    ):
        with anyio.fail_after(5):  # the test itself must not hang if this breaks
            decision = await gate.authorize(
                guarded(commands), [], perms=asking(), ctx=self._ctx(None), log=LOG,
                offload=offload,
            )
        assert isinstance(decision, gate.Refused)
        assert decision.outcome == "timed_out"


class TestWhichAskerIsUsed:
    def test_a_transport_with_no_back_channel_cannot_elicit(self):
        """F23: under protocol 2026-07-28 the SDK raises inside this process.

        Calling elicit anyway would report a genuine client as disconnected.
        """
        assert gate.can_elicit(Ctx(can_send_request=False, caps=Caps(Elicitation(form=object())))) is False

    def test_a_bare_elicitation_capability_counts(self):
        """F22: Claude Code declares `elicitation: {}` and names no sub-mode.

        Requiring `form` refuses the client this project exists for.
        """
        assert gate.can_elicit(Ctx(can_send_request=True, caps=Caps(Elicitation()))) is True

    def test_url_only_is_still_not_enough(self):
        ctx = Ctx(can_send_request=True, caps=Caps(Elicitation(url=object())))
        assert gate.can_elicit(ctx) is False

    def test_a_client_declaring_nothing_cannot_elicit(self):
        assert gate.can_elicit(Ctx(can_send_request=True, caps=None)) is False

    def test_a_context_with_no_request_in_flight_cannot_elicit(self):
        """The SDK's `Context.session` is a *property* that raises.

        `ValueError: Context is not available outside of a request` -- and
        `getattr(ctx, "session", None)` does not swallow an exception raised by
        the property it just called. A context that cannot answer the question
        is one that cannot carry a question, so it has to read as False rather
        than take the tool call down with it.
        """

        class NoRequest:
            @property
            def session(self):
                raise ValueError("Context is not available outside of a request")

            @property
            def client_capabilities(self):
                raise ValueError("Context is not available outside of a request")

        assert gate.can_elicit(NoRequest()) is False

    def test_a_context_missing_the_attributes_entirely_cannot_elicit(self):
        assert gate.can_elicit(object()) is False

    @pytest.mark.anyio
    async def test_a_client_that_cannot_elicit_gets_a_clickable_notification(
        self, commands, quiet_notifications, consent_dir
    ):
        """Not a hang, and not a refusal: the question goes to the desktop."""
        sent, _ = quiet_notifications

        with anyio.fail_after(5):
            decision = await gate.authorize(
                guarded(commands), [], perms=asking(), ctx=Ctx(), log=LOG, offload=offload
            )

        assert isinstance(decision, gate.Refused)
        assert decision.outcome == "timed_out"
        assert len(sent) == 1
        assert sent[0]["token"], "the notification has to be clickable to be an ask"
        assert "not clicked" in decision.reason, "silence here is ambiguous, and says so"


class TestAClickCannotBeForged:
    """The agent is the untrusted party, and it can pass arguments to hundreds
    of commands this project did not write."""

    def test_a_file_that_merely_exists_is_not_consent(self, consent_dir):
        token = prompt.new_token()
        prompt._prepare_dir()
        (consent_dir / token).write_text("")
        assert prompt._answer(token) is None

    def test_a_file_with_the_wrong_contents_is_not_consent(self, consent_dir):
        token = prompt.new_token()
        prompt._prepare_dir()
        (consent_dir / token).write_text(prompt.new_token())
        assert prompt._answer(token) is None

    def test_a_real_click_is_consent(self, consent_dir):
        token = prompt.new_token()
        prompt._prepare_dir()
        (consent_dir / token).write_text(token)
        assert prompt._answer(token) == "once"

    @pytest.mark.parametrize("verb", sorted(prompt.VERBS))
    def test_the_panel_verbs_need_the_secret_too(self, consent_dir, verb):
        """The panel can say more than a notification can, through the same
        one-time token. The verb is only ever read from a file that has already
        proved it knows the secret."""
        token = prompt.new_token()
        prompt._prepare_dir()
        (consent_dir / token).write_text(f"{token} {verb}")
        assert prompt._answer(token) == verb

        (consent_dir / token).write_text(f"{prompt.new_token()} {verb}")
        assert prompt._answer(token) is None, "a verb does not excuse a wrong token"

    def test_an_unknown_verb_is_silence_not_a_yes(self, consent_dir):
        """A newer helper writing a word an older daemon does not know must read
        as no answer. Silence already fails closed; guessing would not."""
        token = prompt.new_token()
        prompt._prepare_dir()
        (consent_dir / token).write_text(f"{token} maybe")
        assert prompt._answer(token) is None

    def test_a_token_is_spent_once(self, consent_dir):
        token = prompt.new_token()
        prompt._prepare_dir()
        (consent_dir / token).write_text(token)
        prompt._clear(token)
        assert prompt._answer(token) is None

    def test_the_directory_is_not_world_readable(self, consent_dir):
        prompt._prepare_dir()
        assert consent_dir.stat().st_mode & 0o077 == 0


class TestTheNotificationComesDown:
    """`-u critical` has no expiry and a click does not dismiss it (F26), so
    the dismissal is the only thing that ever takes it off the screen."""

    @pytest.mark.anyio
    async def test_it_is_dismissed_after_an_answer(self, commands, quiet_notifications):
        sent, dismissed = quiet_notifications
        ctx = Ctx(can_send_request=True, caps=Caps(Elicitation(form=object())), reply=Reply("accept"))

        await gate.authorize(
            guarded(commands), [], perms=asking(), ctx=ctx, log=LOG, offload=offload
        )

        assert len(dismissed) == 1
        assert dismissed[0] in sent[0]["headline"]

    @pytest.mark.anyio
    async def test_it_is_dismissed_after_a_timeout(
        self, commands, quiet_notifications, consent_dir
    ):
        _, dismissed = quiet_notifications
        with anyio.fail_after(5):
            await gate.authorize(
                guarded(commands), [], perms=asking(), ctx=Ctx(), log=LOG, offload=offload
            )
        assert len(dismissed) == 1

    @pytest.mark.anyio
    async def test_it_is_dismissed_when_the_call_is_cancelled(
        self, commands, quiet_notifications, consent_dir
    ):
        """The likeliest way to reach the dismissal is a cancellation -- the
        client hung up. An unshielded finally would be cancelled before it ran,
        and a notification with no expiry would sit there forever."""
        _, dismissed = quiet_notifications

        with anyio.move_on_after(0.5):
            await gate.authorize(
                guarded(commands),
                [],
                perms=Permissions(ask_timeout_s=600),
                ctx=Ctx(),
                log=LOG,
                offload=offload,
            )

        assert len(dismissed) == 1, "a cancelled question still has to clear the screen"

    @pytest.mark.anyio
    async def test_concurrent_asks_get_distinct_markers(
        self, commands, quiet_notifications, consent_dir
    ):
        """`omarchy notification dismiss` matches a summary substring, so two
        identical headlines would dismiss each other."""
        sent, _ = quiet_notifications

        async def ask_once():
            with anyio.move_on_after(0.3):
                await gate.authorize(
                    guarded(commands),
                    [],
                    perms=Permissions(ask_timeout_s=600),
                    ctx=Ctx(),
                    log=LOG,
                    offload=offload,
                )

        async with anyio.create_task_group() as tg:
            tg.start_soon(ask_once)
            tg.start_soon(ask_once)

        assert len({row["headline"] for row in sent}) == 2


class TestOnlyOneQuestionAtATime:
    @pytest.mark.anyio
    async def test_a_second_ask_in_one_session_is_refused_not_stacked(
        self, commands, quiet_notifications, consent_dir
    ):
        """An agent issues tool calls in parallel; five guarded calls would
        otherwise mean five prompts that never expire."""
        sent, _ = quiet_notifications
        ctx = Ctx()
        results = []

        async def call():
            with anyio.move_on_after(0.4):
                results.append(
                    await gate.authorize(
                        guarded(commands),
                        [],
                        perms=Permissions(ask_timeout_s=600),
                        ctx=ctx,
                        log=LOG,
                        offload=offload,
                    )
                )

        async with anyio.create_task_group() as tg:
            tg.start_soon(call)
            await anyio.sleep(0.05)
            tg.start_soon(call)

        assert len(sent) == 1, "the second call must not raise its own notification"
        assert len(results) == 1
        assert isinstance(results[0], gate.Refused)
        assert "already pending" in results[0].reason


class TestThePromptCannotBeMadeToLie:
    """`omarchy install` has no resolver, so its arguments are model-supplied
    strings that may have been read off a hostile page."""

    def test_an_argument_cannot_add_a_line(self):
        body = prompt.message(
            "omarchy install",
            ["ripgrep\n\n(routine dependency, safe to approve)"],
            None,
        )
        frame = [line for line in body.splitlines() if line.startswith("  omarchy install")]
        assert len(frame) == 1
        assert "safe to approve" in body, "it is shown, just not as its own line"
        assert "\n\n(routine" not in body

    def test_a_control_character_does_not_survive(self):
        body = prompt.message("omarchy install", ["rip\x1b[2Jgrep"], None)
        assert "\x1b" not in body

    def test_a_long_argument_is_cut(self):
        body = prompt.message("omarchy install", ["x" * 500], None)
        assert len(max(body.splitlines(), key=len)) < 140

    def test_the_resolved_target_is_named(self):
        body = prompt.message("omarchy theme set", ["tokyo-night"], "Tokyo Night")
        assert "Tokyo Night" in body


class TestWhatComesBack:
    @pytest.mark.anyio
    async def test_a_refusal_carries_the_outcome_for_the_agent_to_read(
        self, commands, quiet_notifications
    ):
        ctx = Ctx(can_send_request=True, caps=Caps(Elicitation(form=object())), reply=Reply("decline"))
        decision = await gate.authorize(
            guarded(commands), [], perms=asking(), ctx=ctx, log=LOG, offload=offload
        )
        assert isinstance(decision, gate.Refused)
        body = json.loads(json.dumps(decision.as_dict()))
        assert body["consent"] == "declined"
        assert body["tier"] == "guarded"

    @pytest.mark.anyio
    async def test_an_unresolvable_argument_never_reaches_a_person(
        self, commands, quiet_notifications, monkeypatch
    ):
        """Answering yes to a question that then fails spends the user's
        attention for nothing."""
        sent, _ = quiet_notifications
        cmd = commands["omarchy theme remove"]
        ctx = Ctx(can_send_request=True, caps=Caps(Elicitation(form=object())), reply=Reply("accept"))

        decision = await gate.authorize(
            cmd, ["Tokoy Night"], perms=asking(), ctx=ctx, log=LOG, offload=offload
        )

        assert isinstance(decision, gate.Refused)
        assert decision.as_dict()["unresolved"] == "theme"
        assert ctx.elicited == []
        assert sent == []

    @pytest.mark.anyio
    async def test_what_the_user_typed_is_not_thrown_away(self, commands, quiet_notifications):
        """The reason to prefer a form over a confirm dialog is that it can
        carry an answer, so the accept path must not discard one."""
        answer = await consent.ask(
            lambda: _reply_with(Reply("accept", {"note": "only this once"})),
            what="omarchy install",
        )
        assert answer.accepted
        assert answer.data == {"note": "only this once"}


async def _reply_with(reply):
    return reply


class TestAlways:
    """The second answer, and the one a notification cannot carry."""

    @pytest.fixture
    def local(self, tmp_path, monkeypatch):
        path = tmp_path / "permissions.local.json"
        monkeypatch.setattr(gate, "PERMISSIONS_LOCAL_FILE", path)
        return path

    def _ctx(self, always):
        return Ctx(
            can_send_request=True,
            caps=Caps(Elicitation(form=object())),
            reply=Reply("accept", {"always": True} if always else None),
        )

    @pytest.mark.anyio
    async def test_a_plain_accept_writes_nothing(self, commands, local, quiet_notifications):
        decision = await gate.authorize(
            guarded(commands), [], perms=asking(), ctx=self._ctx(False),
            log=LOG, offload=offload,
        )
        assert isinstance(decision, gate.Allowed)
        assert not local.exists(), "answering once must not grant forever"

    @pytest.mark.anyio
    async def test_always_writes_the_rule_and_still_runs_this_call(
        self, commands, local, quiet_notifications
    ):
        cmd = guarded(commands)
        decision = await gate.authorize(
            cmd, [], perms=asking(), ctx=self._ctx(True), log=LOG, offload=offload
        )
        assert isinstance(decision, gate.Allowed)
        assert decision.consent == "accepted"

        written = permissions.load((local.with_name("permissions.json"), local))
        assert [r.matcher for r in written.rules] == [cmd.route]

    @pytest.mark.anyio
    async def test_the_next_call_no_longer_asks(self, commands, local, quiet_notifications):
        sent, _ = quiet_notifications
        cmd = guarded(commands)
        await gate.authorize(
            cmd, [], perms=asking(), ctx=self._ctx(True), log=LOG, offload=offload
        )
        raised = len(sent)

        # What the reloader will hand the gate a moment later.
        written = permissions.load((local.with_name("permissions.json"), local))
        decision = await gate.authorize(
            cmd, [], perms=written, ctx=self._ctx(False), log=LOG, offload=offload
        )
        assert isinstance(decision, gate.Allowed)
        assert decision.consent is None, "nobody was asked, so nothing was answered"
        assert len(sent) == raised, "and no second prompt was raised"

    @pytest.mark.anyio
    async def test_a_grant_that_cannot_be_written_refuses_the_call(
        self, commands, local, quiet_notifications, monkeypatch
    ):
        """The pool is the authority at the moment of execution. Running the
        command because a click was in flight is indefensible."""
        cmd = guarded(commands)

        def refuse(*args, **kwargs):
            raise permissions.GrantRefused("a deny rule appeared while you were deciding.")

        monkeypatch.setattr(permissions, "grant", refuse)
        decision = await gate.authorize(
            cmd, [], perms=asking(), ctx=self._ctx(True), log=LOG, offload=offload
        )
        assert isinstance(decision, gate.Refused)
        assert "deny rule appeared" in decision.reason
        assert "refused rather than run" in decision.reason

    @pytest.mark.anyio
    async def test_a_grant_is_recorded_in_the_activity_log(
        self, commands, local, quiet_notifications, monkeypatch
    ):
        """The most consequential thing a person does in this UI, and the file
        it lands in says that it was granted but not when."""
        noted = []
        monkeypatch.setattr(gate.activity, "note", lambda name, **f: noted.append((name, f)))

        cmd = guarded(commands)
        await gate.authorize(
            cmd, [], perms=asking(), ctx=self._ctx(True), log=LOG, offload=offload
        )

        assert noted == [("permission", {"verb": "allow", "route": cmd.route, "via": "panel"})]

    @pytest.mark.anyio
    @pytest.mark.parametrize("data", [None, {}, {"always": False}, {"always": "yes"}, "always"])
    async def test_anything_that_is_not_the_flag_is_a_plain_accept(
        self, commands, local, quiet_notifications, data
    ):
        """`data` is whatever the asker handed back, and an eliciting client's is
        a model the user filled in."""
        ctx = Ctx(
            can_send_request=True,
            caps=Caps(Elicitation(form=object())),
            reply=Reply("accept", data),
        )
        decision = await gate.authorize(
            guarded(commands), [], perms=asking(), ctx=ctx, log=LOG, offload=offload
        )
        assert isinstance(decision, gate.Allowed)
        assert not local.exists()


class TestThePanelIsToldWhatIsAsked:
    """The panel is the second surface the question appears on, and it learns
    about it from a frame rather than a file it polls."""

    @pytest.fixture
    def emitted(self, monkeypatch):
        seen = []
        monkeypatch.setattr(gate.frames, "asking", lambda *a: seen.append(("asking", a)))
        monkeypatch.setattr(gate.frames, "answered", lambda *a: seen.append(("answered", a)))
        return seen

    @pytest.mark.anyio
    async def test_the_frame_carries_what_the_question_says(
        self, commands, emitted, quiet_notifications
    ):
        cmd = commands["omarchy theme remove"]
        await gate.authorize(
            cmd, ["Tokyo Night"], perms=asking(), ctx=Ctx(), log=LOG, offload=offload
        )

        kind, (token, route, args, target, marker) = emitted[0]
        assert kind == "asking"
        assert token, "the panel cannot answer without it"
        assert route == cmd.route
        assert args == ["Tokyo Night"]
        assert target == "Tokyo Night", "consent that cannot see the target is not consent"
        assert marker

        # The frame exposes nothing the notification did not: it carries the
        # same resolved call, which is what makes it a justified exception to
        # `frames.py`'s no-arguments rule rather than a quiet relaxation of it.
        body = prompt.message(route, args, target)
        assert all(repr(a) in body for a in args)
        assert target in body

    @pytest.mark.anyio
    async def test_the_question_is_closed_however_it_closes(
        self, commands, emitted, quiet_notifications
    ):
        await gate.authorize(
            guarded(commands), [], perms=asking(), ctx=Ctx(), log=LOG, offload=offload
        )
        kinds = [kind for kind, _ in emitted]
        assert kinds == ["asking", "answered"]
        assert emitted[1][1][0] == emitted[0][1][4], "same marker, so the right panel clears"
        assert emitted[1][1][1] == "timed_out"

    @pytest.mark.anyio
    async def test_an_eliciting_client_raises_no_panel_question(
        self, commands, emitted, quiet_notifications
    ):
        """It answers in its own UI. A panel showing a question it cannot answer
        is worse than showing none."""
        ctx = Ctx(
            can_send_request=True,
            caps=Caps(Elicitation(form=object())),
            reply=Reply("accept"),
        )
        await gate.authorize(
            guarded(commands), [], perms=asking(), ctx=ctx, log=LOG, offload=offload
        )
        assert emitted == []


class TestItStopsAskingEventually:
    """A reflexive click is not consent, so a question that has been put enough
    times is not put again."""

    def _ctx(self, action):
        return Ctx(
            can_send_request=True,
            caps=Caps(Elicitation(form=object())),
            reply=Reply(action) if action else None,
        )

    @pytest.mark.anyio
    async def test_a_refused_route_is_not_asked_about_twice(
        self, commands, quiet_notifications
    ):
        sent, _ = quiet_notifications
        cmd = guarded(commands)

        first = await gate.authorize(
            cmd, [], perms=asking(), ctx=self._ctx("decline"), log=LOG, offload=offload
        )
        assert isinstance(first, gate.Refused)
        assert first.outcome == "declined"
        raised = len(sent)

        second = await gate.authorize(
            cmd, [], perms=asking(), ctx=self._ctx("accept"), log=LOG, offload=offload
        )
        assert isinstance(second, gate.Refused)
        assert second.outcome == "not_asked_again"
        assert len(sent) == raised, "no second notification reached the desktop"

    @pytest.mark.anyio
    async def test_nothing_is_resolved_or_spawned_for_a_suppressed_call(
        self, commands, quiet_notifications, monkeypatch
    ):
        """It costs no subprocess and none of the user's attention."""
        cmd = commands["omarchy theme remove"]
        await gate.authorize(
            cmd, ["Tokyo Night"], perms=asking(), ctx=self._ctx("decline"),
            log=LOG, offload=offload,
        )

        monkeypatch.setattr(
            gate.resolve, "resolve_call", lambda *a: pytest.fail("resolved a suppressed call")
        )
        decision = await gate.authorize(
            cmd, ["Tokyo Night"], perms=asking(), ctx=self._ctx("accept"),
            log=LOG, offload=offload,
        )
        assert isinstance(decision, gate.Refused)

    @pytest.mark.anyio
    async def test_an_accepted_route_may_be_asked_about_again(
        self, commands, quiet_notifications
    ):
        """Otherwise saying yes once would cost the next legitimate question."""
        cmd = guarded(commands)
        first = await gate.authorize(
            cmd, [], perms=asking(), ctx=self._ctx("accept"), log=LOG, offload=offload
        )
        assert isinstance(first, gate.Allowed)

        second = await gate.authorize(
            cmd, [], perms=asking(), ctx=self._ctx("accept"), log=LOG, offload=offload
        )
        assert isinstance(second, gate.Allowed)

    @pytest.mark.anyio
    async def test_another_route_is_unaffected(self, commands, quiet_notifications):
        await gate.authorize(
            guarded(commands), [], perms=asking(), ctx=self._ctx("decline"),
            log=LOG, offload=offload,
        )
        decision = await gate.authorize(
            commands["omarchy system reboot"], [], perms=asking(),
            ctx=self._ctx("accept"), log=LOG, offload=offload,
        )
        assert isinstance(decision, gate.Allowed)

    @pytest.mark.anyio
    async def test_a_timeout_counts_as_a_no(self, commands, quiet_notifications):
        """Re-asking an empty room is the purest form of nagging."""
        cmd = guarded(commands)
        first = await gate.authorize(
            cmd, [], perms=asking(), ctx=Ctx(), log=LOG, offload=offload
        )
        assert first.outcome == "timed_out"

        second = await gate.authorize(
            cmd, [], perms=asking(), ctx=self._ctx("accept"), log=LOG, offload=offload
        )
        assert second.outcome == "not_asked_again"

    @pytest.mark.anyio
    async def test_a_stream_of_yeses_still_stops(self, commands, quiet_notifications):
        """The case a decline-keyed rule cannot see: every prompt approved, and
        the user habituated exactly as thoroughly."""
        from omarchy_mcp.cooldown import BURST_LIMIT

        routes = [
            c.route
            for c in commands.values()
            if base_tier(c) is Tier.GUARDED and c.route not in ("omarchy update lock",)
        ][: BURST_LIMIT + 1]
        assert len(routes) > BURST_LIMIT, "the fixture must hold enough guarded routes"

        outcomes = []
        for route in routes:
            decision = await gate.authorize(
                commands[route], [], perms=asking(), ctx=self._ctx("accept"),
                log=LOG, offload=offload,
            )
            outcomes.append(
                decision.outcome if isinstance(decision, gate.Refused) else "allowed"
            )

        assert outcomes[:BURST_LIMIT] == ["allowed"] * BURST_LIMIT
        assert outcomes[BURST_LIMIT] == "not_asked_again"

    @pytest.mark.anyio
    async def test_an_allowed_route_never_meets_any_of_this(
        self, commands, quiet_notifications
    ):
        """The guard is on *asking*. A rule that already says yes asks nobody,
        so it cannot be suppressed by how much asking has gone on."""
        from omarchy_mcp.cooldown import BURST_LIMIT

        for i in range(BURST_LIMIT + 2):
            gate._cooldowns.asked(f"omarchy filler {i}")

        cmd = guarded(commands)
        decision = await gate.authorize(
            cmd, [], perms=allowing(cmd.route), ctx=self._ctx(None), log=LOG, offload=offload
        )
        assert isinstance(decision, gate.Allowed)
        assert decision.consent is None


class TestWhatTheNotificationTellsThePerson:
    """There is no button to look for. `omarchy notification send` passes an
    empty actions array and rides the click command in a hint, so the whole
    toast is the target (F25) -- and a person told to "click", seeing nothing
    that looks clickable, reasonably concludes there is nothing to click. That
    happened the first time this was used on a real desktop (F31)."""

    @pytest.mark.anyio
    async def test_it_says_what_clicking_does(self, commands, quiet_notifications):
        """And what it does is open the panel, not approve. A click on a toast
        must not be able to grant anything."""
        sent, _ = quiet_notifications
        await gate.authorize(
            guarded(commands), [], perms=asking(), ctx=Ctx(), log=LOG, offload=offload
        )
        body = sent[0]["body"]
        assert "open the panel" in body
        assert "bar opens the same panel" in body, "summoning can fail; say the other way in"

    @pytest.mark.anyio
    async def test_clicking_the_notification_cannot_approve_anything(
        self, commands, quiet_notifications, consent_dir
    ):
        """The whole point of the change. What `--exec` runs opens a panel, and
        the token is not in that argv at all -- it reaches the shell on a frame
        and comes back from a button."""
        from omarchy_mcp import prompt

        sent, _ = quiet_notifications
        await gate.authorize(
            guarded(commands), [], perms=asking(), ctx=Ctx(), log=LOG, offload=offload
        )
        token = sent[0]["token"]
        assert token, "there is still a question, so there is still a token"

        argv = ["omarchy", "notification", "send", "-u", "critical", "h", "b", "--exec"]
        argv += list(prompt.SUMMON)
        assert token not in argv, "the one-time secret has one path, not two"
        assert "summon" in prompt.SUMMON
        assert "approve" not in prompt.SUMMON

    @pytest.mark.anyio
    async def test_it_names_the_surface_that_has_the_other_answers(
        self, commands, quiet_notifications
    ):
        """The notification can express yes and nothing else. A user who never
        learns where Always lives has no way to stop being asked every time."""
        sent, _ = quiet_notifications
        await gate.authorize(
            guarded(commands), [], perms=asking(), ctx=Ctx(), log=LOG, offload=offload
        )
        body = sent[0]["body"]
        assert "panel" in body
        assert "Always" in body and "Deny" in body

    @pytest.mark.anyio
    async def test_it_still_says_that_silence_refuses(self, commands, quiet_notifications):
        sent, _ = quiet_notifications
        await gate.authorize(
            guarded(commands), [], perms=asking(), ctx=Ctx(), log=LOG, offload=offload
        )
        assert "refuses it" in sent[0]["body"]

    @pytest.mark.anyio
    async def test_an_eliciting_client_is_not_told_to_click_a_desktop(
        self, commands, quiet_notifications
    ):
        """It answers in its own UI; there is no token and nothing to click."""
        sent, _ = quiet_notifications
        ctx = Ctx(
            can_send_request=True,
            caps=Caps(Elicitation(form=object())),
            reply=Reply("accept"),
        )
        await gate.authorize(
            guarded(commands), [], perms=asking(), ctx=ctx, log=LOG, offload=offload
        )
        assert "notification" not in sent[0]["body"]
        assert "MCP client" in sent[0]["body"]
