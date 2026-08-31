"""Counters are shared between the ASGI worker and the health route."""

from __future__ import annotations

import threading

from omarchy_mcp.stats import Stats


def test_starts_empty():
    snapshot = Stats().snapshot()
    assert snapshot["calls"] == 0
    assert snapshot["failures"] == 0
    assert snapshot["last_tool"] == ""


def test_records_a_call():
    stats = Stats()
    with stats.call("omarchy_run") as rec:
        rec.route = "omarchy theme current"
    snapshot = stats.snapshot()
    assert snapshot["calls"] == 1
    assert snapshot["failures"] == 0
    assert snapshot["last_route"] == "omarchy theme current"


def test_failures_are_counted_separately():
    stats = Stats()
    with stats.call("omarchy_run") as rec:
        rec.exit = 1
    with stats.call("omarchy_run") as rec:
        rec.exit = 0
    snapshot = stats.snapshot()
    assert snapshot["calls"] == 2
    assert snapshot["failures"] == 1


def test_uptime_is_reported():
    assert Stats().snapshot()["uptime_s"] >= 0


def test_concurrent_records_do_not_lose_counts():
    stats = Stats()

    def hammer():
        for _ in range(200):
            with stats.call("t"):
                pass

    threads = [threading.Thread(target=hammer) for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert stats.snapshot()["calls"] == 1600
