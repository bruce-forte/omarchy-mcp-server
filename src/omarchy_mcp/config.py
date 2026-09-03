"""Configuration: optional TOML, sane defaults, never fatal.

A daemon that refuses to start over a typo'd config file looks exactly like a
daemon that was never installed. Bad configuration is reported and then ignored.
"""

from __future__ import annotations

import tomllib
from dataclasses import dataclass, field, replace
from pathlib import Path

from .paths import ACTIVITY_FILE, CONFIG_FILE

#: Anything above this and a single call could bury the agent's context.
DEFAULT_MAX_OUTPUT_B = 256 * 1024
DEFAULT_TIMEOUT_MS = 30_000
DEFAULT_PORT = 8765

#: Keys that used to live under `[policy]` and now live in permissions.json.
#:
#: Named rather than ignored. An unknown TOML key is dropped silently, which is
#: how somebody comes to believe they still have a `deny` list -- the same
#: silent no-op the permissions document exists to avoid. See `_dead`.
MOVED_POLICY_KEYS = {
    "allow": 'an "allow" rule',
    "allow_groups": 'an "allow" rule with a trailing " *"',
    "deny": 'a "deny" rule',
    "ask": '"guardedDefault"',
    "ask_timeout_s": '"askTimeoutSeconds"',
}

#: Roughly 3-5k calls live, and one rotated generation kept beside it. Weeks of
#: ordinary use, bounded so that a hand-edited value cannot quietly fill a small
#: home partition, and floored so a rotation is not a thing that happens hourly.
DEFAULT_ACTIVITY_MAX_B = 1024 * 1024
ACTIVITY_MAX_B_BOUNDS = (64 * 1024, 64 * 1024 * 1024)


@dataclass(frozen=True)
class Config:
    port: int = DEFAULT_PORT
    timeout_ms: int = DEFAULT_TIMEOUT_MS
    max_output_b: int = DEFAULT_MAX_OUTPUT_B

    # What an agent may run is not here. It moved to permissions.json, because
    # two files that both decide it is a second source of truth, and because the
    # rules wanted a shape TOML arrays could not carry -- see `permissions.py`.

    #: Curated tools switched off. Still reachable through ``omarchy_run``.
    disabled_tools: tuple[str, ...] = ()

    log_level: str = "info"

    #: Whether tool calls are appended to the activity log. On by default: it is
    #: the only record of what an agent did that outlives the daemon. Off is
    #: offered because the file holds command arguments, which can be text the
    #: user typed or copied.
    activity: bool = True
    activity_max_bytes: int = DEFAULT_ACTIVITY_MAX_B
    #: A filename, never a path -- see `_name`.
    activity_file: str = ACTIVITY_FILE

    #: Problems found while loading, for the caller to report. Loading never
    #: raises: a broken file yields defaults plus an explanation.
    problems: tuple[str, ...] = field(default=(), compare=False)

    #: Whether the file parsed at all. False means every value here is a
    #: default standing in for a file that could not be read.
    #:
    #: At startup that distinction does not matter -- defaults are the safe
    #: floor and a daemon that refuses to start over a typo is a daemon that
    #: looks uninstalled. At *reload* it is the whole question: an unparseable
    #: file is not an instruction to switch every disabled tool back on. See
    #: `reload.py`.
    parsed: bool = field(default=True, compare=False)

    # The listen address is deliberately absent. This server executes commands
    # on the desktop; binding beyond loopback would put that behind a single
    # bearer token. If it is ever wanted it is a named, documented feature.
    host: str = "127.0.0.1"


def _strs(raw: object, key: str, problems: list[str]) -> tuple[str, ...]:
    if raw is None:
        return ()
    if not isinstance(raw, list) or not all(isinstance(x, str) for x in raw):
        problems.append(f"{key} must be a list of strings; ignoring it")
        return ()
    return tuple(raw)


