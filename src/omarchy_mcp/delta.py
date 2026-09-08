"""What changed under the rules while nobody was looking.

A permission rule is written once and read against a registry that moves. The
document says `omarchy install *` and means *the install prefix*; what that
prefix contains is decided by whichever Omarchy is installed on the day the rule
is consulted. So three things can change without the document changing, and only
one of them is the one the first draft of this feature described:

===========================  =====================================================
new route, no rule matches   Omarchy adds `omarchy backup wipe`
**a rule silently widened**  your `omarchy install *` matched 15, now matches 18
**a rule went dead**         your `deny: omarchy dev *` matches nothing any more
===========================  =====================================================

The second is the one to be afraid of. You reviewed fifteen routes and consented
to fifteen routes; an `omarchy update` can put three more inside the same
sentence, and nothing in the document changed to say so. The third is a silent
loss of protection: a `deny` that stopped matching looks exactly like a `deny`
that is working.

**Restrictions extend forward. Grants do not.** That is the whole rule, and it
is the precedence ladder extended across time:

===============================  ==========================================
a new route matched by `deny`    denied, immediately
a new route matched by `ask`     asks, immediately
**matched only by `allow`**      **held at `ask` until acknowledged**
matched by nothing               the guarded default, or it is simply safe
===============================  ==========================================

So a wildcard keeps meaning what a wildcard means -- the prefix, not the fifteen
routes that existed the day it was typed -- and the three that arrive under it
still cost one click each, once, before they run.

**What is persisted is the route list, not a fingerprint.** A hash says something
changed and cannot say what; every question above is answerable from the previous
route set plus the current rules, and nothing less will do.

**The snapshot advances only on acknowledgement.** Not on startup: otherwise the
notification fires once, the next boot overwrites the snapshot, and the evidence
of what changed is gone before anybody clicked. The one exception is the very
first run, which has nothing to compare against and nothing to tell anyone --
every route is "new" on a fresh install, and reviewing 400 of them is the
catalogue this feature exists to avoid.

The whole comparison is set arithmetic. ``seen`` is a `frozenset` of the routes
last acknowledged, and Python's set operators do the rest: ``set(commands) -
seen`` is what arrived, ``seen - set(commands)`` is what went away. `compute` is
a pure function of (snapshot, registry, rules) with no I/O in it, so a test can
hand it three values and check the answer.
"""

from __future__ import annotations

import json
import os
import secrets
from dataclasses import dataclass, field
from pathlib import Path

from .permissions import Effect, Permissions
from .policy import GUARDED_GROUPS, GUARDED_ROUTES, Tier, base_tier
from .registry import Command

#: Enough to be unguessable, short enough to fit a filename and an argv. Same
#: shape as a consent token, and for the same reason -- see `Review.token`.
TOKEN_BYTES = 16

#: How many routes a notification names before it becomes a count. A critical
#: notification is a paragraph, not a listing; the panel has the rest.
NAMED_IN_NOTIFICATION = 3


@dataclass(frozen=True)
class Arrival:
    """One route that was not here last time, and what happens to it now."""

    route: str
    tier: str
    effect: str
    #: The rule that decided it, if one did.
    rule: str = ""
    source: str = ""
    #: Held at `ask` because only an `allow` rule covers it, and grants do not
    #: extend forward. Clears on acknowledgement.
    quarantined: bool = False
    #: Its group is one this plugin has never classified, so it derives as
    #: `safe` and runs. Reported loudly because that is what decision 4 does not
    #: cover -- `GUARDED_GROUPS` is hand-written, and a group Omarchy invents
    #: tomorrow is not in it.
    unclassified: bool = False


@dataclass(frozen=True)
class Widened:
    """A rule that covers more than it did, without the rule changing."""

    matcher: str
    effect: str
    source: str
    routes: tuple[str, ...]


@dataclass(frozen=True)
class Dead:
    """A rule that covered something last time and covers nothing now.

    A silent loss of protection when it is a `deny`: upstream renamed a route
    out from under it and nothing else says so.
    """

    matcher: str
    effect: str
    source: str
    covered: tuple[str, ...]


