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

This module is pure. It reads text and returns values; it spawns nothing, reads
no registry of its own, and decides nothing about the running daemon. What to do
about a document that will not load -- refuse to start, keep the last good one --
belongs to the caller, and the two answers are different.
"""

from __future__ import annotations

import json
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator

from .policy import NEVER_STORE, Tier
from .registry import Command

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


class PermissionsBlock(BaseModel):
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

    deny: list[RuleModel] = Field(
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
    #: Which file set `guarded_default`, for the explainer. Empty means nobody
    #: did and the value is this module's own.
    guarded_default_source: str = ""

    def matching(self, route: str) -> Rule | None:
        """The rule that decides ``route``, or ``None``.

        `rules` is already in precedence order, so this is the first match --
        which is the whole rule, stated once, in one place.
        """
        return next((r for r in self.rules if r.matches(route)), None)


# --- reading ----------------------------------------------------------------


def parse(text: str, *, source: str) -> tuple[tuple[Rule, ...], Effect | None]:
    """Parse one file. Raises `PermissionsError` on any defect at all.

    Returns the file's rules in precedence order and its `guardedDefault`, or
    ``None`` when it did not set one -- which is different from setting it to the
    default, because two files must not both claim it.
    """
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

    default = Effect(block.guardedDefault) if block.guardedDefault is not None else None
    return tuple(rules), default


def _explain(exc: ValidationError, source: str) -> str:
    """Turn pydantic's errors into something a person can act on.

    The parse error is the only thing standing between a stray comma and a daemon
    that will not start, so it names the file, the key, the index and the value,
    and repeats the two legal forms rather than assuming they are remembered.
    """
    lines = [f"{source} is not a valid permissions document:"]
    for err in exc.errors():
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
    pooled: dict[Effect, list[Rule]] = {effect: [] for effect in PRECEDENCE}
    default = DEFAULT_GUARDED
    default_source = ""

    for path in paths:
        try:
            text = path.read_text()
        except FileNotFoundError:
            continue
        except OSError as exc:
            raise PermissionsError(f"{path} could not be read: {exc}") from exc

        rules, file_default = parse(text, source=path.name)
        for rule in rules:
            pooled[rule.effect].append(rule)

        if file_default is not None:
            if default_source:
                raise PermissionsError(
                    f"{path.name} also sets guardedDefault, which {default_source} "
                    f"already set. It decides one thing, so it belongs in one file."
                )
            default = file_default
            default_source = path.name

    ordered = tuple(rule for effect in PRECEDENCE for rule in pooled[effect])
    return Permissions(ordered, default, default_source)


# --- what a rule turns out to mean ------------------------------------------


@dataclass(frozen=True)
class Finding:
    """A rule that does not do what it looks like it does.

    Two levels, and the difference is whether the user can be told anything
    useful:

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
    """

    rule: Rule
    level: Literal["error", "void"]
    reason: str


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
    return tuple(findings)


def errors(findings: Iterable[Finding]) -> tuple[Finding, ...]:
    return tuple(f for f in findings if f.level == "error")


# --- the ladder -------------------------------------------------------------


@dataclass(frozen=True)
class Outcome:
    """What happens to one route, and what decided it."""

    effect: Effect
    #: The rule that decided, or ``None`` when nothing matched and the answer came
    #: from the tier or from `guardedDefault`.
    rule: Rule | None
    reason: str

    @property
    def by_rule(self) -> bool:
        return self.rule is not None


def evaluate(route: str, tier: Tier, perms: Permissions) -> Outcome:
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
            None,
            f"`{route}` requires sudo. The MCP server runs without a controlling "
            f"terminal, so a password prompt could never be answered. No "
            f"permission rule changes that.",
        )

    rule = perms.matching(route)
    if rule is not None:
        outcome = Outcome(
            rule.effect,
            rule,
            f"`{route}` is matched by {rule.matcher!r} in the "
            f"{rule.effect.value!r} list of {rule.source}.",
        )
    elif tier is Tier.GUARDED:
        outcome = Outcome(
            perms.guarded_default,
            None,
            f"`{route}` can change the system in ways that are hard to undo, and "
            f"no permission rule covers it.",
        )
    else:
        return Outcome(Effect.ALLOW, None, "")

    if outcome.effect is Effect.ALLOW and route in NEVER_STORE:
        return Outcome(
            Effect.ASK,
            outcome.rule,
            f"`{route}` takes a command line as its argument, so it is asked about "
            f"every time and never granted outright.",
        )
    return outcome
