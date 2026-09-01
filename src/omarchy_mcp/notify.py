"""Telling the person at the desktop something about the daemon itself.

The only client of this daemon is a language model, and the only person who
cares is looking at a desktop rather than a terminal. Daemon-level facts --
the config was rejected, the rules an agent runs under just changed -- have
nowhere else to go: stderr reaches `journalctl`, which nobody is watching.

Tool-call failures do **not** come through here. The agent already has the
error in its response, and a wrong `omarchy_run` would toast constantly.

`prompt.py` sends its own notifications because a consent prompt is a different
thing: it is critical, it carries an `--exec` action, and it is dismissed by
hand later. This is for the ones that are only ever read.
"""

from __future__ import annotations

from . import execute

#: Long enough for a busy notification daemon, short enough that nothing waits
#: on it. Nothing here is worth delaying a reload for.
TIMEOUT_MS = 5_000


def send(headline: str, body: str, *, urgency: str = "normal", log=None) -> None:
    """Raise a notification. Never raises: this is the reporting path.

    A failure here means the desktop could not be told, which is worth a line
    in the log and nothing more -- the caller is usually in the middle of
    reporting some *other* problem, and a raise would replace it.
    """
    try:
        execute.run(
            ["omarchy", "notification", "send", "-u", urgency, headline, body],
            timeout_ms=TIMEOUT_MS,
            max_output_b=4096,
        )
    except Exception as exc:  # reporting path: never let it become the failure
        if log is not None:
            log.warning("could not send a notification (%s): %s", exc, headline)
