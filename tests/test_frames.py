"""The stdout channel the shell reads.

Two promises are worth pinning. A frame is one parseable line even when several
request threads finish at once -- the `SplitParser` on the other end drops
anything else, silently. And a call frame carries no arguments, which is the
same boundary the activity log draws for the same reason: a clipboard write's
argument is the clipboard.
"""

from __future__ import annotations

import io
import json
import threading

import pytest

from omarchy_mcp import frames
from omarchy_mcp.stats import Stats


def lines(capsys) -> list[dict]:
    """Every frame written so far, parsed.

    Read through `capsys` rather than a monkeypatched `sys.stdout`: pytest
    re-installs its own stdout when it resumes capture for the call phase, so a
    patch applied in a fixture is silently undone before the test body runs.
    """
    return [json.loads(line) for line in capsys.readouterr().out.splitlines() if line]


class TestTheFrame:
    def test_a_lifecycle_frame_is_one_json_line(self, capsys):
        frames.emit("listening", port=8765, version="0.1.0")
        assert lines(capsys) == [{"state": "listening", "port": 8765, "version": "0.1.0"}]

    def test_a_call_frame_names_the_tool_and_how_it_ended(self, capsys):
        frames.call("omarchy_theme", "ok")
        assert lines(capsys) == [{"state": "call", "tool": "omarchy_theme", "outcome": "ok"}]

    def test_a_refusal_is_reported_as_one(self, capsys):
        frames.call("omarchy_run", "refused")
        assert lines(capsys)[0]["outcome"] == "refused"

    def test_a_broken_stdout_does_not_reach_the_caller(self, monkeypatch):
        class Broken(io.StringIO):
            def write(self, _text):
                raise OSError("broken pipe")

        monkeypatch.setattr("sys.stdout", Broken())
        # The call this frame describes has already happened. Failing here
        # would turn a dead supervisor into a failed tool call.
        frames.call("omarchy_theme", "ok")


class TestConcurrency:
    def test_frames_from_many_threads_stay_whole(self, capsys):
        def hammer(n):
            for _ in range(50):
                frames.call(f"tool_{n}", "ok")

        threads = [threading.Thread(target=hammer, args=(i,)) for i in range(8)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        # Every line parses, and none was lost or spliced into its neighbour.
        parsed = lines(capsys)
        assert len(parsed) == 400
        assert all(body["state"] == "call" for body in parsed)


class TestTheEditableFrame:
    def test_it_carries_the_token_and_nothing_else(self, capsys):
        frames.editable("tok")
        body = json.loads(capsys.readouterr().out)
        assert body == {"state": "editable", "token": "tok"}


class TestWhatIsNotInIt:
    """The same boundary the activity log draws, for the same reason."""

    def test_a_call_frame_carries_no_arguments(self, capsys):
        stats = Stats(on_call=lambda rec: frames.call(rec.tool, rec.result))
        with stats.call("omarchy_clipboard_write") as rec:
            rec.args = ("sk-ant-api03-a-secret-somebody-copied",)
            rec.exit = 0

        written = capsys.readouterr().out
        assert json.loads(written) == {
            "state": "call",
            "tool": "omarchy_clipboard_write",
            "outcome": "ok",
        }
        assert "secret" not in written


class TestTheSeam:
    def test_stats_tells_the_channel_once_per_call(self, capsys):
        stats = Stats(on_call=lambda rec: frames.call(rec.tool, rec.result))
        with stats.call("omarchy_run") as rec:
            rec.route = "omarchy theme current"
            rec.exit = 0
        assert len(lines(capsys)) == 1

    def test_a_raising_tool_still_emits_a_frame(self, capsys):
        stats = Stats(on_call=lambda rec: frames.call(rec.tool, rec.result))
        with pytest.raises(RuntimeError), stats.call("omarchy_run"):
            raise RuntimeError("boom")
        assert lines(capsys) == [{"state": "call", "tool": "omarchy_run", "outcome": "error"}]

    def test_no_frame_channel_is_counters_only(self):
        stats = Stats()
        with stats.call("omarchy_run") as rec:
            rec.exit = 0
        assert stats.snapshot()["calls"] == 1


class TestTheQuestionFrames:
    """`asking` is the deliberate exception to the no-arguments rule: consent
    that does not show what it is consenting to is not consent."""

    def test_asking_carries_the_call_and_the_token(self, capsys):
        frames.asking("tok", "omarchy theme remove", ["Tokyo Night"], "Tokyo Night", "(#1)")
        body = json.loads(capsys.readouterr().out)
        assert body == {
            "state": "asking",
            "token": "tok",
            "route": "omarchy theme remove",
            "args": ["Tokyo Night"],
            "target": "Tokyo Night",
            "marker": "(#1)",
        }

    def test_answered_carries_the_marker_so_the_right_panel_clears(self, capsys):
        frames.answered("(#2)", "accepted")
        body = json.loads(capsys.readouterr().out)
        assert body == {"state": "answered", "marker": "(#2)", "outcome": "accepted"}

    def test_a_call_frame_still_carries_no_arguments(self, capsys):
        """The exception is `asking` and nothing else."""
        frames.call("omarchy_clipboard_write", "ok")
        body = json.loads(capsys.readouterr().out)
        assert set(body) == {"state", "tool", "outcome"}
