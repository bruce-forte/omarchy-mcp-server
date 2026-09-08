"""Re-reading the config and the permissions while the daemon is serving.

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
| everything in `permissions.json` / `permissions.local.json` | yes |
| `server.timeout_ms`, `max_output_b` | yes |
| `server.port` | no -- the socket is already bound |
| `log.activity_file`, `activity_max_bytes` | no -- the sink is already open |

The restart-only keys are not refused or reported specially. They are read into
the live config like everything else and simply have no reader left; saying so
in the README is cheaper than machinery for an edit nobody makes twice.

Two files, two failure rules, because both are edited by hand while the daemon
reads them:

- **An unparseable file changes nothing.** `config.load` returns defaults for a
  file it cannot read, which is right at startup and wrong here: a stray
  keystroke would switch every disabled tool back on. The running config
  stands, and the person is told.
- **A defective permissions document changes nothing either, and never ends the
  daemon.** At *startup* any defect refuses to start, because there is no
  known-good document to fall back to and "no rules" is not a safe floor -- a
  hand-written `deny` demotes routes the derivation calls safe. At *reload*
  there is one, and it is the document the user last successfully wrote, so it
  stands. An editor's mid-keystroke autosave must not drop every attached MCP
  session; no debounce can tell that from a finished wrong file.
- **A file that has gone missing waits one poll.** Editors write a temporary
  file and rename it over the target, so "absent" is a normal thing to catch
  mid-save. Absent twice in a row is a deliberate deletion, which resets to
  defaults.

The trigger is a poll rather than a file watch because the process that reacts
should own it: a reload works when the shell is mid-restart, and when the daemon
is run by hand outside the shell entirely. `SIGHUP` short-circuits the wait, so
`omarchy-shell <id> reloadConfig` is immediate for someone typing it.

The shape of `Reloader`, since it is the biggest class here: `run` is an endless
loop that waits up to `POLL_S` and then calls `poll` once. `poll` calls five
small `_poll_*` methods, each of which answers one question -- did the config
change, did the permissions change, did somebody press Acknowledge, Prune or
Remove -- and each returns "nothing happened" cheaply. Everything a *person*
pressed arrives the same way: as a file under the consent directory naming a
token this daemon minted and published only on the frame the shell reads. The
panel writes those files through `bin/omarchy-mcp-consent`; nothing an agent can
reach ever learns a token.
"""

from __future__ import annotations

import signal
from collections.abc import Awaitable, Callable
from contextlib import asynccontextmanager
from dataclasses import dataclass, replace
from pathlib import Path

import anyio
import anyio.to_thread

from . import (
    activity,
    config as config_module,
    delta as delta_module,
    notify,
    permissions as permissions_module,
    registry,
)
from .config import Config
from .paths import CONFIG_FILE, PERMISSIONS_FILES, PERMISSIONS_LOCAL_FILE, REGISTRY_SEEN_FILE
from .permissions import Permissions, PermissionsError
from .prompt import CONSENT_DIR
from .settings import Settings
from .tools.catalogue import Catalogue, Change

#: How long the daemon can be running the previous config after a save. Short
#: enough that saving the file and switching to an agent feels immediate, long
#: enough that this is one `read()` of a small file every two seconds.
POLL_S = 2.0

def _describe(before: Permissions, after: Permissions) -> str:
    """What changed about the rules, in the words of the document.

    Returns "" when nothing an agent could tell apart moved, which is what the
    caller tests to decide whether to say anything at all.
    """
    parts = []
    # Sets of (effect, matcher) pairs, so the two documents can be compared with
    # set arithmetic rather than by walking one list against the other.
    was = {(r.effect.value, r.matcher) for r in before.rules}
    now = {(r.effect.value, r.matcher) for r in after.rules}
    for effect, matcher in sorted(now - was):  # in the new, not in the old
        parts.append(f"+{effect}: {matcher}")
    for effect, matcher in sorted(was - now):  # in the old, not in the new
        parts.append(f"-{effect}: {matcher}")
    if before.guarded_default is not after.guarded_default:
        parts.append(f"guardedDefault: {after.guarded_default.value}")
    if before.ask_timeout_s != after.ask_timeout_s:
        parts.append(f"askTimeoutSeconds: {after.ask_timeout_s}")
    return ", ".join(parts)