@dataclass(frozen=True)
class Review:
    """Everything worth putting in front of a person, and nothing else."""

    arrivals: tuple[Arrival, ...] = ()
    widened: tuple[Widened, ...] = ()
    dead: tuple[Dead, ...] = ()
    gone: tuple[str, ...] = ()
    #: True when there was no snapshot at all. Nothing is reported: every route
    #: is new on a fresh install, and that is the catalogue, not the delta.
    first_run: bool = False
    #: Minted when there is something to acknowledge, and published only on the
    #: frame the shell reads. Acknowledging is silent by design -- it consumes a
    #: warning and raises nothing -- so an agent able to do it could clear its
    #: own review with nothing on screen. The panel knows this; the agent never
    #: sees it. See `SECURITY.md`.
    token: str = field(default="", compare=False)

    def __bool__(self) -> bool:
        """Whether there is anything to review at all.

        Defining ``__bool__`` is what lets a caller write ``if review:`` and
        ``if not review:``; without it every object counts as true.
        """
        return bool(self.arrivals or self.widened or self.dead or self.gone)

    @property
    def quarantined(self) -> frozenset[str]:
        """Routes held at `ask` until this review is acknowledged."""
        return frozenset(a.route for a in self.arrivals if a.quarantined)

    @property
    def headline(self) -> str:
        """One line summarising the review, for the notification and the bar."""
        parts = []
        if self.arrivals:
            parts.append(f"{len(self.arrivals)} new")
        if self.widened:
            parts.append(f"{len(self.widened)} rule{_s(self.widened)} now cover more")
        if self.dead:
            parts.append(f"{len(self.dead)} rule{_s(self.dead)} match nothing")
        return ", ".join(parts) or "nothing to review"

    @property
    def urgent(self) -> bool:
        """Whether this is a `critical` notification rather than an ordinary one.

        A rule that widened without being edited, and a command in a group this
        plugin has never classified, are the two that a person has to see. A
        handful of new guarded routes that will ask anyway can wait for the next
        time the panel is opened.
        """
        return bool(self.widened) or any(a.unclassified for a in self.arrivals)


def _s(items) -> str:
    """The plural "s", or nothing when there is exactly one of something."""
    return "" if len(items) == 1 else "s"


def load_seen(path: Path) -> frozenset[str] | None:
    """The routes last acknowledged, or ``None`` if there is no snapshot.

    ``None`` and "an empty snapshot" are different: the first means a fresh
    install with nothing to review, the second means an Omarchy that reported no
    commands, which is a fault rather than a delta.
    """
    try:
        body = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError):
        # A snapshot that will not parse is a snapshot we do not have. It is an
        # observation of the machine, not a decision of the user's: losing it
        # costs one silent re-baseline, and refusing to start over it would be
        # absurd.
        return None
    routes = body.get("routes")
    if not isinstance(routes, list):
        return None
    return frozenset(str(r) for r in routes)


