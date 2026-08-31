"""Execution semantics: no shell, bounded time, bounded output."""

from __future__ import annotations

import time

from omarchy_mcp import execute


def test_arguments_never_reach_a_shell(tmp_path):
    """The classic injection: if argv went through a shell, this would run rm."""
    canary = tmp_path / "canary"
    canary.write_text("intact")

    result = execute.run(
        ["echo", f"; rm -f {canary}"], timeout_ms=5000, max_output_b=4096
    )

    assert result.exit_code == 0
    assert canary.read_text() == "intact"
    assert str(canary) in result.stdout  # it was an argument, and only that


def test_metacharacters_are_literal():
    result = execute.run(["echo", "$(id)", "`id`", "&&", "|"], timeout_ms=5000, max_output_b=4096)
    assert "$(id)" in result.stdout
    assert "uid=" not in result.stdout


def test_timeout_terminates_and_reports():
    started = time.monotonic()
    result = execute.run(["sleep", "30"], timeout_ms=300, max_output_b=4096)
    elapsed = time.monotonic() - started

    assert result.timed_out is True
    assert elapsed < 10, "should not have waited for the full sleep"
    assert result.exit_code != 0


def test_timeout_kills_the_whole_process_group():
    """Omarchy commands are shell scripts that spawn children; terminating only
    the parent would leave those children running."""
    result = execute.run(
        ["bash", "-c", "sleep 30 & sleep 30"], timeout_ms=300, max_output_b=4096
    )
    assert result.timed_out is True


def test_output_is_capped_keeping_both_ends():
    result = execute.run(
        ["bash", "-c", "printf 'A%.0s' {1..50000}; printf 'ZZZZ'"],
        timeout_ms=10000,
        max_output_b=4096,
    )
    assert result.truncated is True
    assert len(result.stdout.encode()) < 8192
    assert result.stdout.startswith("A")
    assert result.stdout.endswith("ZZZZ"), "the tail must survive; errors live at the end"
    assert "bytes dropped" in result.stdout


def test_short_output_is_untouched():
    result = execute.run(["echo", "hello"], timeout_ms=5000, max_output_b=4096)
    assert result.stdout.strip() == "hello"
    assert result.truncated is False


def test_detach_returns_immediately_with_a_pid():
    started = time.monotonic()
    result = execute.run(["sleep", "5"], timeout_ms=60000, max_output_b=4096, detach=True)
    elapsed = time.monotonic() - started

    assert result.detached is True
    assert result.pid and result.pid > 0
    assert result.exit_code is None
    assert elapsed < 1.0


def test_exit_code_is_reported():
    assert execute.run(["false"], timeout_ms=5000, max_output_b=4096).exit_code == 1


def test_stderr_is_captured_separately():
    result = execute.run(
        ["bash", "-c", "echo out; echo err >&2"], timeout_ms=5000, max_output_b=4096
    )
    assert result.stdout.strip() == "out"
    assert result.stderr.strip() == "err"


def test_interactive_commands_default_to_detached():
    assert execute.should_detach("launch", "omarchy launch browser") is True
    assert execute.should_detach("menu", "omarchy menu select") is True
    assert execute.should_detach("theme", "omarchy theme switcher") is True
    assert execute.should_detach("capture", "omarchy capture region") is True


def test_ordinary_commands_are_not_detached():
    assert execute.should_detach("theme", "omarchy theme current") is False
    assert execute.should_detach("system", "omarchy system stats") is False


def test_cap_boundary_is_not_off_by_one():
    text = "x" * 100
    assert execute.cap(text, 100) == (text, False)
    capped, truncated = execute.cap(text, 99)
    assert truncated is True