def _bool(raw: object, key: str, default: bool, problems: list[str]) -> bool:
    if raw is None:
        return default
    if not isinstance(raw, bool):
        problems.append(f"{key} must be true or false; using {str(default).lower()}")
        return default
    return raw


def _int(raw: object, key: str, default: int, problems: list[str], lo: int, hi: int) -> int:
    if raw is None:
        return default
    if not isinstance(raw, int) or isinstance(raw, bool):
        problems.append(f"{key} must be an integer; using {default}")
        return default
    if not lo <= raw <= hi:
        problems.append(f"{key} must be between {lo} and {hi}; using {default}")
        return default
    return raw


def _name(raw: object, key: str, default: str, problems: list[str]) -> str:
    """A bare filename, joined onto the state directory by the caller.

    Not a path. Omarchy reloads the shell on any write inside the plugin
    directory, so a log that could be pointed there would reload the shell once
    per tool call -- which reads as a broken plugin, not as a bad setting. A
    name with no separator in it cannot address anywhere at all.
    """
    if raw is None:
        return default
    if not isinstance(raw, str) or not raw.strip():
        problems.append(f"{key} must be a filename; using {default}")
        return default
    name = raw.strip()
    if "/" in name or "\\" in name or name in (".", ".."):
        problems.append(
            f"{key} must be a bare filename, not a path; it is always created in "
            f"the state directory. Using {default}"
        )
        return default
    return name


def _dead(policy: dict, problems: list[str]) -> None:
    """Report a `[policy]` key that no longer does anything.

    The rest of this module reports a *malformed* value and carries on. This
    reports a well-formed one, because it is the more dangerous case: a
    `policy.deny` list sitting in a file nobody reads is a protection the user
    believes they have.
    """
    for key in sorted(policy):
        if key in MOVED_POLICY_KEYS:
            problems.append(
                f"policy.{key} has moved and does nothing here. Write "
                f"{MOVED_POLICY_KEYS[key]} in ~/.config/omarchy/mcp/permissions.json "
                f"instead, and delete this key."
            )
        else:
            problems.append(f"policy.{key} is not a setting; the [policy] table has moved")


def load(path: Path | None = None) -> Config:
    """Read the config file. Missing or broken yields defaults."""
    path = CONFIG_FILE if path is None else path
    if not path.exists():
        return Config()

    problems: list[str] = []
    try:
        raw = tomllib.loads(path.read_text())
    except (OSError, tomllib.TOMLDecodeError) as exc:
        return Config(
            problems=(f"{path} could not be read ({exc}); using defaults",),
            parsed=False,
        )

    server = raw.get("server") or {}
    tools = raw.get("tools") or {}
    log = raw.get("log") or {}

    _dead(raw.get("policy") or {}, problems)

    level = log.get("level", "info")
    if level not in ("debug", "info", "warn", "error"):
        problems.append(f"log.level {level!r} is not one of debug/info/warn/error; using info")
        level = "info"

    cfg = Config(
        port=_int(server.get("port"), "server.port", DEFAULT_PORT, problems, 1024, 65535),
        timeout_ms=_int(
            server.get("timeout_ms"), "server.timeout_ms", DEFAULT_TIMEOUT_MS, problems, 100, 600_000
        ),
        max_output_b=_int(
            server.get("max_output_b"),
            "server.max_output_b",
            DEFAULT_MAX_OUTPUT_B,
            problems,
            1024,
            16 * 1024 * 1024,
        ),
        disabled_tools=_strs(tools.get("disabled"), "tools.disabled", problems),
        log_level=level,
        activity=_bool(log.get("activity"), "log.activity", True, problems),
        activity_max_bytes=_int(
            log.get("activity_max_bytes"),
            "log.activity_max_bytes",
            DEFAULT_ACTIVITY_MAX_B,
            problems,
            *ACTIVITY_MAX_B_BOUNDS,
        ),
        activity_file=_name(
            log.get("activity_file"), "log.activity_file", ACTIVITY_FILE, problems
        ),
    )
    return replace(cfg, problems=tuple(problems))
