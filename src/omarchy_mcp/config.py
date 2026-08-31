"""Configuration: optional TOML, sane defaults, never fatal.

A daemon that refuses to start over a typo'd config file looks exactly like a
daemon that was never installed. Bad configuration is reported and then ignored.
"""

from __future__ import annotations

import tomllib
from dataclasses import dataclass, field, replace
from pathlib import Path

from .paths import CONFIG_FILE

#: Anything above this and a single call could bury the agent's context.
DEFAULT_MAX_OUTPUT_B = 256 * 1024
DEFAULT_TIMEOUT_MS = 30_000
DEFAULT_PORT = 8765

#: How long a call may wait for the user to answer an approval prompt. Not yet
#: reachable: N4 owns the asking. Bounded rather than free so that neither
#: extreme can exist -- a second is not long enough for a person to read the
#: question, and ten minutes is a request parked on a desk nobody is at.
DEFAULT_ASK_TIMEOUT_S = 60
ASK_TIMEOUT_BOUNDS = (5, 600)


@dataclass(frozen=True)
class Config:
    port: int = DEFAULT_PORT
    timeout_ms: int = DEFAULT_TIMEOUT_MS
    max_output_b: int = DEFAULT_MAX_OUTPUT_B

    #: Guarded routes and groups promoted to runnable.
    allow: tuple[str, ...] = ()
    allow_groups: tuple[str, ...] = ()
    #: Safe routes demoted to guarded.
    deny: tuple[str, ...] = ()

    #: How long an approval prompt waits before the call is refused. No answer
    #: means denied: see `consent.py`.
    ask_timeout_s: int = DEFAULT_ASK_TIMEOUT_S

    #: Curated tools switched off. Still reachable through ``omarchy_run``.
    disabled_tools: tuple[str, ...] = ()

    log_level: str = "info"

    #: Problems found while loading, for the caller to report. Loading never
    #: raises: a broken file yields defaults plus an explanation.
    problems: tuple[str, ...] = field(default=(), compare=False)

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


def load(path: Path | None = None) -> Config:
    """Read the config file. Missing or broken yields defaults."""
    path = CONFIG_FILE if path is None else path
    if not path.exists():
        return Config()

    problems: list[str] = []
    try:
        raw = tomllib.loads(path.read_text())
    except (OSError, tomllib.TOMLDecodeError) as exc:
        return Config(problems=(f"{path} could not be read ({exc}); using defaults",))

    server = raw.get("server") or {}
    policy = raw.get("policy") or {}
    tools = raw.get("tools") or {}
    log = raw.get("log") or {}

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
        allow=_strs(policy.get("allow"), "policy.allow", problems),
        allow_groups=_strs(policy.get("allow_groups"), "policy.allow_groups", problems),
        deny=_strs(policy.get("deny"), "policy.deny", problems),
        ask_timeout_s=_int(
            policy.get("ask_timeout_s"),
            "policy.ask_timeout_s",
            DEFAULT_ASK_TIMEOUT_S,
            problems,
            *ASK_TIMEOUT_BOUNDS,
        ),
        disabled_tools=_strs(tools.get("disabled"), "tools.disabled", problems),
        log_level=level,
    )
    return replace(cfg, problems=tuple(problems))
