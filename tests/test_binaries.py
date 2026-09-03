"""Which file a bare command name runs.

Not a security control, and deliberately absent from `SECURITY.md`: no MCP
client can influence this daemon's environment, so the attack it would defend
against does not exist. It is here because a desktop session's `PATH` is not a
thing anyone designed. On the machine this was written on the daemon inherited
`/usr/share/omarchy/bin`, then fifty-five toolchain-manager shims, and only then
`/usr/bin` -- so which `tesseract` an OCR call used was a property of what the
user had most recently installed.

The second half is that a missing dependency should say so. `execute.run` was
the one spawn site that let `FileNotFoundError` escape, and the SDK strips the
cause, so a renamed `omarchy` reached the agent as "Error executing tool
omarchy_run" and nothing else.
"""

from __future__ import annotations

import json
import logging
import os

import pytest

from omarchy_mcp import execute
from omarchy_mcp.config import Config
from omarchy_mcp.permissions import Permissions
from omarchy_mcp.stats import Stats
from omarchy_mcp.tools._shared import run_route


def fake_binary(directory, name: str, body: str = "#!/bin/sh\nexit 0\n"):
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / name
    path.write_text(body)
    path.chmod(0o755)
    return path


class TestResolution:
    def test_the_first_directory_wins(self, tmp_path):
        """Order is the whole point: Omarchy's own bin comes before /usr/bin."""
        first, second = tmp_path / "a", tmp_path / "b"
        fake_binary(first, "thing")
        fake_binary(second, "thing")
        assert execute.resolve_binary("thing", (first, second)) == str(first / "thing")

    def test_a_later_directory_is_used_when_the_first_has_nothing(self, tmp_path):
        first, second = tmp_path / "a", tmp_path / "b"
        first.mkdir()
        fake_binary(second, "thing")
        assert execute.resolve_binary("thing", (first, second)) == str(second / "thing")

    def test_a_file_that_is_not_executable_is_not_a_binary(self, tmp_path):
        """A stray same-named data file must not shadow the real one."""
        first, second = tmp_path / "a", tmp_path / "b"
        first.mkdir()
        (first / "thing").write_text("not a program")
        fake_binary(second, "thing")
        assert execute.resolve_binary("thing", (first, second)) == str(second / "thing")

    def test_a_directory_is_not_a_binary(self, tmp_path):
        first, second = tmp_path / "a", tmp_path / "b"
        (first / "thing").mkdir(parents=True)
        fake_binary(second, "thing")
        assert execute.resolve_binary("thing", (first, second)) == str(second / "thing")

    def test_a_path_is_used_as_given(self, tmp_path):
        """Standard PATH semantics, and it keeps an explicit choice explicit."""
        explicit = fake_binary(tmp_path / "elsewhere", "thing")
        assert execute.resolve_binary(str(explicit), (tmp_path / "a",)) == str(explicit)

    def test_nothing_found_names_the_binary_and_where_it_looked(self, tmp_path):
        with pytest.raises(execute.NotInstalled) as caught:
            execute.resolve_binary("tesseract", (tmp_path / "a", tmp_path / "b"))
        exc = caught.value
        assert exc.name == "tesseract"
        assert "`tesseract` is not installed" in str(exc)
        assert str(tmp_path / "a") in str(exc)
        assert exc.as_dict()["missing"] == "tesseract"

    def test_the_search_list_follows_omarchy_path(self):
        """The Makefile already trusts $OMARCHY_PATH; a dev-linked checkout has
        to get its own binaries rather than the system's."""
        from omarchy_mcp.paths import OMARCHY_PATH

        assert execute.SEARCH[0] == OMARCHY_PATH / "bin"
        assert str(execute.SEARCH[-1]) == "/usr/bin"

    def test_the_session_path_is_not_searched(self, tmp_path, monkeypatch):
        """The whole point. A shim directory ahead of /usr/bin must not decide
        which binary an OCR call uses."""
        shim = tmp_path / "shims"
        fake_binary(shim, "grim")
        monkeypatch.setenv("PATH", str(shim) + os.pathsep + os.environ["PATH"])
        with pytest.raises(execute.NotInstalled):
            execute.resolve_binary("grim", (tmp_path / "nowhere",))


class TestRunning:
    def test_it_runs_the_resolved_file(self, tmp_path, monkeypatch):
        marker = tmp_path / "ran"
        fake_binary(tmp_path / "bin", "thing", f"#!/bin/sh\ntouch {marker}\n")
        monkeypatch.setattr(execute, "SEARCH", (tmp_path / "bin",))

        result = execute.run(["thing"], timeout_ms=5000, max_output_b=4096)

        assert result.exit_code == 0
        assert marker.exists()
        assert result.executable == str(tmp_path / "bin" / "thing")

    def test_argv0_is_left_alone_so_the_reported_command_is_unchanged(
        self, tmp_path, monkeypatch
    ):
        """The agent is told `omarchy theme set`, not an absolute path. That
        string is in the README, in TOOLS.md, and is meant to be pasteable."""
        fake_binary(tmp_path / "bin", "omarchy")
        monkeypatch.setattr(execute, "SEARCH", (tmp_path / "bin",))

        result = execute.run(["omarchy", "theme", "list"], timeout_ms=5000, max_output_b=4096)

        assert execute.quote(["omarchy", "theme", "list"]) == "omarchy theme list"
        assert "executable" not in result.as_dict(), "which file ran is for the log"

    def test_a_missing_binary_is_not_a_stripped_tool_error(self, tmp_path, monkeypatch):
        monkeypatch.setattr(execute, "SEARCH", (tmp_path / "empty",))
        with pytest.raises(execute.NotInstalled):
            execute.run(["nothing-here"], timeout_ms=5000, max_output_b=4096)

    def test_a_detached_command_resolves_too(self, tmp_path, monkeypatch):
        """Detaching skips the whole result path, so it is easy to miss."""
        monkeypatch.setattr(execute, "SEARCH", (tmp_path / "empty",))
        with pytest.raises(execute.NotInstalled):
            execute.run(["nothing-here"], timeout_ms=5000, max_output_b=4096, detach=True)


class TestWhatTheAgentSees:
    @pytest.mark.anyio
    async def test_a_missing_binary_reads_as_a_missing_binary(self, tmp_path, monkeypatch):
        monkeypatch.setattr(execute, "SEARCH", (tmp_path / "empty",))

        payload = json.loads(
            await run_route(
                "omarchy theme list",
                [],
                config=Config(),
                stats=Stats(),
                perms=Permissions(),
                log=logging.getLogger("test"),
                tool="omarchy_theme",
            )
        )

        assert payload["missing"] == "omarchy"
        assert "not installed" in payload["error"]
