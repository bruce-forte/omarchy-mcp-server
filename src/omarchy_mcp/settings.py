"""The live configuration: one holder, read once per call.

`Config` is frozen and stays frozen. What changes is which `Config` this holder
points at, so a reload is one attribute assignment rather than a walk over
every closure that captured a value.

The holder stops at the edge. A tool body, a resource read, or a reload does
`settings.current` **once** and passes that snapshot down; `policy.py`,
`gate.py`, `consent.py` and `execute.py` keep taking an immutable `Config` and
know nothing about reloading. Two consequences worth the arrangement:

- **One call is decided by one config.** A reload landing between the policy
  check and the execution cannot half-apply, because the second half is reading
  a value the first half already took.
- **The security boundary's tests do not change.** They pass a `Config` because
  that is still what the boundary takes.

Swapping is an attribute assignment, which is atomic under the GIL: a reader is
never handed a half-built config, whichever thread it runs on. There is no lock
because there is nothing to serialise -- readers take one reference and are done
with it.
"""

from __future__ import annotations

from .config import Config


class Settings:
    """Holds the configuration the daemon is currently running under."""

    def __init__(self, config: Config) -> None:
        self._current = config

    @property
    def current(self) -> Config:
        """The configuration in force. Take one snapshot per call, not per read."""
        return self._current

    def swap(self, config: Config) -> Config:
        """Point at a new configuration. Returns the one it replaced."""
        previous = self._current
        self._current = config
        return previous

    @classmethod
    def of(cls, config: Config | "Settings") -> "Settings":
        """Accept either, so a caller that never reloads can pass a `Config`.

        The tests and `make tools` build a server that lives for one function
        call; making them construct a holder they will never swap would be
        ceremony that hides what those call sites are actually saying.
        """
        return config if isinstance(config, cls) else cls(config)
