"""Which executables this daemon is willing to run, and why it believes them.

Every other module spawns something. This is the one place that decides whether
the file about to be executed is the file somebody reviewed.

**This is a security control.** It did not start as one. `execute.py` resolved
``argv[0]`` against a fixed list of directories and said so in its own
docstring: *robustness, not a security control*, on the reasoning that no MCP
client can influence this daemon's environment. The Omarchy marketplace security
review pointed out what that reasoning misses -- the daemon inherits a *session*
``PATH``, and a session ``PATH`` is influenced by whatever the user installed
last. On the machine this was written on, ``curl`` resolved to
``/home/linuxbrew/.linuxbrew/bin/curl``: a binary in a directory the user can
write, winning over ``/usr/bin/curl``, and it was already being executed by the
bootstrap before any token, any policy tier and any consent prompt existed. The
attack did not need an MCP client. It needed a package manager.

So resolution is now a gate rather than a convenience, and it answers three
questions in order:

*Is the name one of ours?* Only `TRUSTED_DIRS` are searched -- never ``PATH``.

*Is the file the one root put there?* The resolved file must be a regular,
executable file owned by ``uid 0`` and writable by nobody else. Then every
directory above it, to ``/``, must be the same. A root-owned binary inside a
directory the user can write is a binary the user can replace.

*And the link, as well as its target?* ``/usr/share/omarchy/bin/omarchy`` is a
symlink to ``/usr/bin/omarchy``, and ``/usr/bin/awk`` is a symlink to ``gawk``:
refusing links outright would refuse a healthy Omarchy. So links are followed,
and **both** paths are verified -- the literal one component by component, and
the fully resolved one. Replacing either would be enough, so neither is trusted
on its own.

Anything that does not answer all three refuses. `NotTrusted` names the file and
the reason, because a bootstrap that fails closed with no explanation reads
exactly like a missing dependency.

The same three questions are asked in bash by `bin/omarchy-mcp-trust`, for the
wrapper that runs before this module is importable. Two implementations of one
rule is a cost; the alternative is a wrapper that cannot check anything until
the interpreter it is building already exists.
"""

from __future__ import annotations

import os
from collections.abc import Iterable
from pathlib import Path

from .paths import OMARCHY_PATH

#: Where a bare command name may come from, in order.
#:
#: A fixed allowlist. ``PATH`` is not consulted, and neither is the current
#: directory. ``OMARCHY_PATH`` still derives the first entry, as it always has,
#: so a dev-linked checkout is *looked* in -- but a checkout under ``$HOME``
#: cannot pass the ownership check below, so it is skipped rather than trusted.
#: That is the intended outcome: the environment can no longer promote a
#: directory into this list, it can only add one that then has to earn it.
TRUSTED_DIRS: tuple[Path, ...] = (
    OMARCHY_PATH / "bin",
    Path("/usr/local/bin"),
    Path("/usr/bin"),
)

#: The owner every trusted component must have. Not configurable: "root" is the
#: whole claim being made about these files.
ROOT_UID = 0

#: Write bits that disqualify a file or a directory: group-write and
#: other-write. ``0o022`` is those two bits; ``mode & 0o022`` is non-zero when
#: either is set. Owner-write is fine -- the owner is root.
WRITABLE_BY_OTHERS = 0o022

#: How many symlinks deep resolution will go before giving up. ``os.path.realpath``
#: already collapses the chain; this only bounds the belt-and-braces walk.
MAX_LINK_DEPTH = 16


class NotTrusted(Exception):
    """A file was found, and this daemon will not execute it.

    Distinct from `execute.NotInstalled`, which means nothing was found at all.
    Keeping them apart matters for the message: "not installed" sends somebody
    to their package manager, and "found but not trusted" sends them to look at
    who owns the file. Answering the second with the first wastes an afternoon.
    """

    def __init__(self, path: str | os.PathLike[str], reason: str) -> None:
        self.path = str(path)
        self.reason = reason
        super().__init__(f"refusing to execute {self.path}: {reason}")


def _components(path: Path) -> list[Path]:
    """Every path from ``/`` down to ``path``, in that order.

    ``Path.parents`` yields the ancestors nearest-first, so it is reversed. The
    file itself is appended last, which is what lets one loop check the whole
    chain.
    """
    return [*reversed(path.parents), path]


def _inspect(path: Path) -> os.stat_result:
    """``lstat``, or a refusal.

    ``lstat`` rather than ``stat``: it describes the entry itself rather than
    what it points at, which is the thing being asked about here.

    A path that cannot be inspected is not trusted, and saying so as a
    `NotTrusted` matters. Letting ``FileNotFoundError`` escape would send an
    absolute path that names nothing out of this module as an unhandled OS
    error, past every caller that knows how to explain a refusal.
    """
    try:
        return path.lstat()
    except OSError as exc:
        raise NotTrusted(path, f"cannot be inspected ({exc.strerror})") from exc


