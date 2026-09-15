"""Running commands.

Three properties matter here and each is deliberate:

*No shell, ever.* Arguments arrive from a language model. ``argv`` goes to
``execve`` as a list, so ``"; rm -rf ~"`` is an argument, not a command.

    This is the single most important line in the file. ``subprocess`` can be
    asked to hand a string to ``/bin/sh``, which then re-reads it looking for
    ``;``, ``|``, ``$(...)`` and the rest. Passing a *list* instead means the
    kernel receives the program and its arguments already separated, and no
    character in any argument has any special meaning to anything.
    ``tests/test_execute.py`` writes a canary file and asserts an injection
    attempt leaves it alone.

*Nothing waits forever.* Many Omarchy commands block on the user by design --
``theme switcher``, ``menu select``, ``capture region``. A blocked call means a
client timeout and a leaked child, so anything interactive is detached instead
and everything else is bounded.

*Output is bounded, not trimmed afterwards.* One command's output should not be
able to bury the caller's context -- or the daemon.

    This used to say "capped", and it read both pipes with ``communicate()``
    before capping. ``communicate()`` returns when the child is done, so the
    whole of its output was in memory *before* ``max_output_b`` was consulted:
    the cap trimmed what an agent saw and never bounded what this process held.
    A single allowed command emitting enough on stdout could exhaust a daemon
    that is meant to run for weeks. Found by the Omarchy marketplace security
    review; see ROADMAP N22. Both pipes are now drained concurrently into
    bounded sinks, and a stream that passes its ceiling has the whole process
    group terminated rather than being read to the end.

*The binary is the one we meant.* ``argv[0]`` is resolved by `trust.py` against a
fixed allowlist of directories, and the file it lands on must be root-owned and
writable by nobody else, as must every directory above it.

    This used to say *robustness, not a security control*, on the reasoning that
    no MCP client can influence this daemon's environment. That reasoning missed
    the session ``PATH``: on the machine this was written on, ``curl`` resolved
    to a binary in a directory the user could write. See `trust.py`, which is
    where the rule now lives, and ROADMAP N20 for what the review found.

*Children get the trusted ``PATH``, not ours.* Choosing the right file settles
nothing if the command then looks *its* helpers up on a list nobody checked.
"""

from __future__ import annotations

import os
import selectors
import shlex
import signal
import subprocess
import time
from collections.abc import Awaitable, Callable, Iterable
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from . import trust

#: How a blocking call in this module reaches a worker thread. The function
#: itself is `tools/_shared.offload`, but `gate.py` and `prompt.py` take it as
#: an argument rather than importing it -- `tools/` depends on both of them --
#: so the name they annotate that argument with lives here, at the point all
#: three already meet.
Offload = Callable[..., Awaitable[Any]]

#: Commands in these groups open a window or wait for the user. Waiting on them
#: is always wrong: they finish when a human is done, not when work is done.
DETACH_GROUPS = frozenset({"launch", "menu", "tui"})

#: Same, for routes whose group is otherwise unremarkable.
DETACH_MARKERS = ("switcher", "selector", "select", "region", "screenrecording", "screensaver")

#: Where a bare ``argv[0]`` is looked for, in order.
#:
#: An alias for `trust.TRUSTED_DIRS`, so the list and the rule that guards it
#: cannot drift apart. The name stays because the test suite redirects this
#: binding to point the resolver at fixture directories.
SEARCH = trust.TRUSTED_DIRS

#: How long a terminated child gets to exit before it is killed.
GRACE_S = 2.0

#: How much is read from a pipe at a time.
CHUNK_B = 64 * 1024

#: The hard ceiling on bytes *read* from one stream, as a multiple of the cap an
#: agent sees, with a floor for very small caps.
#:
#: Two different limits, deliberately. ``max_output_b`` is presentational: how
#: much of the output the agent is shown. This is structural: how much this
#: process will read before concluding the command is a runaway and killing it.
#: The ceiling has to be the larger of the two -- a command that legitimately
#: prints a few megabytes should still finish and be trimmed -- while staying
#: small enough that a process producing without end is stopped in seconds
#: rather than at the timeout.
#:
#: At the default 256 KiB cap this is 8 MiB per stream.
OUTPUT_CEILING_FACTOR = 32
MIN_OUTPUT_CEILING_B = 1024 * 1024


