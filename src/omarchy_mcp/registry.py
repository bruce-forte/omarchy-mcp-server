"""The Omarchy command registry.

``omarchy commands --json`` is a complete, machine-readable description of every
command: route, arguments, summary, examples, group, and whether it needs sudo.
That listing is the reason this server needs no hand-written command catalogue
and does not rot when Omarchy is upgraded.
"""

from __future__ import annotations

import json
import subprocess
from dataclasses import dataclass
from functools import lru_cache

from .paths import OMARCHY_PATH

REGISTRY_TIMEOUT_S = 20


@dataclass(frozen=True)
class Command:
    """One row of ``omarchy commands --json``, e.g. ``omarchy theme set``."""

    #: The full command line without its arguments: ``"omarchy theme set"``.
    route: str
    binary: str
    group: str
    summary: str
    args: str
    examples: tuple[str, ...]
    requires_sudo: bool
    hidden: bool

    @property
    def argv_prefix(self) -> list[str]:
        """The route split into argv, e.g. ``["omarchy", "theme", "set"]``."""
        return self.route.split()


class RegistryError(RuntimeError):
    """The command listing could not be read or made sense of.

    ``pass`` is the empty body: the class needs no behaviour of its own, only a
    distinct name so callers can catch this and nothing else.
    """


def _omarchy_version() -> str:
    """Identifies the installed Omarchy, so the cache drops on upgrade."""
    try:
        return (OMARCHY_PATH / "version").read_text().strip()
    except OSError:
        return "unknown"


def _parse(payload: str) -> dict[str, Command]:
    """Turn the JSON listing into `Command` objects keyed by route."""
    try:
        raw = json.loads(payload)
    except json.JSONDecodeError as exc:
        # ``raise ... from exc`` keeps the original as the cause, so a traceback
        # shows both what failed and what this module made of it.
        raise RegistryError(f"omarchy commands --json returned invalid JSON: {exc}") from exc

    commands: dict[str, Command] = {}
    for row in raw.get("commands") or []:
        route = row.get("route")
        if not route:
            # A row with no route is not addressable, so there is nothing this
            # server could do with it. Skip rather than fail: one odd row must
            # not cost the whole registry.
            continue
        commands[route] = Command(
            route=route,
            binary=row.get("binary") or "",
            group=row.get("group") or "",
            summary=row.get("summary") or "",
            args=row.get("args") or "",
            examples=tuple(row.get("examples") or ()),
            requires_sudo=bool(row.get("requires_sudo")),
            hidden=bool(row.get("hidden")),
        )
    if not commands:
        raise RegistryError("omarchy commands --json listed no commands")
    return commands


# ``@lru_cache`` remembers the result per argument value, so this spawns
# ``omarchy commands`` once and every later call with the same version is a dict
# lookup. The version is an argument *only* so that it is part of the cache key:
# an ``omarchy update`` changes it, which misses the cache and re-reads the
# listing. Hence the leading underscore -- the body never looks at it.
@lru_cache(maxsize=4)
def _load_for_version(_version: str) -> dict[str, Command]:
    """Spawn ``omarchy commands --all --json`` and parse it."""
    # --all so that hidden commands are classified by policy too. They are
    # filtered out of search results unless explicitly asked for.
    #
    # Imported inside the function rather than at the top of the file:
    # ``execute`` imports from here, and two modules importing each other at
    # import time is a circular import. By the time this runs, both exist.
    from .execute import NotInstalled, resolve_binary

    argv = ["omarchy", "commands", "--all", "--json"]
    try:
        exe = resolve_binary(argv[0])
    except NotInstalled as exc:
        raise RegistryError(f"{exc} Is this an Omarchy system?") from exc

    try:
        proc = subprocess.run(
            argv,
            executable=exe,
            capture_output=True,
            text=True,
            timeout=REGISTRY_TIMEOUT_S,
        )
    except FileNotFoundError as exc:
        raise RegistryError("`omarchy` is not on PATH; is this an Omarchy system?") from exc
    except subprocess.TimeoutExpired as exc:
        raise RegistryError("omarchy commands --json timed out") from exc

    if proc.returncode != 0:
        raise RegistryError(f"omarchy commands --json failed: {proc.stderr.strip()[:200]}")
    return _parse(proc.stdout)


def all_commands() -> dict[str, Command]:
    """Every command, keyed by route. Cached until Omarchy's version changes."""
    return _load_for_version(_omarchy_version())


def get(route: str) -> Command | None:
    """The command with this exact route, or ``None`` if there is no such route."""
    return all_commands().get(route)


def groups() -> dict[str, list[Command]]:
    """Every command, bucketed by its group."""
    out: dict[str, list[Command]] = {}
    for cmd in all_commands().values():
        # ``setdefault`` returns the list for this group, inserting an empty one
        # first if the group has not been seen yet -- the one-line form of
        # "create the bucket if missing, then append".
        out.setdefault(cmd.group, []).append(cmd)
    return out


def search(query: str, *, limit: int = 20, include_hidden: bool = False) -> list[Command]:
    """Rank commands against a query.

    Substring matching on route, summary and group. Deliberately dumb: the
    registry is small and the caller is a language model that can re-query.
    """
    q = query.strip().lower()
    # (score, command) pairs, where a *lower* score is a better match.
    scored: list[tuple[int, Command]] = []
    for cmd in all_commands().values():
        if cmd.hidden and not include_hidden:
            continue
        route = cmd.route.lower()
        if not q:
            scored.append((3, cmd))
            continue
        if q == route or q == route.removeprefix("omarchy "):
            score = 0
        elif route.startswith(f"omarchy {q}") or route.startswith(q):
            score = 1
        elif q in route:
            score = 2
        elif q in cmd.summary.lower():
            score = 3
        elif q in cmd.group.lower():
            score = 4
        else:
            continue
        scored.append((score, cmd))

    # ``key`` says what to sort by: a ``lambda`` is a one-expression function
    # written inline. Sorting by the pair (score, route) puts better matches
    # first and breaks ties alphabetically, so the order is stable rather than
    # dependent on how the registry happened to be laid out.
    scored.sort(key=lambda pair: (pair[0], pair[1].route))
    # Take the best ``limit`` and drop the scores; ``_score`` is named with an
    # underscore to say it is deliberately unused.
    return [cmd for _score, cmd in scored[:limit]]


def suggest(route: str, *, limit: int = 5) -> list[Command]:
    """Nearest routes for a route that does not exist.

    Whole-string matching finds nothing for a wrong route -- which is exactly
    when a suggestion is wanted -- so this falls back to the individual words.
    """
    hits = search(route, limit=limit)
    if hits:
        return hits

    # Words of three letters or more; "omarchy" itself matches everything and so
    # tells the caller nothing.
    words = [w for w in route.lower().replace("omarchy", "").split() if len(w) > 2]
    # A dict keyed by route deduplicates commands that several words all found,
    # and keeps them in the order they were first seen -- Python dicts preserve
    # insertion order, so the best word's hits stay at the front.
    seen: dict[str, Command] = {}
    for word in words:
        for cmd in search(word, limit=limit):
            seen.setdefault(cmd.route, cmd)
    return list(seen.values())[:limit]


def as_dict(cmd: Command) -> dict[str, object]:
    """The JSON shape a command takes in a tool result or a resource."""
    return {
        "route": cmd.route,
        "group": cmd.group,
        "summary": cmd.summary,
        "args": cmd.args,
        "examples": list(cmd.examples),
        "requires_sudo": cmd.requires_sudo,
        "hidden": cmd.hidden,
    }
