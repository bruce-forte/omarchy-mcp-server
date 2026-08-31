"""Shutting down has to be bounded, and the supervisor has to be able to insist.

F27: `omarchy-shell <plugin> restart` hung indefinitely, port unbound and
process alive, and it took a `kill -9` every time. Two causes in one bug --
uvicorn waits forever for connections to close by default, and an attached MCP
client holds its stream open for the life of the session. So the documented
reload hung precisely when a client was attached, which is whenever restarting
is worth doing.

The QML half of the fix is pinned here too, by reading `Service.qml`. It is not
importable and `qmllint` cannot know what the file is supposed to mean, so the
properties are asserted as text -- the same approach `test_bootstrap.py` takes
to the bash wrapper.
"""

from __future__ import annotations

import pathlib

import pytest

from omarchy_mcp import __main__ as entry

SERVICE = pathlib.Path(__file__).resolve().parents[1] / "Service.qml"


@pytest.fixture(scope="module")
def service() -> str:
    return SERVICE.read_text()


class TestTheDaemonBoundsItsOwnShutdown:
    def test_the_grace_period_is_finite_and_short(self):
        assert isinstance(entry.SHUTDOWN_GRACE_S, int)
        assert 0 < entry.SHUTDOWN_GRACE_S <= 10

    def test_uvicorn_is_given_the_deadline(self, monkeypatch, tmp_path):
        """Uvicorn's default is None, which means wait forever.

        Asserted at the call rather than by reading the source, because the
        default is the bug: leaving the argument off looks completely normal.
        """
        seen: dict = {}

        def fake_run(app, **kwargs):
            seen.update(kwargs)

        import uvicorn

        from omarchy_mcp import config as config_module, token as token_module

        monkeypatch.setattr(uvicorn, "run", fake_run)
        monkeypatch.setattr(token_module, "ensure", lambda: "x" * 43)
        monkeypatch.setattr(config_module, "load", lambda: config_module.Config())

        assert entry.main([]) == 0
        assert seen["timeout_graceful_shutdown"] == entry.SHUTDOWN_GRACE_S


class TestTheSupervisorCanInsist:
    def test_sigterm_has_a_deadline(self, service):
        assert "daemon.signal(9)" in service, "SIGTERM is asked politely, not obeyed"

    def test_the_kill_waits_longer_than_the_daemon_does(self, service):
        """Otherwise the ordinary path becomes the kill, and a tool call that
        was about to finish is destroyed on every restart."""
        interval = int(
            service.split("id: sigkill")[1].split("interval:")[1].split("\n")[0].strip()
        )
        assert interval > entry.SHUTDOWN_GRACE_S * 1000

    def test_a_restart_waits_for_the_exit_rather_than_guessing(self, service):
        """The 250ms timer this replaced fired while the daemon was still
        shutting down. start() then returned early on `daemon.running`, leaving
        wantRunning false -- so the eventual exit was filed as a deliberate stop
        and nothing ever came back."""
        assert "restartTimer" not in service, "a timer cannot know when a process exited"
        assert "restartPending" in service
        assert "root.start()" in service.split("onExited:")[1]

    def test_an_already_stopped_daemon_still_restarts(self, service):
        """Nothing will exit, so nothing would start it: the one case the
        exit-driven path cannot cover on its own."""
        body = service.split("function restart()")[1].split("function ")[0]
        assert "wasRunning" in body
        assert "start()" in body
