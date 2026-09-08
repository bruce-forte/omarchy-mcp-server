"""What the server has been asked to do.

Two consumers of the same event. The counters are surfaced through `/health`,
so the supervising QML can put them in the bar tooltip -- the only feedback loop
a user has, since the client is a language model and nothing else on the desktop
reveals that an agent just did something. The same event also goes to
`activity.py`, which keeps it after the daemon is gone.

A third consumer, `frames.py`, gets told on the way out too: the counters only
reach the bar on a ten-second poll, so the frame is what makes an agent's action
visible at the moment it happens rather than whenever the widget next asks.

`call()` is the seam. A tool opens one, fills in what it learns, and exactly one
record is written when it leaves -- including when it leaves by exception. That
is what makes the two long-standing bugs unrepresentable: `omarchy_run` used to
count before the gate ran, so a refusal was recorded as a success, and the
desktop tools recorded once per branch.

The seam is a *context manager*, used as ``with stats.call("tool") as rec:``.
A context manager is any object with "set up" and "tear down" halves that
``with`` runs around a block; the point is that the tear-down half runs no
matter how the block ends -- normal return, ``return`` from the middle, or an
exception on the way through. That is the guarantee this module is built on.
"""

from __future__ import annotations

import threading
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from typing import Any

from .activity import Record, Sink


@dataclass
class Stats:
    """Live counters for the whole daemon. One instance, shared by every tool.

    ``@dataclass`` turns the annotated names below into constructor arguments
    with these defaults, so ``Stats()`` is a fresh zeroed counter set.
    """

    #: ``default_factory`` rather than ``= time.monotonic()``: a plain default is
    #: evaluated once, when the class is defined, and every instance would then
    #: share the import-time clock reading. A factory is called per instance.
    #:
    #: ``monotonic`` rather than wall-clock time: it only ever moves forward, so
    #: an NTP correction cannot make an uptime negative.
    started_at: float = field(default_factory=time.monotonic)
    calls: int = 0
    failures: int = 0
    last_tool: str = ""
    last_route: str = ""

    #: Where records go after they are counted. ``None`` is counters only,
    #: which is what a test gets and what `log.activity = false` produces.
    sink: Sink | None = None

    #: Told that a call finished, for the stdout frame the shell reads. Separate
    #: from `sink` because it is not the audit trail and does not follow
    #: `log.activity`: turning the log off should not blind the bar.
    on_call: Callable[[Record], None] | None = None

    #: Tool bodies run on worker threads, so two can reach the counters at once
    #: and ``self.calls += 1`` is not one indivisible step -- it is a read, an
    #: add and a write, and two threads interleaving them lose a count. A lock
    #: lets only one thread inside at a time. ``repr=False`` keeps it out of the
    #: generated ``__repr__``, where it would be noise.
    _lock: threading.Lock = field(default_factory=threading.Lock, repr=False)

    @contextmanager
    def call(self, tool: str) -> Iterator[Record]:
        """One tool call. Times it, and records it once however it ends.

        ``@contextmanager`` turns a generator into something usable with
        ``with``: everything before the ``yield`` is the set-up, the yielded
        value is what ``as rec`` binds, and everything after it is the tear-down.
        """
        rec = Record(tool=tool)
        started = time.monotonic()
        try:
            # Control leaves here and runs the caller's ``with`` block. It comes
            # back on the way out -- returning normally, or raising through.
            yield rec
        # ``BaseException`` rather than ``Exception``: the latter deliberately
        # excludes cancellation and Ctrl-C, and a cancelled call still needs its
        # record written.
        except BaseException:
            # The tool raised, or the call was cancelled under it. Either way
            # nothing completed, and that is a fault in this daemon rather than
            # a command that exited non-zero -- the two want different
            # reactions, so they get different outcomes.
            if not rec.outcome:
                rec.outcome = "error"
            # Re-raise the exception unchanged: this clause is here to label the
            # record, not to swallow the failure.
            raise
        # ``finally`` runs on every path out of the ``try`` -- success, handled
        # exception, re-raised exception. This is the "exactly once" the module
        # docstring promises.
        finally:
            rec.ms = round((time.monotonic() - started) * 1000)
            self._finish(rec)

    def _finish(self, rec: Record) -> None:
        """Count the call, then hand the record to whoever else wants it."""
        # Only the counters need the lock. The sink and the frame writer do their
        # own synchronisation, and holding a lock across them would make every
        # tool call wait on a disk write.
        with self._lock:
            self.calls += 1
            if not rec.ok:
                self.failures += 1
            self.last_tool = rec.tool
            self.last_route = rec.route
        if self.sink is not None:
            self.sink.append(rec.as_dict())
        if self.on_call is not None:
            self.on_call(rec)

    def snapshot(self) -> dict[str, Any]:
        """A consistent copy of the counters, for `/health`.

        Under the lock so the five values describe one moment rather than five.
        """
        with self._lock:
            return {
                "uptime_s": round(time.monotonic() - self.started_at),
                "calls": self.calls,
                "failures": self.failures,
                "last_tool": self.last_tool,
                "last_route": self.last_route,
            }
