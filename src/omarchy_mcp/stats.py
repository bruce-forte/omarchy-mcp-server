"""What the server has been asked to do.

Surfaced through /health so the supervising QML can put it in the bar tooltip.
This is the only feedback loop a user has: the client is a language model, and
nothing else on the desktop reveals that an agent just did something.
"""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass, field


@dataclass
class Stats:
    started_at: float = field(default_factory=time.monotonic)
    calls: int = 0
    failures: int = 0
    last_tool: str = ""
    last_route: str = ""

    _lock: threading.Lock = field(default_factory=threading.Lock, repr=False)

    def record(self, tool: str, *, route: str = "", ok: bool = True) -> None:
        with self._lock:
            self.calls += 1
            if not ok:
                self.failures += 1
            self.last_tool = tool
            self.last_route = route

    def snapshot(self) -> dict[str, object]:
        with self._lock:
            return {
                "uptime_s": round(time.monotonic() - self.started_at),
                "calls": self.calls,
                "failures": self.failures,
                "last_tool": self.last_tool,
                "last_route": self.last_route,
            }
