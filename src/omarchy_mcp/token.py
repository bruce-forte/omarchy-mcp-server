"""The bearer token.

Generated once, kept at mode 0600 in the state directory. It is the only thing
standing between this server and any other process on the machine, including a
web page that has talked your browser into a request to loopback.
"""

from __future__ import annotations

import os
import secrets
import stat

from .paths import STATE_DIR, TOKEN_FILE

TOKEN_BYTES = 32


def ensure() -> str:
    """Return the token, creating it on first use."""
    if TOKEN_FILE.exists():
        existing = TOKEN_FILE.read_text().strip()
        if existing:
            _harden(TOKEN_FILE)
            return existing

    STATE_DIR.mkdir(parents=True, exist_ok=True)
    token = secrets.token_urlsafe(TOKEN_BYTES)
    # Create with the right mode from the start; writing then chmod'ing leaves a
    # window where the token is world-readable.
    fd = os.open(TOKEN_FILE, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as handle:
        handle.write(token + "\n")
    return token


def _harden(path) -> None:
    mode = stat.S_IMODE(path.stat().st_mode)
    if mode & 0o077:
        path.chmod(0o600)
