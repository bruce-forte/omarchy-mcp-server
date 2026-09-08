"""Naming the thing before doing it.

An argument that reaches a command unchecked fails inside a subprocess, and the
failure arrives as somebody else's stderr. Worse, once N4 asks the user to
approve a call, an unchecked argument would put a question on screen about
something that does not exist -- and *"an agent wants to switch the theme"* is
not consent when the user cannot see which theme.

So every identifier an agent supplies is resolved against live system state
before anything is spawned, and resolution yields a :class:`Target`: the value
the command will actually receive, and a human name for it.

Three rules, all of which matter:

*Match the way Omarchy matches.* ``omarchy-theme-set`` lowercases its argument
and turns spaces into dashes before looking for the directory, so ``"tokyo
night"`` and ``"Tokyo Night"`` are the same theme to Omarchy and are the same
theme here. Nothing looser: a near miss is refused with the near misses named,
never silently corrected into a different theme.

*Not found is not the same as could not look.* A wrong name is worth retrying
with a better one; a compositor that is not answering is not. The agent is told
which it is, because they imply different next moves.

*Refuse rather than guess.* This is the property N4 depends on.

The module reads in three layers, marked by the banner comments below: the
*sources* that produce a listing, the *matching* that turns a name into a
`Target`, and the *route table* that says which resolver each command's
arguments go through. A resolver either returns a `Call` or raises
`Unresolvable`; there is no third outcome and no "probably fine".
"""

from __future__ import annotations

import difflib
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from . import desktop, execute, registry

#: Resolution runs before the command does, so it gets a short leash of its own
#: rather than the caller's timeout: a probe that hangs would turn every call
#: into a timeout.
PROBE_TIMEOUT_MS = 5_000

#: Enough of a listing to match against; a machine with more themes than this
#: has other problems.
PROBE_OUTPUT_B = 64 * 1024

#: What a browser may be pointed at. `file://` would turn "open a link" into
#: "read a file and render it", and `javascript:` is a code path, not a place.
URL_SCHEMES = ("http://", "https://")


@dataclass(frozen=True)
class Target:
    """A resolved identifier: what runs, and what to call it in front of a person."""

    #: What sort of thing this is: ``"theme"``, ``"monitor"``, ``"url"``...
    kind: str
    #: Exactly what the command will receive as an argument.
    value: str
    #: How it is described to a human in the approval prompt, which may be
    #: friendlier than the value -- a monitor's label carries its description.
    label: str


@dataclass(frozen=True)
class Call:
    """Arguments as the command will receive them, plus what they name."""

    args: list[str]
    target: Target | None = None


class Unresolvable(Exception):
    """An argument that names nothing real, or that could not be checked."""

    def __init__(
        self,
        kind: str,
        message: str,
        *,
        near: tuple[str, ...] = (),
        source_unavailable: bool = False,
    ) -> None:
        """``near`` are spelling suggestions; ``source_unavailable`` separates
        "there is no such theme" from "the theme list could not be read"."""
        super().__init__(message)
        self.kind = kind
        self.message = message
        self.near = near
        self.source_unavailable = source_unavailable

    def as_dict(self) -> dict[str, object]:
        """The JSON the agent receives instead of a command being run."""
        payload: dict[str, object] = {
            "error": self.message,
            "unresolved": self.kind,
            "reason": "source_unavailable" if self.source_unavailable else "not_found",
        }
        if self.near:
            payload["did_you_mean"] = list(self.near)
        return payload


# --------------------------------------------------------------------- sources
#
# One function per source of truth, each doing nothing but produce a listing.
# The tests pin these -- CI has neither an Omarchy nor a compositor -- so keep
# them free of matching logic.


def _themes() -> list[str]:
    """Installed themes, by display name, as Omarchy itself lists them."""
    cmd = registry.get("omarchy theme list")
    if cmd is None:
        raise Unresolvable(
            "theme",
            "could not list themes: this Omarchy has no `omarchy theme list`.",
            source_unavailable=True,
        )

    result = execute.run(
        cmd.argv_prefix, timeout_ms=PROBE_TIMEOUT_MS, max_output_b=PROBE_OUTPUT_B
    )
    if result.timed_out or result.exit_code != 0:
        detail = "timed out" if result.timed_out else f"exited {result.exit_code}"
        raise Unresolvable(
            "theme",
            f"could not list themes: `omarchy theme list` {detail}.",
            source_unavailable=True,
        )
    return [line.strip() for line in result.stdout.splitlines() if line.strip()]


