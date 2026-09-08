"""Putting the question where the person actually is.

`consent.py` owns the rule -- what an answer means, and what silence means.
This owns the two ways a question reaches a human, and the notification that
sits beside both of them.

The desktop asker exists because MCP elicitation does not reach this client.
Claude Code negotiates protocol ``2026-07-28``, whose transport carries no
server-initiated requests at all, so ``ctx.elicit`` fails inside this process
before anything reaches the wire (F23). A notification's ``--exec`` does reach
the person, in every protocol era and for every client, and it puts the question
on the desktop this project exists to drive rather than in a terminal the user
may not be looking at.

It has exactly one action (F25), and **that action is no longer the answer**.

It used to be: a click ran the consent helper and meant *yes*. That was elegant
-- click is yes, silence is no, the mechanism and the rule agreeing by
construction -- and it was wrong in a way only a real desktop showed (F31).
There is no button to look for, because `omarchy-notification-send` sends an
empty actions array; a person told to "click to approve", seeing nothing that
looks clickable, reasonably concludes there is nothing to click.

So a click now **opens the panel**, and the answer is given there. Three things
that buys, in the order they matter:

- **A click is navigation, not consent.** Approving takes a deliberate press of
  a labelled button. A reflexive click on a toast can no longer grant anything,
  which is the same worry N14 exists for, closed at the source.
- **All three answers are in one place.** The notification could only ever say
  *yes*; *Always* and *Deny* live in the panel, and a user who never found the
  panel had no way to stop being asked the same thing every time.
- **The token never rides on a notification.** It reaches the shell on the
  `asking` frame and comes back from the panel, so the one-time secret has one
  path instead of two.

Silence still refuses, unchanged. And if the panel cannot be summoned for any
reason, the bar icon opens the same panel by hand -- the notification says so.

The panel is the second surface, and it has room for the two answers a
notification cannot carry: *yes, always* and *no*. It writes through the same
one-time token, in the same directory, via the same helper -- see `VERBS`. One
channel, one validator, one unguessable secret; a second way in would be a second
thing to get right.
"""

from __future__ import annotations

import itertools
import os
import secrets
from contextlib import asynccontextmanager
from dataclasses import dataclass
from pathlib import Path

import anyio

from . import execute
from .paths import CONSENT_DIR, PLUGIN_ID

#: How often the parked call looks for a clicked token. Short enough that a
#: click feels immediate, long enough that a minute of waiting is 240 stats
#: calls on one file rather than a busy loop.
POLL_INTERVAL_S = 0.25

#: Enough to be unguessable, short enough to fit a filename and an argv.
TOKEN_BYTES = 16

#: What to do about the question, on the surface that carries it.
#:
#: "Click to approve" was the first wording and it was not enough. There is no
#: button to look for: `omarchy notification send` passes an **empty actions
#: array** and rides the click command in an `omarchy-exec-argv` hint, so the
#: whole toast is the target (F25). A person told to click, seeing nothing that
#: looks clickable, reasonably concludes there is nothing to click -- which is
#: what happened the first time this was used on a real desktop (F31).
#:
#: It also has to name the panel. The notification can express *yes* and nothing
#: else; **Always** and **Deny** live in the bar, and a user who never learns
#: that has no way to stop being asked the same question every time.
DESKTOP_HINT = (
    "Click this notification to open the panel, where you can Allow once, "
    "Always, or Deny.\n"
    "The MCP server icon in the bar opens the same panel.\n"
    "Ignoring this refuses it."
)

#: What a click runs. `summon` rather than `toggle`: a second click on a
#: notification that is still up must not close the panel the first one opened.
#:
#: It targets the *shell*, passing this plugin's id as an argument, so it is not
#: an IPC call on this plugin's own target and `policy.self_refusal` has nothing
#: to say about it. Opening a panel mutates nothing.
SUMMON = ("omarchy-shell", "shell", "summon", PLUGIN_ID, "{}")

#: A control character in an argument could add a line to the message a person
#: reads before clicking. Arguments are model-supplied and may have been copied
#: off a hostile page, so they are flattened before they are shown.
MAX_ARG_CHARS = 80

