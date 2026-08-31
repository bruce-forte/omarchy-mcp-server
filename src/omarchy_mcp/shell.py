"""The other half of Omarchy: the running shell's IPC targets.

Anything drawn by ``omarchy-shell`` -- the bar, the notification stack, the OSD,
the media controller, every loaded plugin -- is reachable only through
Quickshell IPC, not through the command registry. ``qs ipc show`` lists every
target with its method signatures, and that listing is the only documentation
these interfaces have.
"""

from __future__ import annotations

import re
import subprocess
from dataclasses import dataclass
from functools import lru_cache

from .paths import OMARCHY_PATH

LIST_TIMEOUT_S = 15

_TARGET_RE = re.compile(r"^target\s+(?P<name>\S+)\s*$")
_FUNC_RE = re.compile(
    r"^\s+function\s+(?P<name>\w+)\((?P<params>[^)]*)\)\s*:\s*(?P<returns>\w+)\s*$"
)


@dataclass(frozen=True)
class Param:
    name: str
    type: str


@dataclass(frozen=True)
class Method:
    name: str
    params: tuple[Param, ...]
    returns: str

    @property
    def signature(self) -> str:
        inner = ", ".join(f"{p.name}: {p.type}" for p in self.params)
        return f"{self.name}({inner}): {self.returns}"


@dataclass(frozen=True)
class Target:
    name: str
    methods: tuple[Method, ...]


class ShellError(RuntimeError):
    pass


def parse_targets(listing: str) -> dict[str, Target]:
    """Parse the output of ``qs ipc show``."""
    targets: dict[str, list[Method]] = {}
    current: str | None = None

    for line in listing.splitlines():
        target_match = _TARGET_RE.match(line)
        if target_match:
            current = target_match.group("name")
            targets.setdefault(current, [])
            continue

        func_match = _FUNC_RE.match(line)
        if func_match and current is not None:
            raw_params = func_match.group("params").strip()
            params: list[Param] = []
            if raw_params:
                for chunk in raw_params.split(","):
                    name, _, type_ = chunk.partition(":")
                    params.append(Param(name.strip(), type_.strip() or "var"))
            targets[current].append(
                Method(func_match.group("name"), tuple(params), func_match.group("returns"))
            )

    return {
        name: Target(name, tuple(sorted(methods, key=lambda m: m.name)))
        for name, methods in targets.items()
    }


@lru_cache(maxsize=1)
def _list_raw() -> str:
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
    if refresh:
        _list_raw.cache_clear()
    return parse_targets(_list_raw())


def as_dict(target: Target) -> dict[str, object]:
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