def _check_directory(directory: Path) -> None:
    """A directory anybody but root can write is a directory that proves nothing."""
    info = _inspect(directory)
    if info.st_uid != ROOT_UID:
        raise NotTrusted(directory, f"directory is owned by uid {info.st_uid}, not root")
    if info.st_mode & WRITABLE_BY_OTHERS:
        mode = info.st_mode & 0o777
        raise NotTrusted(directory, f"directory is writable by others (mode {mode:o})")


def _check_leaf(leaf: Path) -> None:
    """The file at the end: regular, executable, root's, and nobody else's."""
    info = _inspect(leaf)
    if not leaf.is_file():
        raise NotTrusted(leaf, "not a regular file")
    if info.st_uid != ROOT_UID:
        raise NotTrusted(leaf, f"owned by uid {info.st_uid}, not root")
    if info.st_mode & WRITABLE_BY_OTHERS:
        raise NotTrusted(leaf, f"writable by others (mode {info.st_mode & 0o777:o})")
    if not os.access(leaf, os.X_OK):
        raise NotTrusted(leaf, "not executable")


def _check_chain(path: Path) -> None:
    """Verify ``path`` and every directory above it.

    A symlink met along the way is checked for ownership only. Its *mode* says
    nothing -- Linux stores ``0o777`` on every symlink regardless -- so the
    question that can be asked about it is who is allowed to replace it, and
    that is answered by the owner of the link and the mode of its directory.
    """
    chain = _components(path)
    for component in chain[:-1]:
        _check_directory(component)
    leaf = chain[-1]
    if leaf.is_symlink():
        owner = _inspect(leaf).st_uid
        if owner != ROOT_UID:
            raise NotTrusted(leaf, f"symlink is owned by uid {owner}, not root")
    else:
        _check_leaf(leaf)


def verify(path: str | os.PathLike[str]) -> str:
    """Return ``path`` if this daemon may execute it; raise `NotTrusted` if not.

    Both the literal path and the fully resolved one are checked. Either being
    replaceable is enough to replace what runs, so trusting one because the
    other is sound would be trusting nothing.
    """
    literal = Path(path)
    if not literal.is_absolute():
        raise NotTrusted(literal, "not an absolute path")

    # The path as written, including any symlinks in it.
    _check_chain(literal)

    # And the file those links actually land on. ``resolve`` collapses the whole
    # chain; ``strict=True`` raises rather than inventing a path for something
    # that is not there.
    try:
        resolved = literal.resolve(strict=True)
    except OSError as exc:
        raise NotTrusted(literal, f"cannot be resolved ({exc.strerror})") from exc
    if resolved != literal:
        for component in _components(resolved)[:-1]:
            _check_directory(component)
    _check_leaf(resolved)
    return str(literal)


def resolve(name: str, directories: Iterable[Path], *, verified: bool = True) -> str | None:
    """The first entry named ``name`` in ``directories`` that may be executed.

    ``None`` when nothing is found, which is the caller's cue to raise
    `execute.NotInstalled` -- this module does not own that message.

    A candidate that exists but fails verification is **skipped, not fatal**, and
    the search continues. That is still failing closed: the untrusted file is
    never executed. It is what lets a dev-linked `OMARCHY_PATH` under ``$HOME``
    sit at the front of the list without either being trusted or breaking the
    lookup -- the trusted ``/usr/bin`` copy behind it is used instead.

    ``verified=False`` exists for the test suite, which builds fake binaries in
    a temporary directory it owns. Nothing in the daemon passes it.
    """
    for directory in directories:
        candidate = Path(directory) / name
        if not candidate.exists():
            continue
        if not verified:
            if candidate.is_file() and os.access(candidate, os.X_OK):
                return str(candidate)
            continue
        try:
            return verify(candidate)
        except NotTrusted:
            # Skipped deliberately: a shadowed binary must not stop the real one
            # being found behind it. The refusal is not silent -- `describe`
            # below is what the caller puts in the error when nothing is left.
            continue
    return None


def describe(name: str, directories: Iterable[Path]) -> list[str]:
    """Why each candidate for ``name`` was refused, for an error message.

    Only rejections are listed. An empty list means nothing by that name exists
    anywhere, which is an ordinary missing dependency rather than a shadowing.
    """
    reasons = []
    for directory in directories:
        candidate = Path(directory) / name
        if not candidate.exists():
            continue
        try:
            verify(candidate)
        except NotTrusted as exc:
            reasons.append(str(exc))
    return reasons


def child_env(overrides: dict[str, str] | None = None) -> dict[str, str]:
    """The environment a child gets: this process's, with ``PATH`` replaced.

    Replacing rather than prepending. A child of this daemon that looks a name
    up for itself has to reach the same files this module would have chosen,
    and a session ``PATH`` left on the end is a second list that was never
    checked -- which is the hole this module exists to close, one process along.

    The cost is real and worth stating: an Omarchy command that shells out to
    something the user installed in ``~/.local/bin`` no longer finds it. That is
    the trade the review asked for, and it is the safe side of it.
    """
    env = {**os.environ, **(overrides or {})}
    env["PATH"] = os.pathsep.join(str(directory) for directory in TRUSTED_DIRS)
    return env
