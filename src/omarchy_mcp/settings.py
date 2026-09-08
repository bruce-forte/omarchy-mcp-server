"""The live configuration and permissions: one holder, read once per call.

`Config` and `Permissions` are frozen and stay frozen. What changes is which
one this holder points at, so a reload is one attribute assignment rather than a
walk over every closure that captured a value.

They are two files with two failure rules -- a broken `config.toml` is reported
and ignored, a broken permissions document is not applied at all -- so they swap
independently. One holder, because a tool call wants both and taking them from
two places is how they come from two different moments.

The holder stops at the edge. A tool body, a resource read, or a reload does
`settings.current` **once** and passes that snapshot down; `policy.py`,
`gate.py`, `consent.py` and `execute.py` keep taking an immutable `Config` and
know nothing about reloading. Two consequences worth the arrangement:

- **One call is decided by one config.** A reload landing between the policy
  check and the execution cannot half-apply, because the second half is reading
  a value the first half already took.
- **The security boundary's tests do not change.** They pass a `Config` and a
  `Permissions` because those are still what the boundary takes.

Swapping is an attribute assignment, which is atomic under the GIL: a reader is
never handed a half-built config, whichever thread it runs on. There is no lock
because there is nothing to serialise -- readers take one reference and are done
with it.

The GIL is CPython's global interpreter lock, which lets only one thread run
Python bytecode at a time. It is usually discussed as a limitation; the useful
consequence here is that a single assignment cannot be observed half-done, so
``self._current = config`` needs no locking of its own.
"""

from __future__ import annotations

from .config import Config
from .permissions import Permissions


class Settings:
    """Holds what the daemon is currently running under."""

    def __init__(self, config: Config, permissions: Permissions | None = None) -> None:
        """Start out pointing at ``config``, and at ``permissions`` if given.

        ``Permissions | None`` means "either, and ``None`` is allowed"; the
        default is spelled ``None`` rather than ``Permissions()`` because a
        mutable-looking default is evaluated once at import and shared forever.
        An empty ``Permissions()`` grants nothing, which is the safe start.
        """
        self._current = config
        self._permissions = permissions if permissions is not None else Permissions()
        self._unreviewed: frozenset[str] = frozenset()

    # ``@property`` makes the method read like an attribute -- ``settings.current``
    # rather than ``settings.current()``. There is no matching setter, so the
    # only way to change it is the explicit ``swap`` below.
    @property
    def current(self) -> Config:
        """The configuration in force. Take one snapshot per call, not per read."""
        return self._current

    @property
    def permissions(self) -> Permissions:
        """What an agent may run. One snapshot per call, same as `current`."""
        return self._permissions

    def swap(self, config: Config) -> Config:
        """Point at a new configuration. Returns the one it replaced."""
        previous = self._current
        self._current = config
        return previous

    def swap_permissions(self, permissions: Permissions) -> Permissions:
        """Point at a new permissions document. Returns the one it replaced."""
        previous = self._permissions
        self._permissions = permissions
        return previous

    @property
    def unreviewed(self) -> frozenset[str]:
        """Routes an update added under an existing `allow`, not yet reviewed.

        Held here rather than inside `Permissions` because it is not something
        the user wrote: it is the difference between the document and the
        machine, and it is cleared by acknowledging rather than by editing.
        """
        return self._unreviewed

    def swap_unreviewed(self, routes: frozenset[str]) -> frozenset[str]:
        """Replace the unreviewed set. Returns the one it replaced."""
        previous = self._unreviewed
        self._unreviewed = routes
        return previous

    # A ``@classmethod`` receives the class itself as ``cls`` rather than an
    # instance, which is how Python spells an alternative constructor:
    # ``Settings.of(x)`` is called without there being a ``Settings`` yet.
    @classmethod
    def of(cls, config: Config | Settings) -> Settings:
        """Accept either, so a caller that never reloads can pass a `Config`.

        The tests and `make tools` build a server that lives for one function
        call; making them construct a holder they will never swap would be
        ceremony that hides what those call sites are actually saying.
        """
        return config if isinstance(config, Settings) else cls(config)
