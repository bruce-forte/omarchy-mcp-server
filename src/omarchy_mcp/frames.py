"""The one thing this process says on stdout.

One JSON object per line, read by `Service.qml` with a `SplitParser`.
Everything else -- the log, the warnings, uvicorn's own noise -- goes to
stderr, so stdout stays a channel a machine can parse. That split is what lets
`journalctl --user -f` carry the daemon's log while the shell reads its state.

Two kinds of line:

`listening` and `failed` are lifecycle, printed once each from the main thread.

`call` says an agent just did something. It exists because the widget otherwise
learns of a tool call from the `/health` poll up to ten seconds later, by which
time the desktop has already changed in front of the user and the bar is the
last thing to know. This is the only channel on which "something is happening
right now" travels; the activity log is the one that keeps it afterwards.

**A call frame carries the tool name and how the call ended, and nothing
else.** No arguments, no output. The activity log's boundary (N5) is that
arguments are the user's screen rather than the agent's action -- a clipboard
write's argument *is* the clipboard -- and a channel the shell reads into a bar
widget is the last place to relax it. The log keeps arguments behind a 0600
file; this does not carry them at all.

`asking` and `answered` are the deliberate exception, and it is worth saying why
rather than letting it look like an oversight. An `asking` frame *must* carry the
route and its arguments: consent that does not show what it is consenting to is
not consent (N2), and the panel is one of the two surfaces where the question is
put. It exposes nothing new -- `prompt.message` already puts those same flattened
arguments on the desktop in the notification. It also carries the one-time token,
which is why `Service.qml` holds it in memory and never writes it to the state
file: it is a live capability for the length of one question.

Call frames are written from request threads, so writes are serialised. Two
threads interleaving a `print` produce a line the shell cannot parse, and the
`SplitParser` on the other end would silently drop it.
"""

from __future__ import annotations

import json
import sys
import threading

_lock = threading.Lock()


def emit(state: str, **fields: object) -> None:
    """Write one frame. Never raises: a tool call must not fail over a pipe.

    The reader is `omarchy-shell`, which consumes continuously. When the daemon
    is run by hand instead, stdout is a terminal and these lines are visible
    there, which is the intended debugging view rather than an accident.
    """
    line = json.dumps({"state": state, **fields})
    try:
        with _lock:
            sys.stdout.write(line + "\n")
            sys.stdout.flush()
    except (OSError, ValueError):
        # A closed or broken stdout means nobody is supervising this process.
        # That is worth surviving rather than reporting: the tool call the
        # frame describes has already happened.
        pass


def call(tool: str, outcome: str) -> None:
    """A tool call finished. See the module docstring for what is not in it."""
    emit("call", tool=tool, outcome=outcome)


def asking(token: str, route: str, args: list[str], target: str | None, marker: str) -> None:
    """A question is on screen, and this is what it says.

    The panel needs all of it: the route and arguments to show, and the token to
    answer with. `marker` is the notification's unique tail, so the panel can say
    which prompt it is looking at when two clients ask at once.
    """
    emit(
        "asking",
        token=token,
        route=route,
        args=list(args),
        target=target,
        marker=marker,
    )


def answered(marker: str, outcome: str) -> None:
    """The question is closed, however it closed.

    A bar surface exists once per screen, so three panels can be showing one
    pending ask. The first answer spends the token; this is what tells the others
    to stop showing a question nobody can answer any more.
    """
    emit("answered", marker=marker, outcome=outcome)


def review(token: str, headline: str, arrivals: int, widened: int, dead: int) -> None:
    """Something changed under the rules and nobody has looked at it yet.

    Counts and a headline; the panel asks the daemon for the rest. The token is
    how the panel acknowledges, and it is published only here -- acknowledging is
    silent by design, so an agent able to do it could clear its own review with
    nothing on screen. `Service.qml` holds it in memory like a consent token.
    """
    emit(
        "review",
        token=token,
        headline=headline,
        arrivals=arrivals,
        widened=widened,
        dead=dead,
    )


def reloaded(tools: int, declared: int, config_ok: bool, permissions_ok: bool = True) -> None:
    """The config file was re-read.

    Counts, never contents. The bar shows "Tools 18 of 19" and whether either
    file was rejected; which tools, and what the rules now say, are answered by
    the files the person just edited.

    Two health flags rather than one. `config.toml` and the permissions document
    fail independently and are fixed in different places, so a single lamp would
    say "something is wrong" and leave the person to guess which.

    Like `call`, this exists so the widget learns now rather than at the next
    ten-second `/health` poll -- the person has just saved a file and is looking
    at the bar to find out whether it took.
    """
    emit(
        "reloaded",
        tools=tools,
        declared=declared,
        config_ok=config_ok,
        permissions_ok=permissions_ok,
    )
