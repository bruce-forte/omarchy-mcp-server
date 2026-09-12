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

*Output is capped.* One command's output should not be able to bury the caller's
context.

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
import shlex
import signal
import subprocess
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

#: ``{dropped}`` is filled in by ``.format`` in `cap`, not an f-string: the
#: value is not known here, only the shape of the sentence.
_TRUNCATION_NOTE = "\n\n... [{dropped} bytes dropped] ...\n\n"


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
    keep = max(limit // 2 - 64, 256)
    # Slices: the first ``keep`` bytes, and the last ``keep`` bytes. Cutting at
    # an arbitrary byte can land mid-character, which is the other reason for
    # ``"replace"`` on the way back.
    head = raw[:keep].decode("utf-8", "replace")
    tail = raw[-keep:].decode("utf-8", "replace")
    note = _TRUNCATION_NOTE.format(dropped=len(raw) - 2 * keep)
    return head + note + tail, True


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
    proc = subprocess.Popen(
        argv,
        executable=exe,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        start_new_session=True,
        text=True,
        errors="replace",
        env=full_env,
    )

    timed_out = False
    try:
        # ``communicate`` reads both pipes and waits for the child. Reading them
        # by hand instead risks a deadlock: a child filling the stderr pipe
        # blocks forever while the parent is still waiting on stdout.
        stdout, stderr = proc.communicate(timeout=timeout_ms / 1000)
    except subprocess.TimeoutExpired:
        timed_out = True
        # The child was started in its own session, so signal the whole group:
        # an Omarchy command is usually a shell script with children of its own.
        _terminate_group(proc)
        try:
            # It was asked to stop; give it GRACE_S to do so tidily.
            stdout, stderr = proc.communicate(timeout=GRACE_S)
        except subprocess.TimeoutExpired:
            # It did not. SIGKILL cannot be ignored, and the final
            # ``communicate`` reaps the child so it does not become a zombie.
            proc.kill()
            stdout, stderr = proc.communicate()

    stdout, cut_out = cap(stdout or "", max_output_b)
    stderr, cut_err = cap(stderr or "", max_output_b)

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
