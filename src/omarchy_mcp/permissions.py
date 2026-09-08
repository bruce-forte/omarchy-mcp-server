"""What an agent is permitted to do, as a document the user owns.

`policy.py` derives three tiers from the registry, so the classification does not
rot when Omarchy adds commands. This is the other half: which of the guarded ones
may actually run. It used to be two TOML arrays that nobody edited, which made
`guarded` mean *never*; it is now three lists of rules, evaluated
``deny`` -> ``ask`` -> ``allow``.

The shape is Claude Code's, because it solves the same problem for the same kind
of caller and a user of this plugin has probably already met it::

    {
      "permissions": {
        "guardedDefault": "ask",
        "deny":  [{"kind": "route", "matcher": "omarchy dev *"}],
        "ask":   [{"kind": "route", "matcher": "omarchy install *"}],
        "allow": [{"kind": "route", "matcher": "omarchy theme *"}]
      }
    }

Three things about it are deliberate and are the reason this module exists at all
rather than a couple more `Config` fields.

**First match wins, and specificity never reorders it.** A narrow ``allow`` does
not beat a broad ``ask``, exactly as upstream. That is what keeps a broad ``deny``
from being defeatable by a narrower grant, and the property is worth more than
the convenience it costs. The cost is real: an ``always`` click that would be
shadowed by the user's own ``ask`` rule is a control with no effect, so the panel
has to know not to offer it -- see `evaluate`, which names the deciding rule for
exactly that reason.

**The document is not the top authority.** Two things beat any rule anyone can
write: a command needing sudo, which no configuration promotes (decision 4), and
a route whose argument *is* a command line, which may be asked about and never
granted. `evaluate` is the one place that ladder exists.

**Matchers are an exact route or a prefix with a trailing ``*``, and nothing
else.** Upstream allows a ``*`` anywhere because a shell command line is an
unbounded string. This route space is closed and enumerable, which buys something
better instead: a matcher can be expanded against the registry and shown as the
concrete routes it covers.

This module spawns nothing and decides nothing about the running daemon. What to
do about a document that will not load -- refuse to start, keep the last good one
-- belongs to the caller, and the two answers are different.

It does own one write: `grant`, which appends an ``allow`` rule when a person
answers *always* at the desk. The daemon is the only writer, it only ever writes
`permissions.local.json`, and it only ever writes an exact route -- see `grant`
for why each of those is load-bearing.

Reading this file
-----------------

It is long, and it is four things in order, marked by banner comments:

1. **the document** -- pydantic models describing the JSON on disk. They both
   validate what is read and generate the published JSON Schema.
2. **rules in memory** -- `Rule` and `Permissions`, the frozen shapes the rest
   of the daemon holds.
3. **reading** -- `parse` and `load`, turning files into those shapes, plus
   `check`, which finds the defects only the live registry can reveal.
4. **the ladder** -- `evaluate` and `decide`, the actual decision, and then the
   writers (`grant`, `prune`, `revoke`, `seed`).

If you read one function, read `evaluate`: it is the whole precedence ladder in
one place, deliberately, so that it cannot be applied by halves.
"""

from __future__ import annotations

import hashlib
import json
import os
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator

from .policy import Tier, base_tier
from .registry import Command

#: Guarded routes that may be asked about and never granted outright.
#:
#: The criterion is narrow on purpose: **the route's own argument is a command
#: line**. `omarchy update lock run <command> [args...]` runs whatever it is
#: handed, so one standing grant on it is a standing grant on everything, shown
#: in a permissions review as a single calm row.
#:
#: The wider reading -- "could lead to running attacker-chosen code" -- would
#: swallow `install`, `pkg aur add` and `dev link`, and then nothing worth
#: granting could be granted. This is not a tier: such a route is perfectly
#: runnable, and a person answering a question about a specific call is exactly
#: the right amount of friction for it.
NEVER_STORE = frozenset({"omarchy update lock"})

#: Where the published schema lives, so an editor opening the daemon's own file
#: validates it the same way it validates the user's.
SCHEMA_URL = (
    "https://raw.githubusercontent.com/bruce-forte/omarchy-mcp-server/master/"
    "permissions.schema.json"
)

#: What the daemon's own file says about itself. A person who finds it should be
#: able to tell in one line what wrote it and whether they may touch it.
LOCAL_COMMENT = (
    "Written by the Omarchy MCP server when you answer 'always' at the desk. "
    "Safe to edit or delete by hand; gitignore it. Your own rules go in "
    "permissions.json beside it."
)

#: What a file created by `seed` says instead. It is the only documentation a
#: person gets at the moment they open an empty rules file, so it states the
#: ladder and both matcher forms rather than pointing somewhere else.
SEED_COMMENT = (
    "Rules are read deny, then ask, then allow; the first match decides, and a "
    "narrower rule never reorders that. A matcher is an exact route "
    '("omarchy install app") or a prefix with a trailing " *" '
    '("omarchy install *"), which also covers the bare route. Run '
    "`omarchy-mcpd --permissions` to see what each rule covers on this machine."
)

#: The wildcard suffix, space included. The space is part of the rule: a matcher
#: is a sequence of whole route tokens, and `omarchy install*` is not a shorter
#: spelling of anything -- it is rejected rather than guessed at.
WILDCARD = " *"


class Effect(str, Enum):
    """What a rule says, and what `evaluate` answers with."""

    DENY = "deny"
    ASK = "ask"
    ALLOW = "allow"


#: The order rules are consulted in. Not alphabetical, not the order they appear
#: in the file: restrictions are considered before grants so that a grant cannot
#: carve an exception out of a restriction.
PRECEDENCE = (Effect.DENY, Effect.ASK, Effect.ALLOW)

