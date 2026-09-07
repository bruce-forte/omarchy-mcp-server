"""What the agent actually did, on disk, after the daemon is gone.

`stats.py` counts calls in memory and `/health` publishes the counters. That
answers *is it serving* and nothing else: it cannot say what an agent did to
this desktop ten minutes ago, and it dies with the process. This module appends
one JSON object per tool call to a file, so there is an audit trail something
other than `journalctl` can read.

Three properties are deliberate:

*A tool call never waits for the disk.* `record()` puts the event on a bounded
queue and returns; one writer thread drains it. A stalled disk slows nothing
down, and because that thread is the file's only writer there is no lock on the
file at all.

*Loss is bounded and never silent.* A full queue drops the event, counts it, and
the drop is written into the file itself as soon as the writer catches up. A gap
in an audit trail that nothing accounts for is worse than no audit trail.

*Command output is never written here.* Arguments are, truncated -- they are
what the agent asked for, and they are the point. Output is not: OCR text and
clipboard reads are the screen's contents, and a log of those is a different and
much worse thing than a log of actions. The file is still `0600` in a `0700`
directory, because an argument can be text the user copied.
"""

from __future__ import annotations

import json
import os
import queue
import threading
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

from . import __version__, execute
from .paths import ACTIVITY_FILE, STATE_DIR

#: Per argument, before it is elided. Long enough for a theme name, a path or a
#: URL; short enough that one pasted document cannot fill the file.
MAX_ARG_CHARS = 120

#: A tool takes a handful of arguments. Anything past this is a payload.
MAX_ARGS = 16

#: What an argument is cut to when the whole line is still too long.
HARD_ARG_CHARS = 40

#: A whole line. Beyond it the arguments are elided harder, then dropped, so a
#: single call can never displace the history around it.
MAX_LINE_B = 2048

#: Deep enough that a burst of parallel tool calls never touches it, shallow
#: enough that a wedged disk costs about a megabyte rather than the session.
QUEUE_DEPTH = 4096

#: How long the sentinel gets. `Service.qml` SIGKILLs 5s after SIGTERM and
#: uvicorn's own grace is 3s, so this has to fit in what is left.
JOIN_TIMEOUT_S = 1.0

_STOP = object()


def _now() -> str:
    """Local time with an offset. A person reads this file; local is what they mean."""
    return datetime.now().astimezone().isoformat(timespec="seconds")


def _elide(text: str, limit: int) -> str:
    if len(text) <= limit:
        return text
    return f"{text[:limit]}…(+{len(text) - limit})"


@dataclass
class Record:
    """One tool call, filled in as the call learns things about itself.

    ``ts`` is stamped when the call starts rather than when it finishes, so the
    time in the file is the time the agent asked. Lines are still written in
    completion order, which for concurrent calls is not the same order.
    """

    tool: str
    route: str = ""
    args: tuple[str, ...] = ()
    target: str | None = None
    tier: str | None = None
    consent: str | None = None
    exit: int | None = None
    detached: bool = False
    timed_out: bool = False
    #: Set explicitly only for what cannot be derived from the fields above:
    #: `refused`, `not_installed`, `error`.
    outcome: str | None = None
    ms: int = 0
    ts: str = field(default_factory=_now)

    @property
    def result(self) -> str:
        """One of ok, failed, timed_out, refused, not_installed, error."""
        if self.outcome:
            return self.outcome
        if self.timed_out:
            return "timed_out"
        if self.exit in (0, None):
            return "ok"
        return "failed"

    @property
    def ok(self) -> bool:
        return self.result == "ok"

    def as_dict(self) -> dict[str, object]:
        """The line. Absent fields are omitted rather than null."""
        body: dict[str, object] = {"ts": self.ts, "tool": self.tool}
        if self.route:
            body["route"] = self.route
        if self.args:
            body["args"] = [_elide(a, MAX_ARG_CHARS) for a in self.args[:MAX_ARGS]]
        for name in ("target", "tier", "consent"):
            value = getattr(self, name)
            if value:
                body[name] = value
        body["outcome"] = self.result
        if self.exit is not None:
            body["exit"] = self.exit
        for name in ("detached", "timed_out"):
            if getattr(self, name):
                body[name] = True
        body["ms"] = self.ms
        return body


def line_of(body: dict[str, object]) -> str:
    """Serialise one record, eliding harder if it does not fit.

    Only the arguments are cut, because everything else in a line is bounded by
    construction -- a tool name, a route, a resolved label, an exit code. An
    argument is the one field a model supplies, and it may be a document
    somebody copied.
    """
    text = json.dumps(body, ensure_ascii=False)
    if len(text.encode()) <= MAX_LINE_B or "args" not in body:
        return text
    body = dict(body, args=[_elide(str(a), HARD_ARG_CHARS) for a in body["args"]])
    return json.dumps(body, ensure_ascii=False)