#: Distinguishes concurrent prompts, because `omarchy notification dismiss`
#: matches a summary *substring* and two identical headlines would dismiss each
#: other (F26).
#:
#: ``itertools.count`` is an endless counter: ``next(_counter)`` yields 1, then
#: 2, and so on for the life of the process. One shared counter at module level,
#: so two prompts can never draw the same number.
_counter = itertools.count(1)


#: The vocabulary the answer file may carry, and what each word means.
#:
#: `once` is the bare token, which is what a notification click writes and what
#: this channel has always meant. The other two exist because N6's panel has room
#: for buttons a notification does not: it carries exactly one action (F25), so
#: it can express *yes* but not *yes, always* and not *no*.
#:
#: Anything not in here is **no answer at all** -- not a yes. A newer helper
#: writing a word an older daemon does not know must read as silence, and
#: silence already fails closed.
#: Each value is (what the answer means, whether to remember it).
VERBS = {
    "once": ("accept", False),
    "always": ("accept", True),
    "deny": ("decline", False),
}


@dataclass(frozen=True)
class Clicked:
    """What the desktop asker returns, shaped like an elicitation result.

    `consent._interpret` reads `action`, so the two askers hand back the same
    thing and the rule does not learn which surface answered. `always` rides in
    `data` rather than being a fourth outcome: it is a rider on an accept, not a
    different way of leaving the question.
    """

    action: str = "accept"
    data: object | None = None


def _flatten(text: str) -> str:
    """Strip an untrusted string down to something that cannot forge a line."""
    # ``isprintable()`` is False for newlines, tabs and every other control
    # character, which is exactly the set that could forge a line in the message
    # a person reads. Joining the survivors rebuilds the string without them.
    clean = "".join(ch for ch in text if ch.isprintable())
    if len(clean) > MAX_ARG_CHARS:
        # One character short of the cap, to leave room for the ellipsis.
        clean = clean[: MAX_ARG_CHARS - 1] + "…"
    return clean


def message(route: str, args: list[str], target: str | None) -> str:
    """The text a person reads before deciding.

    Assembled from a fixed frame. `omarchy install` has no resolver by design --
    package names have no local truth -- so its arguments are model-supplied
    strings that may have been read off a web page by `omarchy_screen_text`, and
    they land in front of a human who is about to approve something. Every one
    is flattened and quoted, so nothing an argument contains can add a line or
    counterfeit the frame around it.
    """
    # ``repr`` puts quotes round each argument and makes any remaining oddity
    # visible as an escape, so an argument cannot pass itself off as part of the
    # frame around it.
    shown = " ".join(repr(_flatten(a)) for a in args)
    lines = [
        "An agent is asking to run a guarded command.",
        "",
        f"  {route}" + (f" {shown}" if shown else ""),
        "",
    ]
    lines.append(f"Target: {target}" if target else "Target: not resolvable to a known object")
    lines.append("")
    lines.append("Approve only if you asked for this.")
    return "\n".join(lines)


def new_token() -> str:
    """A fresh one-time secret for one question. Never given to the model."""
    return secrets.token_urlsafe(TOKEN_BYTES)


def _path(token: str) -> Path:
    """Where the answer to this question would be written."""
    return CONSENT_DIR / token


def _prepare_dir() -> None:
    """Make sure the consent directory exists and only this user can see it."""
    CONSENT_DIR.mkdir(parents=True, exist_ok=True)
    # ``0o700`` is owner-only, including the execute bit that a directory needs
    # to be entered at all. Set every time rather than only at creation, in case
    # something loosened it since.
    os.chmod(CONSENT_DIR, 0o700)