#: What an unmatched guarded route does. `ASK` is the default because a document
#: that fills itself through use never fills if nothing is ever asked.
DEFAULT_GUARDED = Effect.ASK

#: How long a call waits for the user to answer. Long enough to walk back from
#: the kettle, short enough that an agent is not parked on a dead request.
#: Bounded so that neither extreme can exist: a second is not long enough to
#: read the question, and ten minutes is a request parked on an empty desk.
DEFAULT_ASK_TIMEOUT_S = 60
ASK_TIMEOUT_BOUNDS = (5, 600)


class PermissionsError(ValueError):
    """The document is defective, and the caller cannot know what was intended.

    Raised for every defect, however small, because a permissions file that
    parses halfway is a file whose intent is a guess. `config.py`'s doctrine --
    report the problem and fall back to defaults -- is right for `config.toml`,
    where ignoring a key falls back to a safe default. It inverts here: ignoring
    a ``deny`` is a loss of protection, and "no rules" is not the safe floor,
    because a hand-written ``deny`` demotes routes the derivation calls safe.
    """


# --- the document -----------------------------------------------------------
#
# pydantic rather than a hand-rolled parser, and rather than a `jsonschema`
# dependency: the SDK already brings pydantic in, the models generate the
# published JSON Schema (`make schema`), and per-field errors are already the
# shape an error message needs -- which key, which index, and what was wrong.
#
# If pydantic is new: a `BaseModel` subclass declares fields as annotated
# attributes, and `Model.model_validate(some_dict)` checks a dict against them,
# raising `ValidationError` listing every field that did not fit. Unlike a plain
# type hint, this *is* enforced -- validating untrusted input is the whole job.
# `Field(...)` attaches the description and constraints that end up in the
# published schema, and `Literal["route"]` means the value may be that exact
# string and nothing else.


class RuleModel(BaseModel):
    """One rule as it appears on disk.

    An object rather than upstream's ``Tool(specifier)`` string, so `kind`
    validates as an enum and a second kind is an enum addition rather than a
    parser change.
    """

    model_config = ConfigDict(extra="forbid")

    kind: Literal["route"] = Field(
        description="What the matcher names. Only routes today; a second kind is an addition here."
    )
    matcher: str = Field(
        description=(
            'An exact route ("omarchy install app"), or a prefix with a trailing '
            '" *" ("omarchy install *"), which also matches the bare route. '
            "Wildcards anywhere else are rejected: the route space is closed, so "
            "there is nothing for them to express."
        ),
        examples=["omarchy install app", "omarchy install *"],
        # Only in the published schema, so an editor rejects a bad matcher before
        # the daemon does. The validator below is what runs here, because its
        # message can say which of the two forms was meant; a regex failure says
        # only that a pattern did not match.
        json_schema_extra={"pattern": r"^[^\s*]+( [^\s*]+)*( \*)?$"},
    )

    # A pydantic validator: it runs on the ``matcher`` field during validation,
    # and raising ``ValueError`` inside it is how a field is rejected -- the
    # message ends up in the ``ValidationError`` that `_explain` formats.
    @field_validator("matcher")
    @classmethod
    def _shape(cls, value: str) -> str:
        """Two legal forms, and the error has to name both.

        Shape only. Whether the matcher names anything that exists is a question
        for the registry, and the answer changes with an Omarchy upgrade -- see
        `check`.
        """
        legal = 'either an exact route ("omarchy install app") or a prefix and a trailing " *" ("omarchy install *")'
        if value != value.strip() or "  " in value:
            raise ValueError(f"{value!r} has stray whitespace; {legal}")
        if not value:
            raise ValueError(f"a matcher cannot be empty; {legal}")

        stars = value.count("*")
        if stars == 0:
            return value
        if stars > 1:
            raise ValueError(f"{value!r} has more than one '*'; {legal}")
        if value == "*":
            raise ValueError(
                f"a bare '*' has no prefix, and would cover every command Omarchy "
                f"ships. Write the prefix you mean, or 'omarchy *' if you mean all "
                f"of them; {legal}"
            )
        if not value.endswith(WILDCARD):
            raise ValueError(
                f"{value!r} puts '*' somewhere other than the end, or omits the "
                f"space before it; {legal}"
            )
        return value


# The ``"permissions"`` object: the two settings and the three rule lists.
#
# A comment rather than a docstring, deliberately: pydantic copies a model's
# docstring into the generated JSON Schema as the object's ``description``, and
# `make schema` would then fail as stale on a purely editorial change.
class PermissionsBlock(BaseModel):
    #: ``extra="forbid"`` rejects any key not declared below. The default would
    #: silently ignore it, which is how a misspelled ``"dney"`` becomes a
    #: protection somebody believes they have.
    model_config = ConfigDict(extra="forbid")

    #: What a guarded route with no matching rule does. Named after upstream's
    #: `defaultMode`, and the way to get the old refuse-by-default behaviour
    #: back -- discoverable in the schema rather than buried in a comment.
    guardedDefault: Literal["ask", "deny"] | None = Field(
        default=None,
        description=(
            "What a destructive command does when no rule matches it. 'ask' raises a "
            "notification on the desktop naming the command and what it resolved to; "
            "'deny' refuses it outright. Commands that are not destructive always run, "
            "and commands requiring sudo never do."
        ),
    )

    askTimeoutSeconds: int | None = Field(
        default=None,
        ge=ASK_TIMEOUT_BOUNDS[0],
        le=ASK_TIMEOUT_BOUNDS[1],
        description=(
            "How long an approval notification waits before the call is refused. "
            "No answer means denied: a prompt that granted on expiry would grant to "
            "an empty room."
        ),
    )

    deny: list[RuleModel] = Field(
        # ``default_factory=list`` gives each document its own empty list; a
        # plain ``= []`` default would be one list shared by every instance.
        default_factory=list,
        description="Refused outright. Consulted first, and a narrower allow never overrides one.",
    )
    ask: list[RuleModel] = Field(
        default_factory=list,
        description="Asked about on the desktop every time. Consulted before allow.",
    )
    allow: list[RuleModel] = Field(
        default_factory=list,
        description="Runs without asking. Consulted last, so a matching deny or ask wins.",
    )


