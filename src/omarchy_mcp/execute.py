"""Running commands.

Three properties matter here and each is deliberate:

*No shell, ever.* Arguments arrive from a language model. ``argv`` goes to
``execve`` as a list, so ``"; rm -rf ~"`` is an argument, not a command.

*Nothing waits forever.* Many Omarchy commands block on the user by design --
``theme switcher``, ``menu select``, ``capture region``. A blocked call means a
client timeout and a leaked child, so anything interactive is detached instead
and everything else is bounded.

*Output is capped.* One command's output should not be able to bury the caller's
context.
"""

from __future__ import annotations

import os
import shlex
import signal
import subprocess
from dataclasses import dataclass, asdict

#: Commands in these groups open a window or wait for the user. Waiting on them
#: is always wrong: they finish when a human is done, not when work is done.
DETACH_GROUPS = frozenset({"launch", "menu", "tui"})

#: Same, for routes whose group is otherwise unremarkable.
DETACH_MARKERS = ("switcher", "selector", "select", "region", "screenrecording", "screensaver")

#: How long a terminated child gets to exit before it is killed.
GRACE_S = 2.0

_TRUNCATION_NOTE = "\n\n... [{dropped} bytes dropped] ...\n\n"


@dataclass(frozen=True)
class Result:
    exit_code: int | None
    stdout: str
    stderr: str
    timed_out: bool
    detached: bool
    pid: int | None = None
    truncated: bool = False

    def as_dict(self) -> dict[str, object]:
        return asdict(self)


def should_detach(group: str, route: str) -> bool:
    """Whether this command finishes on a human's schedule rather than ours."""
    if group in DETACH_GROUPS:
        return True
    return any(marker in route for marker in DETACH_MARKERS)


def cap(text: str, limit: int) -> tuple[str, bool]:
    """Keep the head and tail of over-long output; the middle is the filler."""
    raw = text.encode("utf-8", "replace")
    if len(raw) <= limit:
        return text, False
    keep = max(limit // 2 - 64, 256)
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
    """Execute ``argv`` with no shell involved."""
    if not argv:
        raise ValueError("argv must not be empty")

    full_env = {**os.environ, **(env or {})}

    if detach:
        # start_new_session detaches from our process group, so the child
        # survives a daemon restart and never receives our signals.
        proc = subprocess.Popen(
            argv,
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
        )

    proc = subprocess.Popen(
        argv,
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
        stdout, stderr = proc.communicate(timeout=timeout_ms / 1000)
    except subprocess.TimeoutExpired:
        timed_out = True
        # The child was started in its own session, so signal the whole group:
        # an Omarchy command is usually a shell script with children of its own.
        _terminate_group(proc)
        try:
            stdout, stderr = proc.communicate(timeout=GRACE_S)
        except subprocess.TimeoutExpired:
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
    )


def _terminate_group(proc: subprocess.Popen) -> None:
    try:
        os.killpg(os.getpgid(proc.pid), signal.SIGTERM)
    except (ProcessLookupError, PermissionError):
        proc.terminate()


def quote(argv: list[str]) -> str:
    """A copy-pasteable rendering of what was run, for logs and errors."""
    return shlex.join(argv)
