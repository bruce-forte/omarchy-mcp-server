"""When the daemon declines to ask, having asked enough already.

`guardedDefault` is `ask`, which is what makes the permissions document fill
itself through use. It also hands an agent a way to put a `-u critical`
notification on somebody's desktop, over and over, and **a reflexive click is
not consent**. `gate._pending` already stops a flood arriving at once -- one
question per session, answered before the next -- but that bounds concurrency,
not volume, and two attached clients are two sessions.

The original write-up asked for "a per-route cooldown after a decline, and after
N declines in a window, auto-refuse". Building it, those turned out to be two
different problems wearing one coat, and only one of them is habituation:

**Nagging.** The user said no and the agent asked again. This is the concrete
way an agent grinds somebody down, and it is what a per-route cooldown is for.
Every way of *not* accepting counts -- declined, dismissed, timed out -- because
`consent.py` already treats all of them as no, and re-asking an empty room is
the purest form of the thing. Consecutive refusals double the wait, because an
agent still asking after two noes will not stop after the third.

**Habituation.** Volume, regardless of the answer. A decline-keyed rule cannot
see it at all: fifty prompts and fifty clicks contains no declines and is the
worst case there is. So the second mechanism counts *prompts raised*, not
prompts refused, and stops asking for the rest of the window when there have
been too many.

Two things this deliberately is not:

- **Not persisted.** A cooldown is a nag-guard, not a permission. Surviving a
  restart would make it a decision nobody took, and the state directory is for
  things the user chose. It dies with the daemon, and an agent cannot restart
  the daemon (N12).
- **Not clearable from anywhere.** Clearing widens -- it lets the asking start
  again -- so a button for it would need the same token dance as everything else
  that widens, for a mechanism that expires on its own within minutes. The panel
  *shows* the state instead, because a call refused without explanation is the
  actual failure mode here. Silent suppression is a mystery; suppression with a
  line in the panel saying why is a feature.
"""

from __future__ import annotations

import time
from collections import deque
from dataclasses import dataclass, field

#: How long a route waits after one refusal before it may be asked about again.
#: Long enough that an agent's next attempt is a different conversation, short
#: enough that a user who changes their mind is not stuck with their first
#: answer for the afternoon.
FIRST_COOLDOWN_S = 300

#: Each consecutive refusal doubles the wait, to here. An agent still asking
#: after an hour of noes is not going to be talked round by a sixth prompt.
MAX_COOLDOWN_S = 3600

#: Prompts allowed in `BURST_WINDOW_S` before the daemon stops asking at all.
#: Generous on purpose: setting up a machine legitimately produces a burst, and
#: a limit that fires during ordinary work teaches people to route around the
#: whole mechanism, which is worse than not having it.
BURST_LIMIT = 12
BURST_WINDOW_S = 600


def _plural(seconds: float) -> str:
    minutes = max(1, round(seconds / 60))
    return f"{minutes} minute" + ("" if minutes == 1 else "s")


@dataclass
class _Route:
    refusals: int = 0
    until: float = 0.0


@dataclass
class Cooldowns:
    """Per-route nag-guards and one global burst cap.

    Injectable clock rather than a global one, so the tests can move time
    without sleeping and without patching the module. `time.monotonic` because
    this measures elapsed time and must not care what the wall clock does.
    """

    clock: object = time.monotonic
    _routes: dict[str, _Route] = field(default_factory=dict)
    _asked: deque = field(default_factory=deque)

    def _now(self) -> float:
        return self.clock()

    def refusal(self, route: str) -> str | None:
        """Why this route must not be asked about right now, or ``None``.

        Checked before a question is put together, so a suppressed call costs no
        notification, no resolver subprocess, and none of the user's attention.
        """
        now = self._now()
        self._forget(now)

        entry = self._routes.get(route)
        if entry is not None and entry.until > now:
            left = _plural(entry.until - now)
            answers = "answer" if entry.refusals == 1 else "answers"
            return (
                f"`{route}` was put to the user {entry.refusals} time"
                f"{'' if entry.refusals == 1 else 's'} without a yes, so it will not "
                f"be asked about again for {left}. That is {entry.refusals} "
                f"{answers} of no, however they were given -- refused, dismissed, or "
                f"nobody there. Do not keep trying: ask the user directly, or have "
                f'them add {{"kind": "route", "matcher": "{route}"}} to the "allow" '
                f"list in ~/.config/omarchy/mcp/permissions.json."
            )

        if len(self._asked) >= BURST_LIMIT:
            left = _plural(self._asked[0] + BURST_WINDOW_S - now)
            return (
                f"{len(self._asked)} approval prompts have been put on the user's "
                f"desktop in the last {_plural(BURST_WINDOW_S)}, which is enough that "
                f"the next one would be answered out of habit rather than read. No "
                f"more will be raised for {left}. Stop and tell the user what you are "
                f"trying to do, so they can allow it once instead of approving it "
                f"twelve times."
            )
        return None

    def asked(self, route: str) -> None:
        """A prompt went up. Counted whatever the answer turns out to be."""
        self._asked.append(self._now())

    def answered(self, route: str, *, accepted: bool) -> None:
        """How it ended. Anything but a yes starts or lengthens the cooldown."""
        now = self._now()
        if accepted:
            # An engaged user is the opposite of a habituated one, and the
            # route they just approved is not one they are being nagged about.
            self._routes.pop(route, None)
            return

        entry = self._routes.setdefault(route, _Route())
        entry.refusals += 1
        wait = min(FIRST_COOLDOWN_S * (2 ** (entry.refusals - 1)), MAX_COOLDOWN_S)
        entry.until = now + wait

    def _forget(self, now: float) -> None:
        """Drop prompts that have left the window, and cooldowns that expired."""
        while self._asked and self._asked[0] + BURST_WINDOW_S <= now:
            self._asked.popleft()
        for route in [r for r, e in self._routes.items() if e.until <= now]:
            del self._routes[route]

    def state(self) -> dict[str, object]:
        """What the panel shows, so a suppressed call is never a mystery."""
        now = self._now()
        self._forget(now)
        return {
            "recentPrompts": len(self._asked),
            "promptLimit": BURST_LIMIT,
            "windowSeconds": BURST_WINDOW_S,
            "suppressed": len(self._asked) >= BURST_LIMIT,
            "cooling": [
                {
                    "route": route,
                    "refusals": entry.refusals,
                    "secondsLeft": int(entry.until - now),
                }
                for route, entry in sorted(self._routes.items())
            ],
        }