class PermissionsDocument(BaseModel):
    """A whole file. ``extra="forbid"`` throughout: a misspelled key is a defect,
    and silently ignoring it is how somebody comes to believe they wrote a rule
    they did not."""

    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    json_schema: str | None = Field(default=None, alias="$schema")
    #: JSON has no comment syntax and this file is one somebody will find and
    #: wonder about. Allowed rather than forbidden so the daemon can say what
    #: `permissions.local.json` is, and so a person can leave themselves a note
    #: without the document becoming defective and stopping the daemon.
    comment: str | None = Field(default=None, alias="_comment")
    permissions: PermissionsBlock = Field(default_factory=PermissionsBlock)


# --- rules in memory --------------------------------------------------------


@dataclass(frozen=True)
class Rule:
    """One rule, plus where it came from.

    `source` and `index` exist for the explainer. "Why can the agent do this?" is
    the question a person opens the panel to ask, and an answer that cannot name
    the file and the line is not an answer -- it is also what stops the panel
    offering a Revoke button for a rule it cannot revoke.
    """

    effect: Effect
    matcher: str
    source: str
    index: int
    kind: str = "route"

    @property
    def prefix(self) -> str:
        """The matcher without its wildcard, or ``""`` for an exact matcher."""
        return self.matcher[: -len(WILDCARD)] if self.wild else ""

    @property
    def wild(self) -> bool:
        """Whether this is a prefix matcher rather than an exact route."""
        return self.matcher.endswith(WILDCARD)

    def matches(self, route: str) -> bool:
        """Whether this rule covers ``route``.

        A trailing ``*`` covers the bare route as well as everything under it,
        which upstream does too and which is load-bearing here: `group` is
        exactly the second token of every route Omarchy ships, so
        ``omarchy migrate *`` is the ``migrate`` group -- and 19 routes are two
        tokens long, making ``omarchy migrate`` both a group and a route. Without
        this, a group matcher would silently miss the bare route, which is
        usually the most dangerous member.
        """
        if not self.wild:
            return route == self.matcher
        prefix = self.prefix
        # The ``+ " "`` matters: without it ``omarchy install *`` would also
        # match a hypothetical ``omarchy installer``, which is a different word.
        return route == prefix or route.startswith(prefix + " ")

    def covers(self, commands: Iterable[Command]) -> tuple[str, ...]:
        """The concrete routes this matcher expands to, in order.

        The thing a closed route space buys that an unbounded one cannot: a rule
        can be shown as what it actually covers, in the panel and in the delta.
        """
        return tuple(sorted(c.route for c in commands if self.matches(c.route)))


@dataclass(frozen=True)
class Permissions:
    """Every rule from every file, pooled, in precedence order."""

    rules: tuple[Rule, ...] = ()
    guarded_default: Effect = DEFAULT_GUARDED
    #: ``float`` rather than ``int``: this is a duration, and the document's
    #: own `askTimeoutSeconds` is still whole seconds. The wider type is what
    #: lets a test give the gate a fifth of a second instead of thirty.
    ask_timeout_s: float = DEFAULT_ASK_TIMEOUT_S
    #: Which file set `guarded_default`, for the explainer. Empty means nobody
    #: did and the value is this module's own.
    guarded_default_source: str = ""

    def matching(self, route: str) -> Rule | None:
        """The rule that decides ``route``, or ``None``.

        `rules` is already in precedence order, so this is the first match --
        which is the whole rule, stated once, in one place.
        """
        # ``next(generator, default)`` takes the first item a generator produces
        # and stops there, returning the default if there is none. Nothing after
        # the first match is even evaluated, which is the point.
        return next((r for r in self.rules if r.matches(route)), None)


# --- reading ----------------------------------------------------------------


@dataclass(frozen=True)
class Options:
    """A file's settings, as opposed to its rules.

    ``None`` means the file did not mention the key, which is different from
    setting it to the default: each one decides a single thing, so two files
    claiming the same key is an error rather than a precedence question.
    """

    guarded_default: Effect | None = None
    ask_timeout_s: int | None = None

    def named(self) -> dict[str, Any]:
        """The keys this file actually set, by the name it wrote them under."""
        return {
            name: value
            for name, value in (
                ("guardedDefault", self.guarded_default),
                ("askTimeoutSeconds", self.ask_timeout_s),
            )
            if value is not None
        }


def parse(text: str, *, source: str) -> tuple[tuple[Rule, ...], Options]:
    """Parse one file. Raises `PermissionsError` on any defect at all."""
    try:
        raw = json.loads(text)
    except json.JSONDecodeError as exc:
        raise PermissionsError(f"{source} is not valid JSON: {exc}") from exc

    if not isinstance(raw, dict):
        raise PermissionsError(f"{source} must contain a JSON object, not a {type(raw).__name__}")

    try:
        doc = PermissionsDocument.model_validate(raw)
    except ValidationError as exc:
        raise PermissionsError(_explain(exc, source)) from exc

    block = doc.permissions
    rules: list[Rule] = []
    for effect in PRECEDENCE:
        # ``getattr(block, "deny")`` is ``block.deny``, with the attribute name
        # taken from the enum -- so the three lists are read in precedence order
        # by construction rather than by three copies of the same loop.
        for index, model in enumerate(getattr(block, effect.value)):
            rules.append(
                Rule(
                    effect=effect,
                    matcher=model.matcher,
                    source=source,
                    index=index,
                    kind=model.kind,
                )
            )

    options = Options(
        guarded_default=Effect(block.guardedDefault) if block.guardedDefault else None,
        ask_timeout_s=block.askTimeoutSeconds,
    )
    return tuple(rules), options


