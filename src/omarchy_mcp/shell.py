"""The other half of Omarchy: the running shell's IPC targets.

Anything drawn by ``omarchy-shell`` -- the bar, the notification stack, the OSD,
the media controller, every loaded plugin -- is reachable only through
Quickshell IPC, not through the command registry. ``qs ipc show`` lists every
target with its method signatures, and that listing is the only documentation
these interfaces have.

So this module is a small parser. ``qs ipc show`` prints something like::

    target omarchy.media
      function playPause(): void
      function seek(seconds: int): bool

and the two regular expressions below pick the two kinds of line out of it.
"""

from __future__ import annotations

import re
import subprocess
from dataclasses import dataclass
from functools import lru_cache

from .paths import OMARCHY_PATH

LIST_TIMEOUT_S = 15

#: A regular expression is a pattern for matching text. The ``r"..."`` prefix
#: makes it a raw string, so a backslash reaches the regex engine rather than
#: being read as a Python escape -- always used for patterns.
#:
#: This one: start of line, ``target``, whitespace, then one or more non-space
#: characters captured under the name ``name``, then optional trailing space and
#: end of line. ``(?P<name>...)`` is what makes the piece retrievable later by
#: ``match.group("name")`` instead of by number.
_TARGET_RE = re.compile(r"^target\s+(?P<name>\S+)\s*$")
#: The method lines, which are indented under their target. ``[^)]*`` is
#: "anything that is not a closing bracket", which is the parameter list.
_FUNC_RE = re.compile(
    r"^\s+function\s+(?P<name>\w+)\((?P<params>[^)]*)\)\s*:\s*(?P<returns>\w+)\s*$"
)


@dataclass(frozen=True)
class Param:
    """One parameter of an IPC method: ``seconds: int``."""

    name: str
    #: Named ``type`` even though that shadows the builtin ``type()``, because
    #: this is a field on a dataclass rather than a variable -- it is only ever
    #: reached as ``param.type``, so nothing is actually hidden.
    type: str


@dataclass(frozen=True)
class Method:
    """One callable function on a target."""

    name: str
    params: tuple[Param, ...]
    returns: str

    @property
    def signature(self) -> str:
        """The method as it reads in the listing: ``seek(seconds: int): bool``."""
        inner = ", ".join(f"{p.name}: {p.type}" for p in self.params)
        return f"{self.name}({inner}): {self.returns}"


@dataclass(frozen=True)
class Target:
    """One IPC target -- a plugin or a piece of the shell -- and what it offers."""

    name: str
    methods: tuple[Method, ...]


class ShellError(RuntimeError):
    """The shell could not be listed: not running, or ``qs`` not installed."""


def parse_targets(listing: str) -> dict[str, Target]:
    """Parse the output of ``qs ipc show``."""
    targets: dict[str, list[Method]] = {}
    #: Which target the indented lines currently belong to. The format is
    #: positional -- a method line says nothing about its own target -- so
    #: parsing has to remember the last heading it saw.
    current: str | None = None

    for line in listing.splitlines():
        target_match = _TARGET_RE.match(line)
        if target_match:
            current = target_match.group("name")
            # Register it even if it turns out to have no methods, so a target
            # that exists but offers nothing is still reported as existing.
            targets.setdefault(current, [])
            continue

        func_match = _FUNC_RE.match(line)
        if func_match and current is not None:
            raw_params = func_match.group("params").strip()
            params: list[Param] = []
            if raw_params:
                for chunk in raw_params.split(","):
                    # ``type_`` with a trailing underscore is the convention for
                    # a name that would otherwise shadow a builtin -- here
                    # ``type()``. ``or "var"`` covers an untyped parameter, which
                    # is what QML calls a value of any type.
                    name, _, type_ = chunk.partition(":")
                    params.append(Param(name.strip(), type_.strip() or "var"))
            targets[current].append(
                Method(func_match.group("name"), tuple(params), func_match.group("returns"))
            )

    # A dict comprehension: the same idea as a list comprehension, building
    # ``key: value`` pairs. Methods are sorted by name so the listing an agent
    # sees does not depend on the order the shell happened to register them.
    return {
        name: Target(name, tuple(sorted(methods, key=lambda m: m.name)))
        for name, methods in targets.items()
    }


# Cached with no arguments at all, so the subprocess runs once for the life of
# the process. ``targets(refresh=True)`` clears it -- see below.
@lru_cache(maxsize=1)
def _list_raw() -> str:
    """The raw text of ``qs ipc show``, or raise `ShellError` explaining why not."""
    # Imported here rather than at module level to avoid a circular import; see
    # the same note in `registry.py`.
    from .execute import NotInstalled, resolve_binary

    argv = ["qs", "ipc", "-n", "-p", str(OMARCHY_PATH / "shell"), "show"]
    try:
        exe = resolve_binary(argv[0])
    except NotInstalled as exc:
        raise ShellError(f"{exc} Is Quickshell installed?") from exc

    try:
        proc = subprocess.run(
            argv,
            executable=exe,
            capture_output=True,
            text=True,
            timeout=LIST_TIMEOUT_S,
        )
    except FileNotFoundError as exc:
        raise ShellError("`qs` (Quickshell) is not on PATH") from exc
    except subprocess.TimeoutExpired as exc:
        raise ShellError("`qs ipc show` timed out; is omarchy-shell running?") from exc

    if proc.returncode != 0:
        raise ShellError(
            f"`qs ipc show` failed: {proc.stderr.strip()[:200] or 'is omarchy-shell running?'}"
        )
    return proc.stdout


def targets(*, refresh: bool = False) -> dict[str, Target]:
    """Every IPC target the running shell exposes, keyed by name.

    Cached, because plugins register at shell start. ``refresh`` re-reads it,
    which is what you want after enabling a plugin.
    """
    # ``cache_clear`` is a method ``@lru_cache`` attaches to the function it
    # wraps; calling it makes the next call spawn ``qs`` again.
    if refresh:
        _list_raw.cache_clear()
    return parse_targets(_list_raw())


def as_dict(target: Target) -> dict[str, object]:
    """The JSON shape a target takes in `omarchy_shell_targets`."""
    return {
        "target": target.name,
        "methods": [
            {
                "name": m.name,
                "signature": m.signature,
                "params": [{"name": p.name, "type": p.type} for p in m.params],
                "returns": m.returns,
            }
            for m in target.methods
        ],
    }


def call_argv(target: str, method: str, args: list[str]) -> list[str]:
    """The argv for an IPC call. Kept separate so it can be tested without a shell."""
    return ["omarchy-shell", target, method, *args]
