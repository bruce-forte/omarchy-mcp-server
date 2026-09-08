"""The bearer token.

Generated once, kept at mode 0600 in the state directory. It is the only thing
standing between this server and any other process on the machine, including a
web page that has talked your browser into a request to loopback.

The mode matters as much as the randomness. ``0600`` is the octal Unix
permission "owner may read and write, nobody else may do anything"; every other
user on the machine is locked out by the filesystem rather than by hope.
"""

from __future__ import annotations

import os
import secrets
import stat

from .paths import STATE_DIR, TOKEN_FILE

#: How much randomness goes into the token. 32 bytes is 256 bits, far past
#: anything guessable.
TOKEN_BYTES = 32


def ensure() -> str:
    """Return the token, creating it on first use.

    Called on every start. A token that already exists is reused, so restarting
    the daemon does not invalidate the one sitting in a client's config file.
    """
    if TOKEN_FILE.exists():
        existing = TOKEN_FILE.read_text().strip()
        if existing:
            _harden(TOKEN_FILE)
            return existing

    # ``parents=True`` creates any missing directory above it too; ``exist_ok``
    # means "already there" is success rather than an exception.
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    # ``secrets``, never ``random``: the latter is a predictable generator meant
    # for simulations, and its output can be reconstructed from earlier output.
    token = secrets.token_urlsafe(TOKEN_BYTES)
    # Create with the right mode from the start; writing then chmod'ing leaves a
    # window where the token is world-readable. ``os.open`` is the low-level
    # call that takes a permission mode, unlike ``open()``, which does not; the
    # flags say write-only, create if absent, truncate if present.
    fd = os.open(TOKEN_FILE, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    # ``os.open`` hands back a raw file descriptor (an integer). ``fdopen``
    # wraps it in a normal file object, and ``with`` closes it on the way out --
    # including if the write raises.
    with os.fdopen(fd, "w") as handle:
        handle.write(token + "\n")
    return token


def _harden(path) -> None:
    """Re-tighten a token file that something else loosened.

    A leading underscore is the convention for "private to this module": nothing
    outside ``token.py`` is expected to call it.
    """
    # ``st_mode`` packs the file type and its permission bits into one integer;
    # ``S_IMODE`` masks off everything but the permissions.
    mode = stat.S_IMODE(path.stat().st_mode)
    # ``0o077`` is every bit belonging to group and others. A non-zero result
    # means somebody other than the owner has some access.
    if mode & 0o077:
        path.chmod(0o600)