def _explain(exc: ValidationError, source: str) -> str:
    """Turn pydantic's errors into something a person can act on.

    The parse error is the only thing standing between a stray comma and a daemon
    that will not start, so it names the file, the key, the index and the value,
    and repeats the two legal forms rather than assuming they are remembered.
    """
    lines = [f"{source} is not a valid permissions document:"]
    for err in exc.errors():
        # ``loc`` is pydantic's path to the offending value, as a tuple like
        # ``("permissions", "deny", 0, "matcher")``. Joined with dots it reads
        # as the place in the file a person should look.
        where = ".".join(str(part) for part in err["loc"]) or "(root)"
        message = err["msg"].removeprefix("Value error, ")
        lines.append(f"  {where}: {message}")
    return "\n".join(lines)


def load(paths: Sequence[Path]) -> Permissions:
    """Read and pool every file that exists, in the order given.

    A missing file is not a defect -- the overwhelmingly common state is no file
    at all, and a fresh install must work. Neither is an empty ``permissions``
    object: it says "no rules", which is a thing a person may mean.

    ``paths`` is ordered by whose rule should be *named* when two say the same
    thing, not by whose wins: the effect lists are pooled and consulted in
    `PRECEDENCE` order regardless of which file a rule came from. The user's own
    file goes first, so the explainer quotes their rule rather than the daemon's
    copy of the same decision.
    """
    #: One bucket per effect, so rules can be pooled across files and still come
    #: out in precedence order at the end.
    pooled: dict[Effect, list[Rule]] = {effect: [] for effect in PRECEDENCE}
    #: The settings each file set, by the name it wrote them under. ``Any``
    #: because the values are heterogeneous -- an `Effect` and an ``int`` -- and
    #: `parse` has already checked the type of each against the key it came
    #: under, so nothing downstream re-derives it.
    settings: dict[str, Any] = {}
    #: Which file set each setting, so the second one to claim it is an error
    #: naming both files rather than a silent overwrite.
    claimed: dict[str, str] = {}

    for path in paths:
        try:
            text = path.read_text()
        except FileNotFoundError:
            continue
        except OSError as exc:
            raise PermissionsError(f"{path} could not be read: {exc}") from exc

        rules, options = parse(text, source=path.name)
        for rule in rules:
            pooled[rule.effect].append(rule)

        for key, value in options.named().items():
            if key in claimed:
                raise PermissionsError(
                    f"{path.name} also sets {key}, which {claimed[key]} already set. "
                    f"It decides one thing, so it belongs in one file."
                )
            claimed[key] = path.name
            settings[key] = value

    # A nested comprehension: the outer ``for`` walks the effects in precedence
    # order and the inner one flattens each bucket into a single tuple. Every
    # deny, then every ask, then every allow.
    ordered = tuple(rule for effect in PRECEDENCE for rule in pooled[effect])
    return Permissions(
        rules=ordered,
        guarded_default=settings.get("guardedDefault", DEFAULT_GUARDED),
        ask_timeout_s=settings.get("askTimeoutSeconds", DEFAULT_ASK_TIMEOUT_S),
        guarded_default_source=claimed.get("guardedDefault", ""),
    )


# --- what a rule turns out to mean ------------------------------------------


@dataclass(frozen=True)
class Finding:
    """A rule that does not do what it looks like it does.

    Four levels. The first two are about a rule that cannot work; the last two
    are about one that works and is never reached:

    ``error``
        The rule asserts a permission the system will never honour -- granting a
        sudo route, or granting one whose argument is a command line. Believing
        you granted something you did not is worse than being told loudly, so
        this stops the daemon.

    ``void``
        The matcher covers nothing today. Indistinguishable, by construction,
        from a matcher for a route that has not shipped yet: refusing it would
        turn an upstream rename into a daemon that will not start, punishing the
        user for somebody else's commit. It loads, and the explainer says so.

    ``shadowed``
        Every route it covers is already decided by an earlier rule with a
        *different* effect, so it never decides anything. The one defect nothing
        reported before N15: you believe you granted something and you did not.
        It fails safe by construction -- ``deny`` is first in `PRECEDENCE`, so
        only an ``ask`` or an ``allow`` can be shadowed -- which is why it is a
        finding and never a notification.

    ``redundant``
        The same, but the earlier rule has the *same* effect. The verdict is
        unchanged either way; what it means is that the rule can go, which is
        what the panel offers to do with it.
    """

    rule: Rule
    level: Literal["error", "void", "shadowed", "redundant"]
    reason: str
    #: The rule that got there first, for the two levels where there is one.
    #: "Shadowed" without a culprit leaves a person diffing two files by eye,
    #: and the two fixes -- delete this rule, or narrow the one above it --
    #: cannot be chosen between without knowing which rule is above.
    by: Rule | None = None