def _notify(headline: str, body: str) -> None:
    """A daemon-level fault earns a toast; a tool-call failure never does.

    An audit trail that stopped working is the first kind. It is reported once,
    because the same broken disk will produce one of these per tool call.
    """
    try:
        execute.run(
            ["omarchy", "notification", "send", "-u", "critical", headline, body],
            timeout_ms=5_000,
            max_output_b=4096,
        )
    except Exception:  # noqa: BLE001 -- a failed toast must not take the writer with it
        pass


class Sink:
    """The queue, the writer thread, and the file it appends to."""

    def __init__(self, path: Path, max_bytes: int, log) -> None:
        self.path = path
        self.max_bytes = max_bytes
        self._log = log
        self._q: queue.Queue = queue.Queue(maxsize=QUEUE_DEPTH)
        self._thread: threading.Thread | None = None
        self._drop_lock = threading.Lock()
        self._dropped = 0
        self._warned = False
        self._told_user = False
        self._closed = False
        self._fh = None
        self._size = 0

    # -- producer side: called from the event loop and from worker threads ----

    def append(self, body: dict[str, object]) -> None:
        """Never blocks, never raises. A tool call must not wait for a log."""
        try:
            self._q.put_nowait(body)
        except queue.Full:
            with self._drop_lock:
                self._dropped += 1
                warn = not self._warned
                self._warned = True
            if warn:
                self._log.warning(
                    "activity log is behind; events are being dropped (%s)", self.path
                )

    def event(self, name: str, **fields: object) -> None:
        self.append({"ts": _now(), "event": name, **fields})

    # -- lifecycle ------------------------------------------------------------

    def start(self) -> None:
        self._thread = threading.Thread(target=self._drain, name="activity", daemon=True)
        self._thread.start()

    def close(self) -> None:
        """Write the closing marker and shut the writer down. Idempotent.

        Called from the ASGI lifespan shutdown rather than from the end of
        `main`, because nothing at the end of `main` runs: uvicorn restores the
        default signal handler and re-raises the signal that stopped it, so the
        process dies *by signal*. A `finally`, an `atexit`, a non-daemon thread
        -- none of them get a turn (F28). The lifespan shutdown does, and it
        completes before the re-raise.
        """
        if self._closed:
            return
        self._closed = True
        self.event("stopped")
        self.stop()

    def stop(self) -> None:
        """Sentinel, then join, both bounded.

        The sentinel goes on the queue *behind* whatever is already there, so a
        queued event is still written. It is the one `put` in this module that
        may block: dropping the sentinel would leave the writer parked on `get`
        with nothing coming, and everything queued behind it unwritten.
        """
        if self._thread is None:
            return
        try:
            self._q.put(_STOP, timeout=JOIN_TIMEOUT_S)
        except queue.Full:
            self._log.warning(
                "activity writer is still behind; the last records may be missing from %s",
                self.path,
            )
            self._thread = None
            return
        self._thread.join(JOIN_TIMEOUT_S)
        if self._thread.is_alive():
            self._log.warning("activity writer did not finish within %ss", JOIN_TIMEOUT_S)
        self._thread = None

    # -- consumer side: the writer thread, and the only thing touching the file

    def _drain(self) -> None:
        while True:
            body = self._q.get()
            if body is _STOP:
                self._close()
                return
            self._flush_drops()
            self._write(body)

    def _flush_drops(self) -> None:
        with self._drop_lock:
            dropped, self._dropped = self._dropped, 0
            if dropped:
                self._warned = False
        if dropped:
            self._write({"ts": _now(), "event": "dropped", "n": dropped})

    def _write(self, body: dict[str, object]) -> None:
        try:
            if self._fh is None:
                self._open()
            line = (line_of(body) + "\n").encode()
            # Rotate *before* the write that would cross the cap, so the file
            # is never over it and a record is never split across generations.
            if self._size and self._size + len(line) > self.max_bytes:
                self._rotate()
            self._fh.write(line)
            self._fh.flush()
            self._size += len(line)
        except OSError as exc:
            self._failed(exc)

    def _open(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        os.chmod(self.path.parent, 0o700)
        fd = os.open(self.path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
        # Binary, so one line is one write of a known length.
        self._fh = os.fdopen(fd, "ab")
        self._size = self.path.stat().st_size

    def _rotate(self) -> None:
        """One generation kept. A tailer re-opens on the rename; see N6."""
        self._fh.close()
        self._fh = None
        os.replace(self.path, rotated(self.path))
        self._open()

    def _close(self) -> None:
        if self._fh is not None:
            try:
                self._fh.close()
            except OSError:
                pass
            self._fh = None

    def _failed(self, exc: OSError) -> None:
        """Say so every time on stderr, toast once, and keep serving.

        The daemon's job is not the log. Draining continues so that a wedged
        disk cannot turn into an unbounded queue.
        """
        self._log.error("activity log write failed (%s): %s", self.path, exc)
        self._close()
        if not self._told_user:
            self._told_user = True
            _notify(
                "Omarchy MCP: activity log stopped",
                f"Could not write {self.path}: {exc}. The server is still running; "
                f"tool calls are unaffected.",
            )


def rotated(path: Path) -> Path:
    return path.with_name(path.name + ".1")


#: The sink the daemon is currently writing through, or ``None``.
#:
#: A module global for the same reason `frames.emit` is one: the events that
#: want it are not tool calls and have no `Stats` to hang off. `stats.call(...)`
#: stays the only way a *call* is recorded -- one record per call, on every exit
#: path -- and this is for the handful of things that are not calls.
_current: object | None = None


def note(name: str, **fields: object) -> None:
    """Record something that is not a tool call. Never raises.

    A grant is the case this exists for. Somebody answered *always* at the desk
    and a rule was written to a file; that is the most consequential thing a
    person does in this UI, it outlives the daemon, and the file it lands in says
    *that* it was granted but not when, from which surface, or in answer to what.

    Off with `log.activity`, like everything else here. Slightly uncomfortable --
    the audit of grants disappears with the audit of calls -- but a second switch
    for one event kind is worse, and `permissions.local.json` still holds the
    state itself.
    """
    sink = _current
    if sink is None:
        return
    try:
        sink.event(name, **fields)
    except Exception:  # noqa: BLE001 -- an audit line must not fail a tool call
        pass


@contextmanager
def writer(config, log):
    """Run the writer for as long as the daemon serves.

    Yields ``None`` when logging is switched off, which is what `Stats` takes
    for "counters only" -- so the off switch costs one branch here and none
    anywhere else.
    """
    if not getattr(config, "activity", True):
        yield None
        return

    global _current

    sink = Sink(path_for(config), config.activity_max_bytes, log)
    sink.start()
    sink.event("started", version=__version__, port=config.port)
    _current = sink
    try:
        yield sink
    finally:
        _current = None
        # Normally already closed from the lifespan shutdown; this is the path
        # for anything that drives the writer without an ASGI server.
        sink.close()


class Closing:
    """Closes the log when the application shuts down.

    A pure-ASGI wrapper for the same reason `auth.py` is one, and because the
    only shutdown hook this process reliably gets is the lifespan's: see
    `Sink.close`. Non-lifespan traffic passes straight through untouched.
    """

    def __init__(self, app, sink: Sink) -> None:
        self.app = app
        self._sink = sink

    async def __call__(self, scope, receive, send):
        if scope["type"] != "lifespan":
            await self.app(scope, receive, send)
            return

        async def watched(message):
            # Before the completion is reported, not after: once uvicorn has
            # its answer it is free to re-raise and end the process.
            if message["type"] == "lifespan.shutdown.complete":
                self._sink.close()
            await send(message)

        await self.app(scope, receive, watched)


def path_for(config) -> Path:
    """Always under the state directory.

    `config.activity_file` is a filename, not a path, and `config.py` refuses
    anything with a separator in it. A log that could be pointed at the plugin
    directory would make Omarchy reload the shell on every tool call.
    """
    return STATE_DIR / config.activity_file


# -- reading it back ---------------------------------------------------------


def tail(n: int = 20, path: Path | None = None) -> list[dict]:
    """The last ``n`` records, oldest first, reading across a rotation."""
    path = STATE_DIR / ACTIVITY_FILE if path is None else path
    lines: list[str] = []
    for candidate in (rotated(path), path):
        try:
            lines += candidate.read_text(encoding="utf-8", errors="replace").splitlines()
        except OSError:
            continue

    out: list[dict] = []
    for raw in lines[-n:] if n > 0 else lines:
        try:
            body = json.loads(raw)
        except ValueError:
            continue
        if isinstance(body, dict):
            out.append(body)
    return out


def render(body: dict) -> str:
    """One record as a line a person reads, for ``omarchy-mcpd --tail``."""
    when = str(body.get("ts", ""))[11:19]
    if "event" in body:
        extra = " ".join(f"{k}={v}" for k, v in body.items() if k not in ("ts", "event"))
        return f"{when}  -- {body['event']} {extra}".rstrip()

    parts = [when, str(body.get("tool", "")), str(body.get("route", ""))]
    if body.get("args"):
        parts.append(" ".join(repr(a) for a in body["args"]))
    if body.get("target"):
        parts.append(f"→ {body['target']}")
    parts.append(str(body.get("outcome", "")))
    if body.get("consent"):
        parts.append(f"consent={body['consent']}")
    if body.get("exit") is not None:
        parts.append(f"exit={body['exit']}")
    parts.append(f"{body.get('ms', 0)}ms")
    return "  ".join(p for p in parts if p)
