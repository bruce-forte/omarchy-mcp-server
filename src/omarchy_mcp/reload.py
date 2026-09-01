"""Re-reading the config file while the daemon is serving.

`enabled(...)` used to be evaluated when tools were registered, so a tool
switched off in `config.toml` was never registered and never appeared in
`tools/list`. That half was already right, and it is the half that matters: a
tool the model cannot see is one prompt injection cannot talk it into trying.
What was missing is that the decision was frozen until a restart -- a session
started before the edit kept offering a tool that now refuses, or kept hiding
one just granted.

What reloads, and what does not:

| Key | Live |
|-----|------|
| `tools.disabled` | yes -- the tool is added or removed |
| `policy.allow`, `allow_groups`, `deny`, `ask`, `ask_timeout_s` | yes |
| `server.timeout_ms`, `max_output_b` | yes |
| `server.port` | no -- the socket is already bound |
| `log.activity_file`, `activity_max_bytes` | no -- the sink is already open |

The restart-only keys are not refused or reported specially. They are read into
the live config like everything else and simply have no reader left; saying so
in the README is cheaper than machinery for an edit nobody makes twice.

Two failure rules, because the file is edited by hand while the daemon reads it:

- **An unparseable file changes nothing.** `config.load` returns defaults for a
  file it cannot read, which is right at startup and wrong here: a stray
  keystroke would empty `policy.deny` and switch every disabled tool back on.
  The running config stands, and the person is told.
- **A file that has gone missing waits one poll.** Editors write a temporary
  file and rename it over the target, so "absent" is a normal thing to catch
  mid-save. Absent twice in a row is a deliberate deletion, which resets to
  defaults.

The trigger is a poll rather than a file watch because the process that reacts
should own it: a reload works when the shell is mid-restart, and when the daemon
is run by hand outside the shell entirely. `SIGHUP` short-circuits the wait, so
`omarchy-shell <id> reloadConfig` is immediate for someone typing it.
"""

from __future__ import annotations

import signal
from collections.abc import Awaitable, Callable
from contextlib import asynccontextmanager
from dataclasses import dataclass
from pathlib import Path

import anyio
import anyio.to_thread

from . import config as config_module, notify
from .config import Config
from .paths import CONFIG_FILE
from .settings import Settings
from .tools.catalogue import Catalogue, Change

#: How long the daemon can be running the previous config after a save. Short
#: enough that saving the file and switching to an agent feels immediate, long
#: enough that this is one `read()` of a small file every two seconds.
POLL_S = 2.0

#: The keys whose change alters what an agent is allowed to run. A change to any
#: of these is announced on the desktop -- see `_policy_of`.
POLICY_KEYS = ("allow", "allow_groups", "deny", "ask", "ask_timeout_s")


def _policy_of(config: Config) -> tuple:
    return tuple(getattr(config, key) for key in POLICY_KEYS)


def _describe(before: Config, after: Config) -> str:
    """What changed about the rules, in the words of the config file."""
    parts = []
    for key in ("allow", "allow_groups", "deny"):
        was, now = set(getattr(before, key)), set(getattr(after, key))
        for added in sorted(now - was):
            parts.append(f"+{key}: {added}")
        for gone in sorted(was - now):
            parts.append(f"-{key}: {gone}")
    if before.ask != after.ask:
        parts.append(f"ask: {str(after.ask).lower()}")
    if before.ask_timeout_s != after.ask_timeout_s:
        parts.append(f"ask_timeout_s: {after.ask_timeout_s}")
    return ", ".join(parts)


@dataclass(frozen=True)
class Reloaded:
    """What one reload did. Falsy when the file was read and meant nothing new."""

    config: Config
    tools: Change = Change()
    policy_changed: bool = False
    rejected: bool = False

    def __bool__(self) -> bool:
        return bool(self.tools) or self.policy_changed


