"""Configuration: optional TOML, sane defaults, never fatal.

A daemon that refuses to start over a typo'd config file looks exactly like a
daemon that was never installed. Bad configuration is reported and then ignored.

The shape of this module is one idea repeated. `load` reads the file into plain
dicts, then every individual setting goes through a small ``_strs`` / ``_bool``
/ ``_int`` / ``_name`` helper that takes the raw value and a shared ``problems``
list. A helper never raises: it either returns a value it has checked, or it
appends a sentence a human can act on and returns the default. What comes back
is therefore always a usable `Config`, with any complaints attached to it.
"""

from __future__ import annotations

import tomllib
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any

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
    """Everything the daemon is currently running under, except permissions.

    ``frozen=True`` makes instances read-only: ``config.port = 9000`` raises
    rather than silently changing what a call in flight is running under.
    Reconfiguring means building a *new* `Config` and pointing `settings.py` at
    it, which is why nothing here needs a lock.
    """

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
    #:
    #: ``tuple[str, ...]`` is a tuple of any number of strings -- a tuple rather
    #: than a list because a frozen dataclass holding a list would still let a
    #: caller append to it. ``compare=False`` keeps it out of the generated
    #: ``__eq__``, so two configs with the same settings count as equal even if
    #: one of them had complaints on the way in.
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
    """A list of strings, or nothing plus a complaint.

    ``raw: object`` because it came out of a TOML file and could be anything at
    all; ``key`` names it in the complaint, and ``problems`` is the shared list
    every helper appends to.
    """
    if raw is None:
        return ()
    # ``isinstance(x, T)`` asks whether a value is of that type. Checked rather
    # than assumed: the type hints in this file are not enforced at runtime, and
    # this value came from a user-edited file.
    if not isinstance(raw, list) or not all(isinstance(x, str) for x in raw):
        problems.append(f"{key} must be a list of strings; ignoring it")
        return ()
    return tuple(raw)


def _bool(raw: object, key: str, default: bool, problems: list[str]) -> bool:
    """A true/false setting, or ``default`` plus a complaint."""
    if raw is None:
        return default
    if not isinstance(raw, bool):
        problems.append(f"{key} must be true or false; using {str(default).lower()}")
        return default
    return raw


def _int(raw: object, key: str, default: int, problems: list[str], lo: int, hi: int) -> int:
    """An integer within ``lo``..``hi``, or ``default`` plus a complaint.

    The bounds are not decoration: they are what stops a hand-edited file from
    setting a zero timeout or a port this daemon may not bind.
    """
    if raw is None:
        return default
    # ``bool`` is a subclass of ``int`` in Python -- ``isinstance(True, int)`` is
    # True and ``True + 1`` is 2 -- so a stray ``true`` would otherwise sail
    # through as the number 1.
    if not isinstance(raw, int) or isinstance(raw, bool):
        problems.append(f"{key} must be an integer; using {default}")
        return default
    # Python chains comparisons: this reads as ``lo <= raw and raw <= hi``.
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


def _dead(policy: dict[str, Any], problems: list[str]) -> None:
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

    # The list every helper below appends to. One list, passed down, rather than
    # each helper returning a (value, problems) pair and the caller stitching
    # them back together.
    problems: list[str] = []
    try:
        # ``tomllib`` is TOML support in the standard library since 3.11 -- read
        # only, which is all this needs. The daemon never writes this file.
        raw = tomllib.loads(path.read_text())
    # A tuple of exception types catches any of them. OSError covers an
    # unreadable file; TOMLDecodeError covers a malformed one.
    except (OSError, tomllib.TOMLDecodeError) as exc:
        return Config(
            problems=(f"{path} could not be read ({exc}); using defaults",),
            parsed=False,
        )

    # ``.get`` returns None for a missing key instead of raising; ``or {}`` also
    # covers a key present but empty, so the lookups below are always safe.
    server = raw.get("server") or {}
    tools = raw.get("tools") or {}
    log = raw.get("log") or {}

    _dead(raw.get("policy") or {}, problems)

    level = log.get("level", "info")
    if level not in ("debug", "info", "warn", "error"):
        # ``!r`` inside an f-string inserts the *repr* -- quoted, escapes visible
        # -- so a level of "" or " info" reads as something rather than nothing.
        problems.append(f"log.level {level!r} is not one of debug/info/warn/error; using info")
        level = "info"

    cfg = Config(
        port=_int(server.get("port"), "server.port", DEFAULT_PORT, problems, 1024, 65535),
        timeout_ms=_int(
            server.get("timeout_ms"),
            "server.timeout_ms",
            DEFAULT_TIMEOUT_MS,
            problems,
            100,
            600_000,
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
            # ``*`` unpacks the two-element tuple into the ``lo`` and ``hi``
            # arguments, keeping the bounds defined in one place above.
            *ACTIVITY_MAX_B_BOUNDS,
        ),
        activity_file=_name(
            log.get("activity_file"), "log.activity_file", ACTIVITY_FILE, problems
        ),
    )
    # The dataclass is frozen, so the complaints cannot be assigned afterwards.
    # ``replace`` builds a copy with that one field changed, which is the frozen
    # equivalent of ``cfg.problems = ...``.
    return replace(cfg, problems=tuple(problems))