def check(perms: Permissions, commands: dict[str, Command]) -> tuple[Finding, ...]:
    """Everything wrong with these rules that only the registry can reveal.

    Separate from `parse` because it is a different kind of wrong and it changes
    without the file changing: the same document is clean today and has a dead
    rule after an `omarchy update`.
    """
    findings: list[Finding] = []
    for rule in perms.rules:
        covered = rule.covers(commands.values())

        if not covered:
            findings.append(
                Finding(
                    rule,
                    "void",
                    f"{rule.matcher!r} matches no command Omarchy ships. Either a "
                    f"typo, or a route that has been renamed or has not shipped yet.",
                )
            )
            continue

        # A wildcard that happens to cover a sudo route is not an assertion about
        # it -- `omarchy update *` is a reasonable thing to write, and refusing
        # it because one of its members needs sudo would make prefixes unusable.
        # An exact matcher is an assertion, and this is the one to catch.
        if rule.wild or rule.effect is Effect.DENY:
            continue

        route = covered[0]
        if commands[route].requires_sudo:
            findings.append(
                Finding(
                    rule,
                    "error",
                    f"{rule.matcher!r} is in the {rule.effect.value!r} list, but that "
                    f"route needs sudo and can never run: this daemon has no "
                    f"controlling terminal. Remove it, or move it to 'deny' if you "
                    f"meant to agree.",
                )
            )
        elif route in NEVER_STORE:
            findings.append(
                Finding(
                    rule,
                    "error",
                    f"{rule.matcher!r} is in the {rule.effect.value!r} list, but that "
                    f"route takes a command line as its argument: allowing it once "
                    f"allows everything, so it can be asked about and never granted.",
                )
            )
    # Identity, not equality: two rules can be identical in every field -- same
    # effect, same matcher, same file -- and still be two separate entries that
    # must be reported separately. ``id()`` is what tells them apart.
    findings.extend(_inert(perms, commands, {id(f.rule) for f in findings}))
    return tuple(findings)


def _inert(
    perms: Permissions, commands: dict[str, Command], flagged: set[int]
) -> Iterable[Finding]:
    """Rules that match commands and still never decide any of them.

    `rules` is in precedence order and `matching` takes the first hit, so a rule
    is inert exactly when every route it covers is claimed by one earlier in the
    pool. **Partial** coverage is not a defect and must not be reported as one:
    ``allow omarchy theme *`` under a ``deny omarchy theme set`` is a good pair,
    and flagging it would make prefixes unwritable.

    A rule that is already an error is left alone -- it stops the daemon, which
    is a louder thing to be told, and two findings for one rule would be two
    lines saying different things about the same broken sentence.
    """
    for position, rule in enumerate(perms.rules):
        if id(rule) in flagged:
            continue
        covered = rule.covers(commands.values())
        if not covered:
            continue

        earlier = perms.rules[:position]
        deciders = []
        for route in covered:
            first = next((r for r in earlier if r.matches(route)), None)
            if first is None:
                # One route this rule alone decides is enough: it is not inert.
                break
            deciders.append(first)
        # A ``for``/``else``: the ``else`` runs only when the loop finished
        # without hitting ``break``. Here that means *every* covered route was
        # already claimed, which is exactly the definition of inert.
        else:
            differing = next((r for r in deciders if r.effect is not rule.effect), None)
            by = differing if differing is not None else deciders[0]
            if differing is not None:
                yield Finding(
                    rule,
                    "shadowed",
                    f"{rule.matcher!r} never decides anything: everything it covers is "
                    f"already {by.effect.value!r} under {by.matcher!r} in {by.source}, "
                    f"which is read first. Remove this rule, or narrow that one.",
                    by=by,
                )
            else:
                yield Finding(
                    rule,
                    "redundant",
                    f"{rule.matcher!r} changes nothing: everything it covers is already "
                    f"{by.effect.value!r} under {by.matcher!r} in {by.source}. Safe to "
                    f"remove.",
                    by=by,
                )


def errors(findings: Iterable[Finding]) -> tuple[Finding, ...]:
    """Just the findings that stop the daemon. See `Finding` for the four levels."""
    return tuple(f for f in findings if f.level == "error")


# --- the ladder -------------------------------------------------------------


@dataclass(frozen=True)
class Outcome:
    """What happens to one route, and what decided it."""

    effect: Effect
    #: What kind of command it is, which is not what happens to it. The activity
    #: log records both: "refused, and it was a guarded command" and "refused,
    #: and it needed sudo" are different events for the person reading it back.
    tier: Tier
    #: The rule that decided, or ``None`` when nothing matched and the answer came
    #: from the tier or from `guardedDefault`.
    rule: Rule | None
    reason: str

    @property
    def allowed(self) -> bool:
        """Whether this route runs with nobody being asked."""
        return self.effect is Effect.ALLOW

    @property
    def asks(self) -> bool:
        """Whether this route puts a question on the desktop rather than running
        or refusing. Published by `omarchy_search_commands` and the commands
        resource so an agent knows a call is worth making -- reporting it as
        refused would make a careful agent never call it, and the prompt would
        never fire."""
        return self.effect is Effect.ASK

    @property
    def by_rule(self) -> bool:
        """Whether a written rule decided this, as opposed to the tier or the default."""
        return self.rule is not None