class Reloader:
    """Watches one config file and swaps what the daemon runs under."""

    def __init__(
        self,
        settings: Settings,
        catalogue: Catalogue,
        mcp,
        log,
        *,
        path: Path = CONFIG_FILE,
        on_change: Callable[[Reloaded], None] | None = None,
        announce: Callable[[], Awaitable[None]] | None = None,
    ) -> None:
        self._settings = settings
        self._catalogue = catalogue
        self._mcp = mcp
        self._log = log
        self._path = path
        self._on_change = on_change
        self._announce = announce

        # What was on disk when the daemon started, so the first poll does not
        # report the file it has already read as an edit.
        self._seen = self._read()
        self._absences = 0
        self._rejected = False
        self._wake: anyio.Event | None = None

    # -- reading -------------------------------------------------------------

    def _read(self) -> bytes | None:
        """The file's bytes, or None if it is not there.

        Content rather than mtime: the file is small, comparing bytes needs no
        clock, and it makes "the same broken file, still broken" distinguishable
        from a fresh save -- which is what keeps a rejection from toasting every
        two seconds.
        """
        try:
            return self._path.read_bytes()
        except FileNotFoundError:
            return None
        except OSError as exc:
            self._log.warning("%s could not be read: %s", self._path, exc)
            return self._seen

    # -- the decision --------------------------------------------------------

    def poll(self) -> Reloaded | None:
        """Look once. Returns None when there was nothing to do."""
        raw = self._read()

        if raw is None:
            self._absences += 1
            if self._absences < 2:
                # Probably an editor's write-and-rename, caught in the gap.
                return None
        else:
            self._absences = 0

        if raw == self._seen:
            return None
        self._seen = raw

        config = config_module.load(self._path)
        if not config.parsed:
            return self._reject(config)

        return self._accept(config)

    def _reject(self, config: Config) -> Reloaded:
        """A file that does not parse leaves the daemon exactly as it was."""
        for problem in config.problems:
            self._log.error("%s: %s", self._path, problem)
        if not self._rejected:
            self._rejected = True
            notify.send(
                "MCP server: config not applied",
                f"{self._path} does not parse. The daemon is still running the "
                f"configuration it started with. See journalctl --user -f.",
                urgency="critical",
                log=self._log,
            )
        return Reloaded(config=self._settings.current, rejected=True)

    def _accept(self, config: Config) -> Reloaded:
        for problem in config.problems:
            self._log.warning("%s: %s", self._path, problem)

        previous = self._settings.swap(config)
        tools = self._catalogue.apply(self._mcp, config)
        policy_changed = _policy_of(previous) != _policy_of(config)

        if tools:
            self._log.info(
                "config reloaded: tools +%s -%s (%d offered)",
                list(tools.added),
                list(tools.removed),
                len(self._catalogue.present),
            )
        if policy_changed:
            summary = _describe(previous, config)
            self._log.info("config reloaded: policy %s", summary)
            # Announced in both directions. A widening is the one that matters
            # most, but a narrowing explains a refusal that is about to happen
            # and would otherwise look like a bug.
            notify.send(
                "MCP server: policy changed",
                summary or "The rules an agent runs under have changed.",
                log=self._log,
            )
        if not tools and not policy_changed:
            self._log.info("config reloaded: nothing an agent can tell apart")

        if self._rejected:
            self._rejected = False
            notify.send(
                "MCP server: config applied",
                f"{self._path} parses again and is now in force.",
                log=self._log,
            )

        result = Reloaded(config=config, tools=tools, policy_changed=policy_changed)
        if self._on_change is not None:
            self._on_change(result)
        return result

    # -- the loop ------------------------------------------------------------

    def nudge(self) -> None:
        """Ask the loop to look now rather than at the next tick."""
        if self._wake is not None:
            self._wake.set()

    async def _signals(self) -> None:
        """SIGHUP means look now.

        SIGHUP because uvicorn takes SIGTERM and SIGINT for shutdown and leaves
        this one free.

        A signal handler can only be installed from the main thread, which the
        daemon always is and an in-process test server never is. Losing the
        short-circuit is not worth failing over: the poll is what makes the
        reload happen, and SIGHUP only makes it happen sooner.
        """
        try:
            receiver = anyio.open_signal_receiver(signal.SIGHUP)
        except (RuntimeError, NotImplementedError) as exc:
            self._log.debug("no SIGHUP handler here (%s); polling only", exc)
            return
        try:
            with receiver as sighups:
                async for _ in sighups:
                    self._log.info("SIGHUP: re-reading %s", self._path)
                    self.nudge()
        except (RuntimeError, NotImplementedError) as exc:
            self._log.debug("no SIGHUP handler here (%s); polling only", exc)

    async def run(self) -> None:
        """Poll until cancelled."""
        self._wake = anyio.Event()
        async with anyio.create_task_group() as tg:
            tg.start_soon(self._signals)
            while True:
                with anyio.move_on_after(POLL_S):
                    await self._wake.wait()
                self._wake = anyio.Event()
                try:
                    result = await anyio.to_thread.run_sync(self.poll)
                    if result is not None and result.tools and self._announce is not None:
                        # Only for a tool-set change: `tools/list_changed` is a
                        # claim about the tool list, and a policy edit does not
                        # move it. A client that re-listed on a policy change
                        # would get the same answer and learn nothing.
                        await self._announce()
                except Exception:
                    # A reload must not be able to end the daemon. Whatever went
                    # wrong, the config in force is still a valid one.
                    self._log.exception("reload failed; keeping the running configuration")


def lifespan_for(reloader: Reloader):
    """An MCPServer lifespan that runs `reloader` for as long as the server does.

    The lifespan is the seam that exists for this: it opens before the first
    request and closes after the last, and cancelling the scope on the way out
    is what stops the poll rather than a flag nobody checks.
    """

    @asynccontextmanager
    async def lifespan(_server):
        async with anyio.create_task_group() as tg:
            tg.start_soon(reloader.run)
            try:
                yield {}
            finally:
                tg.cancel_scope.cancel()

    return lifespan