@dataclass(frozen=True)
class Reloaded:
    """What one reload did. Falsy when the file was read and meant nothing new."""

    config: Config
    tools: Change = Change()
    policy_changed: bool = False
    rejected: bool = False
    #: The permissions document was re-read and says something new.
    permissions_changed: bool = False
    #: It was re-read and would not load. The previous one still stands.
    permissions_rejected: bool = False
    #: Somebody pressed Acknowledge, so the snapshot advanced and the quarantine
    #: cleared. The one thing that moves `registry-seen.json`.
    acknowledged: bool = False
    #: Somebody pressed Prune, and this many dead rules left the daemon's file.
    pruned: int = 0
    #: Somebody pressed Remove, and this many grants left it.
    revoked: int = 0

    def __bool__(self) -> bool:
        """Whether anything happened that anyone should be told about.

        `rejected` is deliberately absent: a rejection means the daemon is
        running exactly what it was, which is the definition of nothing new.
        """
        return (
            bool(self.tools)
            or self.policy_changed
            or self.permissions_changed
            or self.acknowledged
            or bool(self.pruned)
            or bool(self.revoked)
        )


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
        permission_paths: tuple[Path, ...] = PERMISSIONS_FILES,
        seen_path: Path = REGISTRY_SEEN_FILE,
        consent_dir: Path = CONSENT_DIR,
        local_path: Path = PERMISSIONS_LOCAL_FILE,
        review: delta_module.Review | None = None,
        on_change: Callable[[Reloaded], None] | None = None,
        announce: Callable[[], Awaitable[None]] | None = None,
    ) -> None:
        """Wire up the reloader. Every path is an argument so tests can redirect
        them at a temporary directory; the defaults are the real ones."""
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

        self._permission_paths = tuple(permission_paths)
        self._permissions_seen = self._read_permissions()
        self._permissions_rejected = False

        self._seen_path = seen_path
        self._consent_dir = consent_dir
        self._review = review if review is not None else delta_module.Review()
        self._local_path = local_path
        self._prune_token = ""
        self._revoke_token = ""

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

    def _read_permissions(self) -> tuple[bytes | None, ...]:
        """Both permission files' bytes, in order. Missing reads as None.

        Both together, because they are pooled into one document: a rule added
        to one and removed from the other is a single change, and reading them
        separately would apply half of it.
        """
        contents = []
        for path in self._permission_paths:
            try:
                contents.append(path.read_bytes())
            except FileNotFoundError:
                contents.append(None)
            except OSError as exc:
                self._log.warning("%s could not be read: %s", path, exc)
                contents.append(b"")
        return tuple(contents)

    # -- the decision --------------------------------------------------------

    @property
    def review(self):
        """What is waiting to be reviewed, as of the last poll."""
        return self._review

    def _poll_acknowledgement(self) -> bool:
        """Whether somebody pressed Acknowledge since the last look.

        The token is minted by the daemon and published only on the frame the
        shell reads, so a file naming it is a person at the desk. Spent once, and
        removed whether or not it was current -- a leftover must never
        acknowledge a later review.
        """
        if not self._review or not self._review.token:
            return False
        marker = self._consent_dir / f"ack-{self._review.token}"
        try:
            body = marker.read_text().strip()
        except OSError:
            return False
        try:
            marker.unlink()
        except OSError:
            pass
        if body != self._review.token:
            return False

        try:
            commands = registry.all_commands()
        except registry.RegistryError as exc:
            self._log.warning("cannot acknowledge without a registry: %s", exc)
            return False

        delta_module.save_seen(self._seen_path, commands)
        self._review = delta_module.Review()
        self._settings.swap_unreviewed(frozenset())
        self._log.info("permissions review acknowledged; snapshot advanced")
        return True

    @property
    def prune_token(self) -> str:
        """The token the panel prunes with, or "" if pruning is not on offer."""
        return self._prune_token

    def offer_prune(self, token: str) -> None:
        """Publish a token for the dead rules the panel may tidy away."""
        self._prune_token = token

    def _poll_prune(self) -> int:
        """Whether somebody pressed Prune, and how many rules went.

        Same protection as acknowledging, for a narrower reason. Pruning cannot
        widen anything -- a dead rule grants nothing, because it matches nothing
        -- but it can erase evidence: a `deny` somebody hand-added to this file
        and that has stopped matching is a protection that quietly failed, and
        tidying it away without being seen is the wrong order.
        """
        if not self._prune_token:
            return 0
        marker = self._consent_dir / f"prune-{self._prune_token}"
        try:
            body = marker.read_text().strip()
        except OSError:
            return 0
        try:
            marker.unlink()
        except OSError:
            pass
        if body != self._prune_token:
            return 0

        try:
            commands = registry.all_commands()
        except registry.RegistryError as exc:
            self._log.warning("cannot prune without a registry: %s", exc)
            return 0

        try:
            gone = permissions_module.prune(self._local_path, commands)
        except permissions_module.PermissionsError as exc:
            self._log.error("cannot prune a file that does not load: %s", exc)
            return 0

        self._prune_token = ""
        for rule in gone:
            self._log.info("pruned %r from %s", rule.matcher, self._local_path.name)
            activity.note(
                "permission", verb="prune", route=rule.matcher, via="panel"
            )
        return len(gone)

    @property
    def revoke_token(self) -> str:
        """The token the panel removes grants with, or "" if none is on offer."""
        return self._revoke_token

    def offer_revoke(self, token: str) -> None:
        """Publish the token the panel removes grants with.

        Unlike the others this is not spent per use. Removing an ``allow`` rule
        narrows what an agent may do, so the capability cannot be used to widen
        anything -- the worst it does is disarm whoever holds it. Spending it per
        press would mean three of four presses landing on a dead token inside one
        poll, which is exactly the workflow the button exists for.
        """
        self._revoke_token = token

    def _poll_revoke(self) -> int:
        """Whether somebody pressed Remove, and how many rules went.

        One file per press -- the helper makes each unique with `mktemp` -- so
        two presses inside one poll are two removals rather than one overwriting
        the other. Drained oldest first, because they are a sequence of separate
        decisions and the log should read as one.

        Each file is removed whether or not it was current, and each has to name
        the token as its first word: a file that merely exists is not a press.
        """
        if not self._revoke_token:
            return 0

        # ``glob`` lists files matching a shell-style pattern -- here every
        # press. Sorted by modification time in nanoseconds, with the name as a
        # tie-break so two files written in the same nanosecond still have one
        # definite order.
        markers = sorted(
            self._consent_dir.glob(f"revoke-{self._revoke_token}.*"),
            key=lambda p: (p.stat().st_mtime_ns, p.name),
        )
        gone = 0
        for marker in markers:
            try:
                body = marker.read_text().strip()
            except OSError:
                continue
            finally:
                # ``finally`` on a loop body runs before ``continue`` as well as
                # on the way out, so the file is removed on every path -- read,
                # unreadable, or rejected below.
                try:
                    marker.unlink()
                except OSError:
                    pass

            # "<token> revoke <effect> <matcher>", taken apart one word at a
            # time so that a matcher containing spaces stays intact.
            token, _, rest = body.partition(" ")
            verb, _, subject = rest.partition(" ")
            effect, _, matcher = subject.partition(" ")
            if token != self._revoke_token or verb != "revoke" or not matcher:
                self._log.warning("a consent file did not name this daemon's token")
                continue

            try:
                removed = permissions_module.revoke(
                    self._local_path, permissions_module.Effect(effect), matcher
                )
            except PermissionsError as exc:
                self._log.error("cannot edit a file that does not load: %s", exc)
                continue
            except (ValueError, permissions_module.RevokeRefused) as exc:
                # `Effect(effect)` raises on a word this daemon has not heard of;
                # `RevokeRefused` on one it will not act on. Both read as no
                # answer rather than as a yes, which is what an older daemon
                # meeting a newer vocabulary has to do.
                self._log.error("revoke refused: %s", exc)
                continue

            for rule in removed:
                gone += 1
                self._log.info("revoked %r from %s", rule.matcher, self._local_path.name)
                activity.note(
                    "permission", verb="revoke", route=rule.matcher, via="panel"
                )
        return gone

    def recompute_review(self) -> None:
        """Read the delta again, because the rules or the registry moved.

        Deliberately does **not** advance the snapshot. Only acknowledgement
        does that; recomputing on a reload would let an edit quietly consume a
        warning nobody saw.
        """
        try:
            commands = registry.all_commands()
        except registry.RegistryError:
            return
        seen = delta_module.load_seen(self._seen_path)
        if seen is None:
            # Baselined at startup. A snapshot that vanished mid-run is not an
            # invitation to re-review everything.
            return
        self._review = delta_module.compute(seen, commands, self._settings.permissions)
        self._settings.swap_unreviewed(self._review.quarantined)

    def poll(self) -> Reloaded | None:
        """Look once. Returns None when there was nothing to do.

        Blocking -- it reads files -- so `run` calls it on a worker thread.
        """
        result = self._poll_config()
        permissions = self._poll_permissions()
        acknowledged = self._poll_acknowledgement()
        pruned = self._poll_prune()
        revoked = self._poll_revoke()

        if permissions is not None and permissions[0]:
            # A new rule can quarantine or release routes, so the delta is read
            # through whichever document is in force now.
            self.recompute_review()

        if permissions is not None:
            changed, rejected = permissions
            base = result if result is not None else Reloaded(config=self._settings.current)
            # `Reloaded` is frozen, so fields are added by building a copy;
            # ``replace`` is the dataclass helper for that.
            result = replace(
                base, permissions_changed=changed, permissions_rejected=rejected
            )

        if acknowledged or pruned or revoked:
            base = result if result is not None else Reloaded(config=self._settings.current)
            result = replace(
                base, acknowledged=acknowledged, pruned=pruned, revoked=revoked
            )

        if result is not None and self._on_change is not None:
            self._on_change(result)
        return result

    def _poll_permissions(self) -> tuple[bool, bool] | None:
        """Re-read the permissions. Returns (changed, rejected), or None.

        Unlike the config file there is no absence dance: a missing permissions
        file is a valid document -- it says "no rules" -- so a rename caught
        mid-save reads as an empty document for one poll and corrects itself on
        the next. The cost of that is bounded by `POLL_S`; the cost of guessing
        is a window where the user's rules are not the ones in force.
        """
        raw = self._read_permissions()
        if raw == self._permissions_seen:
            return None
        previous = self._permissions_seen
        self._permissions_seen = raw
        ours = self._is_our_own_write(previous, raw)

        try:
            loaded = permissions_module.load(self._permission_paths)
        except PermissionsError as exc:
            # Never fatal here. At startup this refuses to start, because there
            # is nothing known-good to keep; by now there is, and it is what the
            # user last successfully wrote.
            self._log.error("permissions not applied: %s", exc)
            if not self._permissions_rejected:
                self._permissions_rejected = True
                notify.send(
                    "MCP server: permissions not applied",
                    f"{exc}\n\nThe daemon is still running the permissions it had.",
                    urgency="critical",
                    log=self._log,
                )
            return (False, True)

        before = self._settings.swap_permissions(loaded)
        changed = _describe(before, loaded)
        if changed:
            self._log.info("permissions reloaded: %s", changed)
            # Announced in both directions. A widening is the one that matters
            # most, but a narrowing explains a refusal that is about to happen
            # and would otherwise look like a bug.
            #
            # Except when this daemon wrote it. The rule is that the daemon
            # announces changes the person did not make: they pressed Always, or
            # Remove, or Prune, and a toast a second later is their own press
            # read back to them.
            if not ours:
                notify.send("MCP server: permissions changed", changed, log=self._log)
        else:
            self._log.info("permissions reloaded: nothing an agent can tell apart")

        if self._permissions_rejected:
            self._permissions_rejected = False
            notify.send(
                "MCP server: permissions applied",
                "The permissions document loads again and is now in force.",
                log=self._log,
            )
        return (bool(changed), False)

    def _is_our_own_write(
        self, previous: tuple[bytes | None, ...], raw: tuple[bytes | None, ...]
    ) -> bool:
        """Whether the only thing that moved is a file this daemon just wrote.

        Deliberately narrow. A hand edit landing in the same two-second window as
        a grant is still news, so the user's own file having moved at all is
        enough to announce -- and so is a local file whose contents are not the
        ones written here.
        """
        try:
            index = self._permission_paths.index(self._local_path)
        except ValueError:
            return False
        if len(previous) != len(raw):
            return False
        # ``zip`` walks the two tuples in step, pairing them up; ``enumerate``
        # adds the position, which is what lets the daemon's own file be skipped.
        # So this asks: did anything *other* than our file move?
        if any(a != b for position, (a, b) in enumerate(zip(previous, raw)) if position != index):
            return False
        return permissions_module.wrote_ourselves(raw[index])

    def _poll_config(self) -> Reloaded | None:
        """Re-read `config.toml` if its bytes moved. None means nothing to do."""
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
        # A rejection is reported too: "your file was not applied" is the thing
        # the person most needs the bar to say.
        return self._reject(config) if not config.parsed else self._accept(config)

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
        """Put a config that parsed into force, and register the tools it asks for."""
        for problem in config.problems:
            self._log.warning("%s: %s", self._path, problem)

        self._settings.swap(config)
        tools = self._catalogue.apply(self._mcp, config)
        if tools:
            self._log.info(
                "config reloaded: tools +%s -%s (%d offered)",
                list(tools.added),
                list(tools.removed),
                len(self._catalogue.present),
            )
        if not tools:
            self._log.info("config reloaded: nothing an agent can tell apart")

        if self._rejected:
            self._rejected = False
            notify.send(
                "MCP server: config applied",
                f"{self._path} parses again and is now in force.",
                log=self._log,
            )

        return Reloaded(config=config, tools=tools)

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
        # An ``Event`` is a flag one task waits on and another sets. `nudge`
        # sets it, which is how SIGHUP cuts the wait short.
        self._wake = anyio.Event()
        # A task group runs tasks concurrently and does not leave the ``async
        # with`` until all of them are done -- so the signal watcher cannot
        # outlive this loop.
        async with anyio.create_task_group() as tg:
            tg.start_soon(self._signals)
            while True:
                # Wait for a nudge, but no longer than POLL_S: the timeout is
                # the ordinary path and the event is the shortcut.
                with anyio.move_on_after(POLL_S):
                    await self._wake.wait()
                # An event cannot be un-set, so each round gets a fresh one.
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
                    # ``log.exception`` logs the message *and* the traceback,
                    # which is what makes this survivable rather than silent.
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
                # Everything before the yield is startup, everything after is
                # shutdown; the server runs for the length of the yield.
                yield {}
            finally:
                # `run` loops forever, so the only way out is to cancel it.
                # Without this the task group would wait here for good.
                tg.cancel_scope.cancel()

    return lifespan