def _answer(token: str) -> str | None:
    """Which of `VERBS` was written, or ``None`` for no answer yet.

    The agent is the untrusted party here, and `omarchy_run` passes arguments to
    hundreds of commands this project did not write. If the existence of a path
    were consent, an agent that talked any one of them into writing a file would
    approve its own guarded call. So the token is both the name and the
    contents, and both have to match -- the verb is only ever read from a file
    that has already proved it knows the secret.
    """
    try:
        body = _path(token).read_text().strip()
    except OSError:
        # No file yet -- which is the normal case while the question is open --
        # or one that cannot be read. Neither is an answer.
        return None
    if body == token:
        # The bare token on its own: the original "yes, once".
        return "once"
    # Otherwise "<token> <verb>". Splitting on the first space keeps the check
    # on the token exact.
    head, _, verb = body.partition(" ")
    if head != token:
        return None
    return verb if verb in VERBS else None


def _clear(token: str) -> None:
    """A token is spent once. A leftover file must never approve a later ask."""
    try:
        # ``unlink`` is delete. It raises when the file is not there, which is
        # the ordinary case for a question nobody answered.
        _path(token).unlink()
    except OSError:
        pass


def send(headline: str, body: str, *, token: str | None = None) -> None:
    """Raise the notification.

    A `token` means there is a question to put to somebody, so the toast is made
    clickable -- and clicking it opens the panel rather than answering. The
    token itself is **not** in this argv: it reaches the shell on the `asking`
    frame and comes back through the helper when a button is pressed. A
    one-time secret with one path is easier to reason about than one with two,
    and `/proc/<pid>/cmdline` is world-readable.
    """
    argv = ["omarchy", "notification", "send", "-u", "critical", headline, body]
    if token is not None:
        argv += ["--exec", *SUMMON]
    execute.run(argv, timeout_ms=5_000, max_output_b=4096)


def dismiss(marker: str) -> None:
    """Take it off the screen. `marker` is the unique part of the headline."""
    execute.run(
        ["omarchy", "notification", "dismiss", marker], timeout_ms=5_000, max_output_b=4096
    )


# ``@asynccontextmanager`` is ``@contextmanager``'s async twin: the same
# yield-in-the-middle shape, used with ``async with`` and able to ``await``
# on both sides of the yield.
@asynccontextmanager
async def pending(label: str, body: str, *, token: str | None, log, offload):
    """Hold a critical notification up for as long as the question is open.

    A `-u critical` notification has no expiry, and a click does not dismiss it
    either (F26), so the dismissal is the only thing that ever takes it down. It
    runs under a shielded cancel scope because the most likely way to reach it
    is a cancellation -- the client disconnected, or the deadline passed -- and
    an unshielded `finally` would be cancelled before it did anything, leaving a
    prompt on the screen forever.
    """
    marker = f"(#{next(_counter)})"
    headline = f"Approval needed: {label} {marker}"
    hint = DESKTOP_HINT if token else "Answer in your MCP client."
    if token is not None:
        await offload(_prepare_dir)
    await offload(send, headline, f"{body}\n\n{hint}" if body else hint, token=token)
    log.info("consent asked: %s %s", label, marker)
    try:
        # The question is now on screen. The caller's ``async with`` body waits
        # for an answer; control returns here whichever way that ends.
        yield marker
    finally:
        # A shielded scope cannot be cancelled, so these two awaits run to
        # completion even when the reason for getting here is that everything
        # around them was cancelled. Without the shield, a cancelled call would
        # leave its critical notification on screen for good.
        with anyio.CancelScope(shield=True):
            await offload(dismiss, marker)
            if token is not None:
                await offload(_clear, token)


async def desktop_ask(token: str) -> Clicked:
    """Wait for an answer. Returns only on a real one; the deadline is elsewhere.

    Deliberately has no timeout of its own. `consent.ask` owns the deadline for
    both askers, so there is one place where "no answer means denied" is
    implemented and one place to read it.
    """
    while True:
        # Reading a file is blocking, so it goes to a worker thread; the event
        # loop stays free for every other client while this one waits.
        verb = await anyio.to_thread.run_sync(_answer, token)
        if verb is not None:
            action, always = VERBS[verb]
            return Clicked(action, {"always": True} if always else None)
        # ``await`` on a sleep yields the loop rather than blocking it. This
        # loop has no exit of its own: it is cancelled from outside, by the
        # deadline in `consent.ask`.
        await anyio.sleep(POLL_INTERVAL_S)