def _monitors() -> list[dict[str, Any]]:
    """Connected outputs, from the compositor that names them."""
    try:
        monitors = desktop.hyprctl("monitors")
    except desktop.DesktopError as exc:
        raise Unresolvable(
            "monitor", f"could not list monitors: {exc}", source_unavailable=True
        ) from exc
    if not isinstance(monitors, list):
        raise Unresolvable(
            "monitor",
            "could not list monitors: hyprctl returned something unexpected.",
            source_unavailable=True,
        )
    return monitors


# --------------------------------------------------------------------- matching


def slug(name: str) -> str:
    """Omarchy's own idea of when two theme names are the same one.

    Mirrors `omarchy-theme-set`, which strips `<...>` placeholders, lowercases,
    and turns spaces into dashes before it looks for the directory. Matching any
    other way would let this module accept a name the command then rejects, or
    the reverse.
    """
    # ``re.sub(pattern, "", name)`` deletes every match. Read left to right:
    # drop ``<...>`` placeholders, trim the ends, lowercase, dashes for spaces.
    return re.sub(r"<[^>]+>", "", name).strip().lower().replace(" ", "-")


def _near(needle: str, candidates: dict[str, str]) -> tuple[str, ...]:
    """Display names close enough to be worth offering back.

    ``candidates`` maps the form being matched on to the form worth showing --
    slug to display name for themes -- so the comparison stays consistent while
    the suggestion stays readable.
    """
    # ``difflib`` is the standard library's fuzzy string matching. ``cutoff`` is
    # a similarity from 0 to 1; 0.6 offers a typo back without offering an
    # unrelated theme, and no suggestion at all is better than a misleading one.
    hits = difflib.get_close_matches(needle, list(candidates), n=3, cutoff=0.6)
    return tuple(candidates[h] for h in hits)


def theme(name: str) -> Target:
    """Resolve a theme name to the one Omarchy will accept."""
    # Keyed by slug so the lookup matches the way Omarchy matches; the value is
    # the display name, which is what a person should be shown.
    by_slug = {slug(t): t for t in _themes()}
    found = by_slug.get(slug(name))
    if found is None:
        raise Unresolvable(
            "theme",
            f"no theme named {name!r} is installed. "
            f"List them with omarchy_theme(action=\"list\").",
            near=_near(slug(name), by_slug),
        )
    return Target("theme", found, found)


def monitor(name: str) -> Target:
    """Resolve a monitor name to a connected output."""
    by_name = {m.get("name", ""): m for m in _monitors() if m.get("name")}
    found = by_name.get(name)
    if found is None:
        raise Unresolvable(
            "monitor",
            f"no monitor named {name!r} is connected. "
            f"Connected: {', '.join(sorted(by_name)) or 'none'}.",
            near=_near(name, {n: n for n in by_name}),
        )
    description = str(found.get("description") or "").strip()
    label = f"{name} ({description})" if description else name
    return Target("monitor", name, label)


def _absolute(path: str, kind: str) -> Path:
    """Expand `~` and insist on an absolute path.

    Nothing here runs through a shell, so `~/wall.png` reaches `execve` as a
    literal tilde and the command fails on a directory named `~`. Expanding it
    is not a rewrite of what the caller meant, it is what the caller meant. A
    relative path is refused rather than resolved: it would be relative to the
    daemon's working directory, which is nowhere the user is standing.
    """
    # ``expanduser`` turns a leading ``~`` into the home directory. It is a
    # string operation on the path, not a shell expansion -- nothing here runs a
    # shell, which is exactly why the tilde would otherwise survive.
    resolved = Path(path).expanduser()
    if not resolved.is_absolute():
        raise Unresolvable(
            kind,
            f"{path!r} is a relative path. Give an absolute path, or one starting "
            f"with ~; this server has no working directory the caller shares.",
        )
    return resolved


def existing_file(path: str, kind: str) -> Target:
    """A path that has to be there already: a wallpaper that is not is a mistake."""
    resolved = _absolute(path, kind)
    if not resolved.exists():
        raise Unresolvable(kind, f"no such file: {resolved}")
    if not resolved.is_file():
        raise Unresolvable(kind, f"not a file: {resolved}")
    return Target(kind, str(resolved), resolved.name)


def writable_path(path: str, kind: str = "path") -> Target:
    """A path that need not exist yet, in a directory that must.

    Opening a file that is not there yet is ordinary in an editor, so the file
    is not required -- but a typo in the directory is still a typo, and the
    editor would open a buffer that can never be saved.
    """
    resolved = _absolute(path, kind)
    if not resolved.parent.is_dir():
        raise Unresolvable(kind, f"no such directory: {resolved.parent}")
    return Target(kind, str(resolved), resolved.name)