def save_seen(path: Path, commands: dict[str, Command], *, version: str = "") -> None:
    """Record what has been reviewed. Atomic, for the same reason as the rules."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.tmp")
    body = {
        "_comment": (
            "Routes the Omarchy MCP server has shown you. It compares this against "
            "the installed Omarchy to find what an update added under your rules. "
            "Safe to delete: the next start re-baselines silently."
        ),
        "omarchy": version,
        "routes": sorted(commands),
    }
    # Write-to-temp-then-rename, the standard way to update a file atomically:
    # a reader ever only sees the old file or the new one, never a half-written
    # one, and a crash mid-write cannot destroy what was there.
    with open(tmp, "w") as handle:
        json.dump(body, handle, indent=2)
        handle.write("\n")
        # ``flush`` pushes Python's buffer into the OS; ``fsync`` pushes the
        # OS's buffer onto the disk. Both, in that order, or the rename can land
        # before the contents do.
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(tmp, path)


def new_token() -> str:
    """A fresh acknowledgement token. Published only on the frame, never to an agent."""
    return secrets.token_urlsafe(TOKEN_BYTES)


def compute(
    seen: frozenset[str] | None,
    commands: dict[str, Command],
    perms: Permissions,
) -> Review:
    """What has changed since ``seen``, read through the rules in force now."""
    if seen is None:
        return Review(first_run=True)

    from .permissions import evaluate  # local: permissions does not import this

    # Groups are exactly the second token of a route, so the snapshot carries
    # them without having stored them. ``"omarchy theme set".split()[1]`` is
    # "theme".
    known_groups = frozenset(r.split()[1] for r in seen if len(r.split()) > 1)

    arrivals = []
    # ``set(commands)`` is the set of its *keys* -- the routes -- and ``-`` is
    # set difference, so this is "routes that exist now and did not before".
    # Sorted so the review reads the same way twice.
    for route in sorted(set(commands) - seen):
        cmd = commands[route]
        tier = base_tier(cmd)
        outcome = evaluate(route, tier, perms)
        # A grant does not extend forward. Everything else does: a `deny` on a
        # new route denies it on arrival, which is protection propagating, and an
        # `ask` asks. Only a permission that arrived without anyone seeing it is
        # held back.
        quarantined = (
            outcome.effect is Effect.ALLOW
            and outcome.rule is not None
            and outcome.rule.effect is Effect.ALLOW
        )
        arrivals.append(
            Arrival(
                route=route,
                tier=tier.value,
                effect=Effect.ASK.value if quarantined else outcome.effect.value,
                rule=outcome.rule.matcher if outcome.rule else "",
                source=outcome.rule.source if outcome.rule else "",
                quarantined=quarantined,
                unclassified=_unclassified(cmd, tier, known_groups),
            )
        )

    # Two empty lists in one statement; the right-hand side is a tuple that is
    # unpacked into the two names.
    widened, dead = [], []
    for rule in perms.rules:
        before = tuple(r for r in sorted(seen) if rule.matches(r))
        after = rule.covers(commands.values())
        gained = tuple(r for r in after if r not in seen)
        if gained and before:
            # `before` non-empty on purpose: a rule that matched nothing and now
            # matches something is not a rule that widened, it is one that has
            # started working. Its routes are already in `arrivals`.
            widened.append(Widened(rule.matcher, rule.effect.value, rule.source, gained))
        if before and not after:
            dead.append(Dead(rule.matcher, rule.effect.value, rule.source, before))

    review = Review(
        arrivals=tuple(arrivals),
        widened=tuple(widened),
        dead=tuple(dead),
        gone=tuple(sorted(seen - set(commands))),
    )
    # An empty review needs no token: there is nothing to acknowledge.
    return review if not review else _with_token(review)


def _with_token(review: Review) -> Review:
    """A copy of the review carrying a freshly minted acknowledgement token.

    A copy rather than an assignment, because `Review` is frozen. Written out
    field by field rather than with ``dataclasses.replace`` so that adding a
    field without thinking about the token is a visible omission here.
    """
    return Review(
        arrivals=review.arrivals,
        widened=review.widened,
        dead=review.dead,
        gone=review.gone,
        first_run=review.first_run,
        token=new_token(),
    )


def _unclassified(cmd: Command, tier: Tier, known_groups: frozenset[str]) -> bool:
    """Whether this route is safe only because nobody has classified its group.

    `GUARDED_GROUPS` is a hand-written frozenset, so a destructive group Omarchy
    invents tomorrow derives as `safe` and runs on arrival. That is the honest
    limit of decision 4, and this is where it is said out loud rather than left
    for somebody to discover.

    The test is whether the *group is new*, not whether it is absent from
    `GUARDED_GROUPS` -- every safe route's group is absent from that set, which
    is why it is safe. A group that was here last time and was left alone is a
    decision, however implicit; one that has never been seen is not.
    """
    if tier is not Tier.SAFE:
        return False
    if cmd.group in GUARDED_GROUPS or cmd.route in GUARDED_ROUTES:
        return False
    return cmd.group not in known_groups


def as_dict(review: Review) -> dict[str, object]:
    """The review as data, for the panel and for a terminal.

    The token is **not** in here. It is published on the frame the shell reads
    and nowhere else, so a report anybody can ask for cannot hand out the ability
    to consume a warning.
    """
    return {
        "headline": review.headline,
        "urgent": review.urgent,
        "firstRun": review.first_run,
        "arrivals": [
            {
                "route": a.route,
                "tier": a.tier,
                "effect": a.effect,
                "rule": a.rule,
                "source": a.source,
                "quarantined": a.quarantined,
                "unclassified": a.unclassified,
            }
            for a in review.arrivals
        ],
        "widened": [
            {
                "matcher": w.matcher,
                "effect": w.effect,
                "source": w.source,
                "routes": list(w.routes),
            }
            for w in review.widened
        ],
        "dead": [
            {"matcher": d.matcher, "effect": d.effect, "source": d.source}
            for d in review.dead
        ],
        "gone": list(review.gone),
    }


def message(review: Review) -> str:
    """The notification body. A paragraph, not a listing -- the panel has that."""
    lines = []
    # Ordered by how much each one deserves the person's attention: a rule that
    # widened, then a command nothing has classified, then a dead rule, then the
    # ones that will simply ask.
    if review.widened:
        for rule in review.widened[:NAMED_IN_NOTIFICATION]:
            shown = ", ".join(rule.routes[:NAMED_IN_NOTIFICATION])
            more = len(rule.routes) - NAMED_IN_NOTIFICATION
            lines.append(
                f"Your {rule.effect} rule {rule.matcher!r} now also covers {shown}"
                + (f" and {more} more" if more > 0 else "")
                + "."
            )
    unclassified = [a for a in review.arrivals if a.unclassified]
    if unclassified:
        shown = ", ".join(a.route for a in unclassified[:NAMED_IN_NOTIFICATION])
        more = len(unclassified) - NAMED_IN_NOTIFICATION
        lines.append(
            f"{len(unclassified)} command{_s(unclassified)} arrived in a group this "
            f"plugin has never classified, so they run without being asked about: "
            f"{shown}" + (f" and {more} more" if more > 0 else "") + "."
        )
    for rule in review.dead[:NAMED_IN_NOTIFICATION]:
        lines.append(
            f"Your {rule.effect} rule {rule.matcher!r} no longer matches anything."
        )
    held = [a for a in review.arrivals if a.quarantined]
    if held:
        lines.append(
            f"{len(held)} new command{_s(held)} fall under an existing allow rule and "
            f"will be asked about once each until you review them."
        )
    lines.append("Open the MCP server panel in the bar to review.")
    return "\n".join(lines)