def evaluate(
    route: str, tier: Tier, perms: Permissions, *, unreviewed: frozenset[str] = frozenset()
) -> Outcome:
    """The whole ladder, in one function, so it cannot be half-applied.

    ::

        1  blocked (requires_sudo)   no rule promotes it -- decision 4
        2  deny rule
        3  ask rule
        4  allow rule
        5  guardedDefault            when the route is guarded and nothing matched
        6  the tier                  safe runs, and that is the end of it

    with one clamp applied afterwards: a route whose argument is a command line
    can be asked about and never granted, however it got to ``allow``. It sits
    after the rules rather than before them so that a ``deny`` still denies it --
    a restriction on such a route is the user agreeing, and agreeing is never an
    error.
    """
    if tier is Tier.BLOCKED:
        return Outcome(
            Effect.DENY,
            tier,
            None,
            f"`{route}` requires sudo. The MCP server runs without a controlling "
            f"terminal, so a password prompt could never be answered. No "
            f"permission rule changes that.",
        )

    rule = perms.matching(route)
    if rule is not None:
        outcome = Outcome(
            rule.effect,
            tier,
            rule,
            f"`{route}` is matched by {rule.matcher!r} in the "
            f"{rule.effect.value!r} list of {rule.source}.",
        )
    elif tier is Tier.GUARDED:
        outcome = Outcome(
            perms.guarded_default,
            tier,
            None,
            f"`{route}` can change the system in ways that are hard to undo, and no "
            f"permission rule covers it. To allow it, add "
            f'{{"kind": "route", "matcher": "{route}"}} to the "allow" list in '
            f"~/.config/omarchy/mcp/permissions.json.",
        )
    else:
        return Outcome(Effect.ALLOW, tier, None, "")

    if outcome.effect is Effect.ALLOW and route in NEVER_STORE:
        return Outcome(
            Effect.ASK,
            tier,
            outcome.rule,
            f"`{route}` takes a command line as its argument, so it is asked about "
            f"every time and never granted outright.",
        )

    # Restrictions extend forward; grants do not. A route that appeared under an
    # existing `allow` was never seen by the person who wrote that rule, so it
    # is asked about once rather than running on arrival. `deny` and `ask` are
    # untouched: protection propagating is the point. See `delta.py`.
    if outcome.effect is Effect.ALLOW and route in unreviewed:
        return Outcome(
            Effect.ASK,
            tier,
            outcome.rule,
            f"`{route}` is new since you last reviewed permissions, and is covered "
            f"by an existing allow rule rather than one written for it. It is asked "
            f"about until the review in the bar panel is acknowledged.",
        )
    return outcome


ASK_NOTE = (
    "This call pauses while the user is asked to approve it, and is refused if "
    "they decline or do not answer."
)


def describe(
    cmd: Command, perms: Permissions, *, unreviewed: frozenset[str] = frozenset()
) -> dict[str, Any]:
    """This server's verdict on one command, for anything that publishes it.

    One derivation with two readers -- `omarchy_search_commands` and the commands
    resource -- which would otherwise be free to disagree with each other and
    with the gate.

    **A route that will ask is reported runnable.** Reporting it as refused makes
    a careful agent never call it, so the prompt never fires and the feature is
    invisible to the only caller there is.
    """
    outcome = decide(cmd, perms, unreviewed=unreviewed)
    row: dict[str, Any] = {
        "tier": outcome.tier.value,
        "runnable": outcome.allowed or outcome.asks,
    }
    # Which rule decided, and which file it came from. The question a person
    # opens the panel to ask is "why can the agent do this?", and an answer that
    # cannot name the rule and the file is not one -- it is also what stops a
    # surface offering a Revoke button for a rule it cannot revoke.
    if outcome.rule is not None:
        row["rule"] = outcome.rule.matcher
        row["source"] = outcome.rule.source
    if outcome.asks:
        row["asks"] = True
        row["note"] = ASK_NOTE
    elif not outcome.allowed:
        row["refusal"] = outcome.reason
    return row


#: How many covered routes a rule lists before the rest become a count.
#:
#: `omarchy *` covers every route Omarchy ships, and a listing that prints all of
#: them buries the twelve rules around it. The count is always exact; only the
#: enumeration is cut, and it says so.
COVERED_SHOWN = 12


def explain(perms: Permissions, commands: dict[str, Command]) -> dict[str, Any]:
    """The whole permission state, for a person who asked what an agent may do.

    Not the same thing as `omarchy://commands`, which annotates every route with
    a verdict. This is the other direction: the *rules*, what each one actually
    covers on this machine, and then only the routes whose answer is not simply
    "runs". A listing of 426 rows where 370 say "safe, runs" hides the fifty-odd
    that matter.

    Every count here is computed against the live registry rather than the
    document, which is the point: a rule is static and the registry moves under
    it, so "what does this rule cover" is a question only the pair can answer.
    """
    rows = []
    findings = {id(f.rule): f for f in check(perms, commands)}
    for rule in perms.rules:
        covered = rule.covers(commands.values())
        row: dict[str, Any] = {
            "effect": rule.effect.value,
            "matcher": rule.matcher,
            "source": rule.source,
            "covers": len(covered),
            "routes": list(covered[:COVERED_SHOWN]),
        }
        if len(covered) > COVERED_SHOWN:
            row["more"] = len(covered) - COVERED_SHOWN
        finding = findings.get(id(rule))
        if finding is not None:
            row[finding.level] = finding.reason
            # The rule that got there first, so a surface can name it without
            # re-deriving precedence -- and so the panel can point at the file
            # the fix belongs in rather than at this one.
            if finding.by is not None:
                row["by"] = finding.by.matcher
                row["bySource"] = finding.by.source
        rows.append(row)

    interesting = []
    counts = {effect.value: 0 for effect in Effect}
    for cmd in sorted(commands.values(), key=lambda c: c.route):
        outcome = decide(cmd, perms)
        counts[outcome.effect.value] += 1
        # Two uninteresting majorities are left out, and counted instead.
        #
        # A safe route nothing touches simply runs, and there are hundreds. A
        # `blocked` route is refused whatever the document says, and there are
        # over a hundred of those -- listing them would bury the fifty-odd
        # routes this document actually governs under a wall of "needs sudo".
        # What is left is exactly the set a person is asking about: every route
        # whose answer the document had a hand in.
        if outcome.tier is Tier.BLOCKED:
            continue
        if outcome.effect is Effect.ALLOW and outcome.rule is None and outcome.tier is Tier.SAFE:
            continue
        entry: dict[str, Any] = {
            "route": cmd.route,
            "tier": outcome.tier.value,
            "effect": outcome.effect.value,
        }
        if outcome.rule is not None:
            entry["rule"] = outcome.rule.matcher
            entry["source"] = outcome.rule.source
        if cmd.route in NEVER_STORE:
            entry["neverGranted"] = True
        interesting.append(entry)

    return {
        "guardedDefault": perms.guarded_default.value,
        "guardedDefaultSource": perms.guarded_default_source or "the built-in default",
        "askTimeoutSeconds": perms.ask_timeout_s,
        "precedence": [effect.value for effect in PRECEDENCE],
        "rules": rows,
        "counts": {
            "commands": len(commands),
            "byEffect": counts,
            "rules": len(perms.rules),
            "listed": len(interesting),
        },
        "neverGranted": sorted(NEVER_STORE),
        "routes": interesting,
        "note": (
            "Rules are read deny, then ask, then allow; the first match decides, and a "
            "narrower rule never reorders that. `routes` lists only what this document "
            "has a hand in: routes that are safe and matched by no rule are omitted "
            "because they simply run, and routes needing sudo are omitted because they "
            "are refused whatever the document says. `counts.byEffect` covers all of "
            "them."
        ),
    }