def url(value: str) -> Target:
    """A URL a browser may be pointed at."""
    # ``startswith`` accepts a tuple and is True if any of them matches.
    if not value.startswith(URL_SCHEMES):
        raise Unresolvable(
            "url",
            f"{value!r} is not an http(s) URL. Only http:// and https:// are opened; "
            f"a local file is not a place to send the browser.",
        )
    return Target("url", value, value)


# ----------------------------------------------------------------- route table
#
# Each entry takes the arguments as the agent supplied them and returns them as
# the command will receive them, plus what they name. A route resolves its own
# arguments because their shape differs: `--monitor NAME` is a flag, a theme is
# the first word, and an editor path sits behind an optional switch.


def _theme_arg(args: list[str]) -> Call:
    """The theme is the first argument: `omarchy theme set <name>`."""
    # `omarchy theme remove` with no name opens a picker, and a picker is the
    # user choosing for themselves. Nothing to resolve, nothing to refuse.
    if not args or not args[0]:
        return Call(list(args))
    target = theme(args[0])
    # The resolved value replaces the one the agent gave; ``*args[1:]`` unpacks
    # every remaining argument after it, untouched.
    return Call([target.value, *args[1:]], target)


def _background(args: list[str]) -> Call:
    """The wallpaper is the first argument, and it has to exist."""
    if not args or not args[0]:
        return Call(list(args))
    target = existing_file(args[0], "image")
    return Call([target.value, *args[1:]], target)


def _monitor_flag(args: list[str]) -> Call:
    """`omarchy brightness display [--monitor name] ...`"""
    # A copy, so a resolver never edits the caller's list in place.
    out = list(args)
    try:
        # ``index`` raises ValueError when the flag is absent, which here just
        # means there is no monitor to resolve.
        at = out.index("--monitor")
    except ValueError:
        return Call(out)
    if at + 1 >= len(out):
        raise Unresolvable("monitor", "--monitor was given without a monitor name.")
    target = monitor(out[at + 1])
    out[at + 1] = target.value
    return Call(out, target)


def _editor_path(args: list[str]) -> Call:
    """`omarchy launch editor [--inline] <path>` -- the path is the last word."""
    out = list(args)
    # ``enumerate`` yields (index, value) pairs, so this collects the positions
    # of the non-flag arguments rather than the arguments themselves -- the
    # position is what is needed to write the resolved value back.
    positional = [i for i, a in enumerate(out) if not a.startswith("-")]
    if not positional:
        return Call(out)
    at = positional[0]
    target = writable_path(out[at])
    out[at] = target.value
    return Call(out, target)


def _url_arg(args: list[str], *, optional: bool) -> Call:
    """The URL is the first argument. ``optional`` is for `launch browser`,
    which opens the home page when given nothing."""
    if not args or not args[0]:
        if optional:
            return Call(list(args))
        raise Unresolvable("url", "no URL was given.")
    target = url(args[0])
    return Call([target.value, *args[1:]], target)


#: Routes whose arguments name something checkable. Everything absent from this
#: table passes through untouched: package names have no cheap local truth, and
#: refusing one that is not installed yet would refuse every install.
RESOLVERS = {
    "omarchy theme set": _theme_arg,
    "omarchy theme remove": _theme_arg,
    "omarchy theme dir": _theme_arg,
    "omarchy theme bg set": _background,
    "omarchy brightness display": _monitor_flag,
    "omarchy launch editor": _editor_path,
    "omarchy launch config editor": _editor_path,
    # Every entry is a function taking one argument. ``_url_arg`` takes two, so
    # a ``lambda`` fixes the second and leaves a one-argument function behind.
    "omarchy launch webapp": lambda args: _url_arg(args, optional=False),
    "omarchy launch browser": lambda args: _url_arg(args, optional=True),
}


def resolve_call(route: str, args: list[str]) -> Call:
    """Resolve a route's arguments, or refuse.

    The single gate both `omarchy_run` and the curated tools pass through, so
    that reaching a command by the generic route cannot skip the check.
    """
    # Looking the function up in a dict and then calling it: functions are
    # ordinary values in Python and can be stored in one like anything else.
    resolver = RESOLVERS.get(route)
    if resolver is None:
        return Call(list(args))
    return resolver(list(args))
