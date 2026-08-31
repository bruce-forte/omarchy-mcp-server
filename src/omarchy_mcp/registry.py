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
    pass


def _omarchy_version() -> str:
    """Identifies the installed Omarchy, so the cache drops on upgrade."""
    try:
        return (OMARCHY_PATH / "version").read_text().strip()
    except OSError:
        return "unknown"


def _parse(payload: str) -> dict[str, Command]:
    try:
        raw = json.loads(payload)
    except json.JSONDecodeError as exc:
        raise RegistryError(f"omarchy commands --json returned invalid JSON: {exc}") from exc

    commands: dict[str, Command] = {}
    for row in raw.get("commands") or []:
        route = row.get("route")
        if not route:
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


@lru_cache(maxsize=4)
def _load_for_version(_version: str) -> dict[str, Command]:
    # --all so that hidden commands are classified by policy too. They are
    # filtered out of search results unless explicitly asked for.
    try:
        proc = subprocess.run(
            ["omarchy", "commands", "--all", "--json"],
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
    return all_commands().get(route)


def groups() -> dict[str, list[Command]]:
    out: dict[str, list[Command]] = {}
    for cmd in all_commands().values():
        out.setdefault(cmd.group, []).append(cmd)
    return out


def search(query: str, *, limit: int = 20, include_hidden: bool = False) -> list[Command]:
    """Rank commands against a query.

    Substring matching on route, summary and group. Deliberately dumb: the
    registry is small and the caller is a language model that can re-query.
    """
    q = query.strip().lower()
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

    scored.sort(key=lambda pair: (pair[0], pair[1].route))
    return [cmd for _score, cmd in scored[:limit]]


def suggest(route: str, *, limit: int = 5) -> list[Command]:
    """Nearest routes for a route that does not exist.

    Whole-string matching finds nothing for a wrong route -- which is exactly
    when a suggestion is wanted -- so this falls back to the individual words.
    """
    hits = search(route, limit=limit)
    if hits:
        return hits

    words = [w for w in route.lower().replace("omarchy", "").split() if len(w) > 2]
    seen: dict[str, Command] = {}
    for word in words:
        for cmd in search(word, limit=limit):
            seen.setdefault(cmd.route, cmd)
    return list(seen.values())[:limit]


def as_dict(cmd: Command) -> dict[str, object]:
    return {
        "route": cmd.route,
        "group": cmd.group,
        "summary": cmd.summary,
        "args": cmd.args,
        "examples": list(cmd.examples),
        "requires_sudo": cmd.requires_sudo,
        "hidden": cmd.hidden,
    }
