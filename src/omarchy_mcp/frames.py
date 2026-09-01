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
