"""Execution semantics: no shell, bounded time, bounded output."""

from __future__ import annotations

import os
import subprocess
import time

import pytest

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
    _, truncated = execute.cap(text, 99)
    assert truncated is True


# --- bounded output (N22) -------------------------------------------------
#
# The cap used to be applied after `communicate()` returned, so the whole of a
# command's output was in memory before any limit was consulted. These pin the
# difference between trimming what the agent sees and bounding what the daemon
# holds.


class TestTheSink:
    """The accumulator, on its own. The subtle cases live here."""

    def test_output_under_the_limit_is_kept_whole(self):
        sink = execute._Sink(4096)
        sink.feed(b"hello")
        assert sink.text() == ("hello", False)

    def test_exactly_the_limit_is_not_truncated(self):
        sink = execute._Sink(100)
        sink.feed(b"x" * 100)
        text, truncated = sink.text()
        assert truncated is False
        assert len(text) == 100

    def test_the_middle_is_not_lost_just_under_the_limit(self):
        """`keep` is `limit // 2 - 64`, so two of them come to `limit - 128`.

        A sink that collapsed to head-and-tail as soon as the first `keep` bytes
        arrived would silently drop up to 128 bytes out of the middle of output
        that was never over the limit at all. This is that gap.
        """
        limit = 4096
        body = bytes(range(256)) * 16  # 4096 bytes, every value distinct in place
        sink = execute._Sink(limit)
        sink.feed(body[: limit - 1])
        text, truncated = sink.text()
        assert truncated is False
        assert len(text.encode("utf-8", "replace")) == limit - 1 or len(text) == limit - 1

    def test_over_the_limit_keeps_both_ends(self):
        sink = execute._Sink(4096)
        sink.feed(b"A" * 50000 + b"ZZZZ")
        text, truncated = sink.text()
        assert truncated is True
        assert text.startswith("A")
        assert text.endswith("ZZZZ")
        assert "bytes dropped" in text

    def test_memory_is_bounded_however_much_arrives(self):
        """The point of the whole exercise."""
        limit = 4096
        sink = execute._Sink(limit)
        for _ in range(200):
            sink.feed(b"x" * 100_000)
        assert sink.total == 20_000_000
        assert len(sink.head) + len(sink.tail) <= limit + 1 + execute._keep_for(limit)


class TestTheCeiling:
    def test_it_is_larger_than_what_the_agent_is_shown(self):
        """Presentational cap and structural ceiling are different numbers: a
        command legitimately printing a few megabytes is trimmed, not killed."""
        assert execute.ceiling_for(4096) > 4096
        assert execute.ceiling_for(256 * 1024) == 256 * 1024 * execute.OUTPUT_CEILING_FACTOR

    def test_a_tiny_cap_still_gets_a_usable_floor(self):
        assert execute.ceiling_for(16) == execute.MIN_OUTPUT_CEILING_B


class TestARunawayIsStopped:
    def test_endless_output_does_not_run_to_the_timeout(self, monkeypatch):
        """`yes` produces about a gigabyte a second. The timeout bounds how long
        a command runs, not how much it prints inside that time -- so this has to
        end because of the ceiling, well before the deadline."""
        monkeypatch.setattr(execute, "MIN_OUTPUT_CEILING_B", 256 * 1024)
        started = time.monotonic()
        result = execute.run(
            ["bash", "-c", "yes"], timeout_ms=30_000, max_output_b=4096
        )
        elapsed = time.monotonic() - started

        assert elapsed < 10, "the ceiling should have stopped this long before the timeout"
        assert result.timed_out is False, "stopped for size, not for time"
        assert result.truncated is True
        assert len(result.stdout.encode()) < 64 * 1024

    def test_a_runaway_on_stderr_is_stopped_too(self, monkeypatch):
        """Either stream trips it; stderr is the one a caller forgets."""
        monkeypatch.setattr(execute, "MIN_OUTPUT_CEILING_B", 256 * 1024)
        started = time.monotonic()
        result = execute.run(
            ["bash", "-c", "yes >&2"], timeout_ms=30_000, max_output_b=4096
        )
        assert time.monotonic() - started < 10
        assert len(result.stderr.encode()) < 64 * 1024

    def test_the_whole_group_is_reaped(self, monkeypatch):
        """An Omarchy command is usually a shell script with children. Killing
        only the parent leaves them writing, and leaves a zombie behind in a
        daemon meant to run for weeks."""
        monkeypatch.setattr(execute, "MIN_OUTPUT_CEILING_B", 256 * 1024)
        result = execute.run(
            ["bash", "-c", "yes & yes"], timeout_ms=30_000, max_output_b=4096
        )
        assert result.pid is not None
        # The child has been waited on, so its pid is no longer a live process
        # of ours. `os.kill(pid, 0)` raises once it is gone.
        with pytest.raises(OSError):
            os.kill(result.pid, 0)


class TestCapture:
    """The daemon's own calls. For them the cap is a hard ceiling."""

    def test_it_returns_code_and_streams(self):
        code, out, err = execute.capture(
            ["bash", "-c", "echo out; echo err >&2; exit 3"],
            executable=execute.resolve_binary("bash"),
            timeout_s=5,
            max_output_b=4096,
        )
        assert code == 3
        assert out.strip() == "out"
        assert err.strip() == "err"

    def test_too_much_output_raises_rather_than_truncating(self):
        """Handing a parser half a document turns a bound into a JSON error
        three frames away."""
        with pytest.raises(execute.OutputTooLarge):
            execute.capture(
                ["bash", "-c", "yes"],
                executable=execute.resolve_binary("bash"),
                timeout_s=30,
                max_output_b=64 * 1024,
            )

    def test_a_timeout_is_raised_as_the_caller_expects(self):
        """`registry.py` and friends already catch this one, so their messages
        keep working."""
        with pytest.raises(subprocess.TimeoutExpired):
            execute.capture(
                ["sleep", "30"],
                executable=execute.resolve_binary("sleep"),
                timeout_s=0.3,
                max_output_b=4096,
            )