class GrantRefused(RuntimeError):
    """An ``always`` that must not be written, and the call it came from refused.

    The pool is the authority at the moment of execution. If the user's own file
    grew a matching ``deny`` between the question going up and the answer coming
    back, that ``deny`` is newer than the question, and running the command
    because a click was in flight is indefensible.
    """


def grant(path: Path, route: str, perms: Permissions, commands: dict[str, Command]) -> Rule:
    """Append an ``allow`` rule for ``route``, or refuse to.

    **Only ever an exact route.** A click consents to what was on the screen.
    `omarchy install app` clicked nine times never becomes `omarchy install *`,
    however obvious the pattern looks: a wildcard is a thing a person types
    having read what it covers, and inferring one would grant routes nobody was
    shown. If collapsing a set of grants into a prefix is worth offering, it is
    a suggestion a surface makes and the user accepts into their *own* file.

    **Only ever `permissions.local.json`.** `permissions.json` is the user's, it
    is meant to be checked into a dotfiles repository, and a daemon that rewrites
    a tracked file lands in somebody's diff at the wrong moment.

    **Every invariant is checked here**, not only where the button was drawn. A
    surface that hides the button is a UI; this is the security artifact, and the
    two are allowed to disagree only in the safe direction.
    """
    cmd = commands.get(route)
    if cmd is None:
        raise GrantRefused(f"`{route}` is not a command on this Omarchy.")
    if cmd.requires_sudo:
        raise GrantRefused(f"`{route}` needs sudo, which no rule can grant.")
    if route in NEVER_STORE:
        raise GrantRefused(
            f"`{route}` takes a command line as its argument, so it is asked about "
            f"every time and never granted."
        )

    shadow = perms.matching(route)
    if shadow is not None and shadow.effect is not Effect.ALLOW:
        raise GrantRefused(
            f"`{route}` is covered by {shadow.matcher!r} in the "
            f"{shadow.effect.value!r} list of {shadow.source}, so an allow rule "
            f"would have no effect. Edit that file instead."
        )
    if shadow is not None:
        raise GrantRefused(f"`{route}` is already allowed by {shadow.matcher!r}.")

    existing, options = _read_local(path)
    rule = Rule(
        effect=Effect.ALLOW,
        matcher=route,
        source=path.name,
        index=len(existing),
    )
    _write_local(path, [*existing, rule], options)
    return rule


def prunable(path: Path, commands: dict[str, Command]) -> tuple[Rule, ...]:
    """Rules in the daemon's own file that cover nothing on this Omarchy.

    Read-only, and deliberately separate from the delta's `dead` list. Those two
    are different sets: the delta names rules that went dead *since the last
    acknowledgement*, which is a warning; this names rules that are dead *now*,
    which is housekeeping. A rule can be the second without ever having been the
    first, because it was already dead when the snapshot was taken.
    """
    try:
        existing, _ = _read_local(path)
    except PermissionsError:
        # A file that will not parse is not a file to offer to edit. The daemon
        # is already saying so somewhere louder.
        return ()
    return tuple(rule for rule in existing if not rule.covers(commands.values()))


def prune(path: Path, commands: dict[str, Command]) -> tuple[Rule, ...]:
    """Remove the dead rules from the daemon's own file. Returns what went.

    N10 deliberately never prunes on its own: a rule whose route vanished is the
    delta's evidence, and a daemon that quietly edits a file is one the user
    cannot reason about. This is the other half of that -- **user-initiated**,
    after being shown exactly what would go.

    `permissions.local.json` only. The user's own file is theirs to edit, and a
    daemon that tidied it would be editing a tracked file nobody asked it to
    touch.
    """
    existing, options = _read_local(path)
    dead = [rule for rule in existing if not rule.covers(commands.values())]
    if not dead:
        return ()

    kept = [rule for rule in existing if rule not in dead]
    _write_local(path, kept, options)
    return tuple(dead)


class RevokeRefused(RuntimeError):
    """A removal that must not happen, refused where the file is written."""