def ceiling_for(limit: int) -> int:
    """The hard byte ceiling for a stream whose presented cap is ``limit``."""
    return max(limit * OUTPUT_CEILING_FACTOR, MIN_OUTPUT_CEILING_B)


def _keep_for(limit: int) -> int:
    """How many bytes of each end survive truncation.

    One definition, used by both `cap` and `_Sink`, so the streaming path and
    the in-memory path cannot drift into shaping output differently. The 64
    leaves room for the note itself; the floor of 256 keeps a very small limit
    from yielding nothing at all.
    """
    return max(limit // 2 - 64, 256)


class _Sink:
    """A bounded accumulator for one pipe.

    Holds at most ``limit + 1`` bytes of head and ``keep`` bytes of tail, so
    memory is bounded by the cap however much the child produces. ``total``
    keeps counting regardless, because the ceiling and the "how much was
    dropped" note are both about what arrived, not about what was retained.

    The head is sized ``limit + 1`` rather than ``keep`` for a reason worth
    stating: ``keep`` is ``limit // 2 - 64``, so two of them come to
    ``limit - 128``. Collapsing to head-and-tail immediately would silently lose
    up to 128 bytes out of the middle of output that was never over the limit at
    all. So everything is kept until the limit is genuinely passed.
    """

    def __init__(self, limit: int) -> None:
        self.limit = limit
        self.keep = _keep_for(limit)
        self.head = bytearray()
        self.tail = bytearray()
        self.total = 0

    def feed(self, chunk: bytes) -> None:
        self.total += len(chunk)
        room = (self.limit + 1) - len(self.head)
        if room > 0:
            self.head += chunk[:room]
            chunk = chunk[room:]
            if not chunk:
                return
        self.tail += chunk
        if len(self.tail) > self.keep:
            # ``del`` on a slice rather than rebinding: this keeps one buffer
            # rather than allocating a fresh one per chunk.
            del self.tail[: len(self.tail) - self.keep]

    def text(self) -> tuple[str, bool]:
        """The output as a string, and whether anything was dropped."""
        if self.total <= self.limit:
            # It all fits, and it is all still in ``head``.
            return bytes(self.head).decode("utf-8", "replace"), False
        head = bytes(self.head[: self.keep])
        tail = bytes(self.tail[-self.keep :])
        note = _TRUNCATION_NOTE.format(dropped=self.total - len(head) - len(tail))
        # Cutting at an arbitrary byte can land mid-character, which is why the
        # decode replaces rather than raises.
        return head.decode("utf-8", "replace") + note + tail.decode("utf-8", "replace"), True

#: ``{dropped}`` is filled in by ``.format`` in `cap`, not an f-string: the
#: value is not known here, only the shape of the sentence.
_TRUNCATION_NOTE = "\n\n... [{dropped} bytes dropped] ...\n\n"


class OutputTooLarge(Exception):
    """A stream passed its ceiling before the command finished.

    Only the internal callers see this. For them the cap *is* the ceiling: they
    parse what comes back, and handing them a truncated document would turn a
    bound into a JSON parse error three frames away. `run` does not raise it,
    because there the cap is presentational and truncation is the answer.
    """

    def __init__(self, what: str, limit: int) -> None:
        self.limit = limit
        super().__init__(f"{what} produced more than {limit} bytes; refusing to buffer it")


class NotInstalled(Exception):
    """``argv[0]`` names nothing runnable in any searched directory.

    A project-specific exception type, so a caller can catch exactly this and
    turn it into a clear message. Subclassing ``Exception`` is all it takes.
    """

    def __init__(
        self,
        name: str,
        searched: Iterable[Path] | None = None,
        refused: Iterable[str] | None = None,
    ) -> None:
        """Record what was looked for and where, and build the message."""
        searched = SEARCH if searched is None else searched
        self.name = name
        # A generator expression inside ``tuple(...)``: each Path is turned into
        # a string, so the record survives being serialised to JSON later.
        self.searched = tuple(str(d) for d in searched)
        #: Files that were found and refused, with the reason for each. Empty
        #: for an ordinary missing dependency. Carried separately because the
        #: two send a reader to completely different places: a package manager,
        #: or the owner of a file.
        self.refused = tuple(refused or ())
        sentence = f"`{name}` is not installed. Looked in " + ", ".join(self.searched) + "."
        if self.refused:
            sentence += " Refused: " + "; ".join(self.refused) + "."
        # ``super().__init__`` runs the base ``Exception``'s constructor, which
        # is what makes ``str(exc)`` return this sentence.
        super().__init__(sentence)

    def as_dict(self) -> dict[str, object]:
        """The JSON shape a tool returns when the dependency is missing."""
        return {"error": str(self), "missing": self.name}


def resolve_binary(name: str, searched: Iterable[Path] | None = None) -> str:
    """The file a bare command name should run, or raise.

    A name containing a separator is a path already and is used as given, which
    is what every other PATH lookup does and what keeps an explicitly-chosen
    binary explicit.

    ``searched`` defaults to `SEARCH` at call time rather than as a default
    argument: a default is bound once at import, which would make the module
    global a decoy that could be reassigned with no effect.

    Verification is applied when the directories are the real allowlist, and not
    when a caller has deliberately supplied its own. Only the test suite does
    the latter, and it is building fake binaries in a directory it owns -- the
    same distinction `conftest.py` already draws with ``REAL_SEARCH``. Nothing
    in the daemon passes ``searched``.

    Not cached. The `os.stat` calls cost nothing beside the fork that follows,
    there is no invalidation question to answer forever, and a dependency
    installed while the daemon is running works without a restart.
    """
    searched = SEARCH if searched is None else searched
    # ``os.sep`` is "/" here. A name containing one is already a path, and is
    # verified rather than taken on faith -- an absolute path from a caller is
    # still a file somebody could have replaced.
    if os.sep in name:
        return trust.verify(name)
    directories = tuple(searched)
    verified = directories == tuple(trust.TRUSTED_DIRS)
    found = trust.resolve(name, directories, verified=verified)
    if found is not None:
        return found
    refused = trust.describe(name, directories) if verified else []
    raise NotInstalled(name, directories, refused)


@dataclass(frozen=True)
class Result:
    """What one finished (or detached, or timed-out) command left behind."""

    #: ``None`` when there is no exit code to have: a detached child is still
    #: running when this is built. ``int | None`` is how a hint says "either".
    exit_code: int | None
    stdout: str
    stderr: str
    timed_out: bool
    detached: bool
    pid: int | None = None
    truncated: bool = False
    #: Which file actually ran. For the log, not for the agent: it is the same
    #: answer on every call and would cost a line of context each time.
    executable: str = field(default="", compare=False)

    def as_dict(self) -> dict[str, object]:
        """The agent's view of the result: every field except ``executable``."""
        # ``asdict`` is the dataclass helper that turns the instance into a plain
        # dict, ready for ``json.dumps``.
        body = asdict(self)
        body.pop("executable")
        return body


def should_detach(group: str, route: str) -> bool:
    """Whether this command finishes on a human's schedule rather than ours."""
    if group in DETACH_GROUPS:
        return True
    # ``any(...)`` is True as soon as one marker appears anywhere in the route,
    # and stops looking at that point.
    return any(marker in route for marker in DETACH_MARKERS)


def cap(text: str, limit: int) -> tuple[str, bool]:
    """Keep the head and tail of over-long output; the middle is the filler.

    Returns the text and whether anything was dropped. ``return a, b`` builds a
    tuple, and a caller unpacks it with ``text, cut = cap(...)``.
    """
    # The limit is in bytes, so the measuring is done on bytes. A string's length
    # is in characters, and one character can be four bytes of UTF-8.
    # ``"replace"`` substitutes a placeholder for anything undecodable rather
    # than raising -- command output is not guaranteed to be valid UTF-8.
    raw = text.encode("utf-8", "replace")
    if len(raw) <= limit:
        return text, False
    # ``//`` is integer division. The 64 leaves room for the note itself, and
    # the floor of 256 keeps a very small limit from yielding nothing at all.
    keep = _keep_for(limit)
    # Slices: the first ``keep`` bytes, and the last ``keep`` bytes. Cutting at
    # an arbitrary byte can land mid-character, which is the other reason for
    # ``"replace"`` on the way back.
    head = raw[:keep].decode("utf-8", "replace")
    tail = raw[-keep:].decode("utf-8", "replace")
    note = _TRUNCATION_NOTE.format(dropped=len(raw) - 2 * keep)
    return head + note + tail, True


def _drain(
    proc: subprocess.Popen[bytes],
    limit: int,
    ceiling: int,
    timeout_s: float,
) -> tuple[_Sink, _Sink, bool, bool]:
    """Read both pipes concurrently into bounded sinks.

    Returns the two sinks, whether the deadline passed, and whether either
    stream went over ``ceiling``.

    Draining both at once is not a flourish. Reading one to the end before
    starting the other deadlocks the moment the child fills the pipe nobody is
    reading: it blocks on write, so it never finishes, so the read never ends.
    That deadlock is exactly what ``communicate()`` existed to avoid, and this
    has to solve it too -- with a selector rather than a thread each, because
    one loop against one deadline is the thing that can also stop early.
    """
    selector = selectors.DefaultSelector()
    sinks = {"stdout": _Sink(limit), "stderr": _Sink(limit)}
    for name in ("stdout", "stderr"):
        stream = getattr(proc, name)
        if stream is not None:
            selector.register(stream, selectors.EVENT_READ, name)

    deadline = time.monotonic() + timeout_s
    timed_out = False
    overflowed = False
    try:
        while selector.get_map() and not overflowed:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                timed_out = True
                break
            # The 0.5 ceiling on the wait keeps the deadline honest even when
            # the child says nothing at all.
            for key, _ in selector.select(timeout=min(remaining, 0.5)):
                name = str(key.data)
                chunk = os.read(key.fd, CHUNK_B)
                if not chunk:
                    # EOF on this pipe. The child may still be running; the
                    # other stream decides when the loop ends.
                    selector.unregister(key.fileobj)
                    continue
                sink = sinks[name]
                sink.feed(chunk)
                if sink.total > ceiling:
                    overflowed = True
                    break
    finally:
        selector.close()
    return sinks["stdout"], sinks["stderr"], timed_out, overflowed


def _finish(proc: subprocess.Popen[bytes], *, stop: bool) -> None:
    """Close the pipes and reap the child, terminating the group if asked.

    Always reaps. A child left unwaited is a zombie for the life of a daemon
    that is meant to run for weeks, and this is the one function every exit
    path from a bounded read goes through.
    """
    for stream in (proc.stdout, proc.stderr):
        if stream is not None:
            stream.close()
    if stop:
        # The whole group: an Omarchy command is usually a shell script with
        # children, and signalling only the parent leaves them writing.
        _terminate_group(proc)
    try:
        proc.wait(timeout=GRACE_S)
    except subprocess.TimeoutExpired:
        # SIGKILL cannot be ignored, and the wait after it is what reaps.
        proc.kill()
        proc.wait()


def _spawn(
    argv: list[str],
    exe: str,
    env: dict[str, str],
) -> subprocess.Popen[bytes]:
    """A child with both pipes open, in its own session, in binary mode.

    Binary rather than ``text=True``: the ceiling is counted in bytes, and a
    decoder in front of the pipe would make "how much has arrived" a question
    about characters. Decoding happens once, at the end, on what was kept.
    """
    return subprocess.Popen(
        argv,
        executable=exe,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        start_new_session=True,
        env=env,
    )


def capture(
    argv: list[str],
    *,
    executable: str,
    timeout_s: float,
    max_output_b: int,
    env: dict[str, str] | None = None,
) -> tuple[int, str, str]:
    """Run ``argv`` and return ``(returncode, stdout, stderr)``, bounded.

    For the daemon's own calls -- the command registry, the shell's IPC
    listing, the desktop helpers -- rather than for an agent's. They parse what
    comes back, so the cap is a hard ceiling and passing it raises
    `OutputTooLarge` instead of quietly handing back half a document.

    Raises `subprocess.TimeoutExpired` on the deadline, which is what those
    callers already catch, so their messages are unchanged.
    """
    proc = _spawn(argv, executable, trust.child_env(env))
    out, err, timed_out, overflowed = _drain(proc, max_output_b, max_output_b, timeout_s)
    _finish(proc, stop=timed_out or overflowed)
    if timed_out:
        raise subprocess.TimeoutExpired(argv, timeout_s)
    if overflowed:
        raise OutputTooLarge(argv[0], max_output_b)
    return proc.returncode, out.text()[0], err.text()[0]


def run(
    argv: list[str],
    *,
    timeout_ms: int,
    max_output_b: int,
    detach: bool = False,
    env: dict[str, str] | None = None,
) -> Result:
    """Execute ``argv`` with no shell involved.

    ``argv`` is the program and its arguments as separate list entries:
    ``["omarchy", "theme", "set", "tokyo-night"]``. Everything after the bare
    ``*`` must be passed by name.

    Blocking, and deliberately so -- it waits for the child. Callers on the
    event loop reach it through ``tools/_shared.py``'s ``offload``, which runs it
    on a worker thread.
    """
    if not argv:
        raise ValueError("argv must not be empty")

    # Raises NotInstalled rather than letting execve fail: a FileNotFoundError
    # out of Popen reaches the agent as a tool error with the cause stripped.
    exe = resolve_binary(argv[0])

    # The child gets the trusted PATH rather than this process's. Choosing the
    # right file settles nothing if the command then looks its own helpers up on
    # a list nobody checked. See `trust.child_env`, which states the cost.
    full_env = trust.child_env(env)

    if detach:
        # start_new_session detaches from our process group, so the child
        # survives a daemon restart and never receives our signals.
        proc = subprocess.Popen(
            argv,
            executable=exe,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
            env=full_env,
        )
        return Result(
            exit_code=None,
            stdout="",
            stderr="",
            timed_out=False,
            detached=True,
            pid=proc.pid,
            executable=exe,
        )

    # `executable` rather than rewriting argv[0]: the process runs the file we
    # chose, while the command reported back to the agent stays the copy-
    # pasteable `omarchy theme set` rather than an absolute path.
    proc = _spawn(argv, exe, full_env)

    # The cap the agent sees, and the ceiling this process will actually read.
    # They are different numbers on purpose; see `ceiling_for`.
    out, err, timed_out, overflowed = _drain(
        proc, max_output_b, ceiling_for(max_output_b), timeout_ms / 1000
    )
    _finish(proc, stop=timed_out or overflowed)

    stdout, cut_out = out.text()
    stderr, cut_err = err.text()
    # Passing the ceiling is a truncation whether or not the sink had to shape
    # anything: the command was stopped, so what is here is not all there was.
    cut_out = cut_out or overflowed

    return Result(
        exit_code=proc.returncode,
        stdout=stdout,
        stderr=stderr,
        timed_out=timed_out,
        detached=False,
        pid=proc.pid,
        truncated=cut_out or cut_err,
        executable=exe,
    )


def _terminate_group(proc: subprocess.Popen[Any]) -> None:
    """SIGTERM the child and everything it started; fall back to just the child."""
    try:
        # ``getpgid`` finds the process group the child leads (it leads one
        # because ``run`` started it with ``start_new_session=True``), and
        # ``killpg`` signals every process in it.
        os.killpg(os.getpgid(proc.pid), signal.SIGTERM)
    except (ProcessLookupError, PermissionError):
        # Already gone, or not ours to signal. Signalling the one process we
        # certainly own is the best that is left.
        proc.terminate()


def quote(argv: list[str]) -> str:
    """A copy-pasteable rendering of what was run, for logs and errors.

    ``shlex.join`` adds quoting wherever a shell would need it, so the line can
    be pasted into a terminal and mean exactly what ran. It is the inverse of
    the shell parsing this module is careful never to do -- for display only,
    and nothing in this daemon ever reads it back.
    """
    return shlex.join(argv)