def revoke(path: Path, effect: Effect, matcher: str) -> tuple[Rule, ...]:
    """Remove the daemon's own ``allow`` rules for ``matcher``. Returns what went.

    **`allow` only, and only `permissions.local.json`.** The property that buys
    is worth stating as a sentence rather than as three checks: *no button on the
    panel can widen what an agent may do.* A `deny` or an `ask` somebody
    hand-added to this file is a live restriction, and removing one from a bar
    popup -- which is on screen during screen shares -- is a different act from
    withdrawing a grant. It is done in an editor, which the panel has a button
    for.

    **By identity, never by position.** `grant` appends whenever a parked call is
    answered, so an index minted before that answer names a different rule after
    it. That is the one failure mode this must not have.

    **Idempotent, and it takes every copy.** Nothing dedupes rules, identical
    entries are indistinguishable in the display, and removing one of a pair
    would be a button that visibly does nothing. Naming a rule that is not there
    is not an error: somebody already removed it, which is the outcome that was
    wanted.

    Unlike `grant`, this cannot be refused for safety at the moment of writing --
    it only ever narrows. The one failure is a file that will not parse, which is
    not a file to edit blind.
    """
    if effect is not Effect.ALLOW:
        raise RevokeRefused(
            f"only 'allow' rules can be removed here, not {effect.value!r}: a "
            f"restriction is a decision, and it is removed in an editor."
        )

    existing, options = _read_local(path)
    gone = [r for r in existing if r.effect is Effect.ALLOW and r.matcher == matcher]
    if not gone:
        return ()

    kept = [r for r in existing if r not in gone]
    _write_local(path, kept, options)
    return tuple(gone)


def seed(path: Path) -> bool:
    """Write a starting document at ``path`` if there is none. Never overwrites.

    Opened with ``x``, so "never overwrites" is a property of the syscall rather
    than of a check that could race an editor.

    This is the one thing that may create `permissions.json`, and the exception
    is narrow on purpose: a file that does not exist yet, created on a press,
    with the person's editor opening on it a moment later. What the invariant
    forbids is the daemon *rewriting* the file somebody checks into git. The
    template exists because an editor opening an empty buffer has no ``$schema``
    line, and that line is what validates a matcher before this module ever sees
    it.
    """
    # The three lists, empty and in precedence order, so the file a person opens
    # shows them the order they are read in.
    body = {effect.value: [] for effect in PRECEDENCE}
    document = {
        "$schema": SCHEMA_URL,
        "_comment": SEED_COMMENT,
        "permissions": body,
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        # ``"x"`` is exclusive-create: it fails if the file exists. That makes
        # "never overwrites" a guarantee from the operating system rather than
        # from an ``if path.exists()`` an editor could slip in between.
        with open(path, "x") as handle:
            json.dump(document, handle, indent=2)
            handle.write("\n")
    except FileExistsError:
        return False
    os.chmod(path, 0o600)
    return True


def _read_local(path: Path) -> tuple[list[Rule], Options]:
    """The daemon's own file as it stands. Missing reads as empty."""
    try:
        text = path.read_text()
    except FileNotFoundError:
        return [], Options()
    rules, options = parse(text, source=path.name)
    return list(rules), options


#: The document this daemon last wrote to `permissions.local.json`, digested.
#:
#: The reloader watches these files and announces what changed. What it must not
#: announce is a change the person is already looking at -- they pressed Always,
#: or Remove, or Prune two seconds ago, and a toast telling them the permissions
#: changed is the daemon reporting their own press back to them. Matching on the
#: content rather than passing a flag around means it works for every writer,
#: including `grant`, which is called from the gate and has never had a way to
#: reach the reloader.
_self_written = ""


def wrote_ourselves(raw: bytes | None) -> bool:
    """Whether ``raw`` is exactly the document this daemon last wrote itself."""
    if not raw or not _self_written:
        return False
    # A SHA-256 digest is a short fixed-length fingerprint of the bytes.
    # Comparing digests rather than the documents keeps one string in memory
    # instead of a whole file, and identical bytes always digest identically.
    return hashlib.sha256(raw).hexdigest() == _self_written


def _write_local(path: Path, rules: Sequence[Rule], options: Options) -> None:
    """Replace the file atomically, `0600`, in a `0700` directory.

    Temp file in the same directory, `fsync`, rename: a half-written permissions
    file read after a crash says something nobody chose, and a rename is the only
    way to make the replacement all-or-nothing.
    """
    body: dict[str, object] = {}
    named = options.named()
    if named:
        body.update(named)
    for effect in PRECEDENCE:
        matching = [r for r in rules if r.effect is effect]
        if matching:
            body[effect.value] = [{"kind": r.kind, "matcher": r.matcher} for r in matching]

    document = {
        "$schema": SCHEMA_URL,
        "_comment": LOCAL_COMMENT,
        "permissions": body,
    }

    text = json.dumps(document, indent=2) + "\n"
    # Recorded *before* the write, so the reloader's poll can never see the new
    # file ahead of the fingerprint for it. See `wrote_ourselves`.
    global _self_written
    _self_written = hashlib.sha256(text.encode()).hexdigest()

    path.parent.mkdir(parents=True, exist_ok=True)
    os.chmod(path.parent, 0o700)
    tmp = path.with_name(f".{path.name}.tmp")
    with open(tmp, "w") as handle:
        handle.write(text)
        handle.flush()
        os.fsync(handle.fileno())
    os.chmod(tmp, 0o600)
    os.replace(tmp, path)


def decide(
    cmd: Command, perms: Permissions, *, unreviewed: frozenset[str] = frozenset()
) -> Outcome:
    """What happens to ``cmd``. The tier and the document, in one call.

    The one entry point, so that a caller cannot apply the derivation and forget
    the rules -- or read the rules and forget that sudo beats them.
    """
    return evaluate(cmd.route, base_tier(cmd), perms, unreviewed=unreviewed)
