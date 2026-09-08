"""The activity log, which is the only record of what an agent did that
outlives the daemon.

What this pins, in the order it matters:

- output never reaches the file; arguments do, truncated
- a tool call never blocks on the log, and a full queue loses events *loudly*
- exactly one line per call, however the call ended
- the file is 0600 in a 0700 directory, and it stays bounded
- a broken disk does not break the server
"""

from __future__ import annotations

import json
import logging
import queue
import stat

import pytest

from omarchy_mcp import activity
from omarchy_mcp.activity import Record, Sink
from omarchy_mcp.config import Config
from omarchy_mcp.permissions import Effect, Permissions
from omarchy_mcp.settings import Settings
from omarchy_mcp.stats import Stats
from omarchy_mcp.tools.catalogue import Catalogue

LOG = logging.getLogger("test")


@pytest.fixture(autouse=True)
def quiet_notifications(monkeypatch):
    """No test may raise a real desktop notification."""
    sent: list[tuple[str, str]] = []
    monkeypatch.setattr(activity, "_notify", lambda h, b: sent.append((h, b)))
    return sent


@pytest.fixture
def sink(tmp_path):
    s = Sink(tmp_path / "activity.jsonl", max_bytes=1 << 20, log=LOG)
    s.start()
    yield s
    s.stop()


def lines(path) -> list[dict]:
    return [json.loads(ln) for ln in path.read_text().splitlines() if ln]


class TestTheRecord:
    def test_absent_fields_are_omitted_rather_than_null(self):
        body = Record(tool="omarchy_desktop_state").as_dict()
        assert set(body) == {"ts", "tool", "outcome", "ms"}

    def test_outcome_is_derived_from_the_exit_code(self):
        assert Record(tool="t", exit=0).result == "ok"
        assert Record(tool="t", exit=1).result == "failed"

    def test_a_detached_command_is_not_a_failure(self):
        """A detached run has no exit code by definition; it is not a failure."""
        assert Record(tool="t", exit=None, detached=True).result == "ok"

    def test_a_timeout_outranks_the_exit_code(self):
        assert Record(tool="t", exit=-15, timed_out=True).result == "timed_out"

    def test_an_explicit_outcome_wins(self):
        """`refused` and `not_installed` are not derivable from an exit code:
        nothing ran, so there is none."""
        for outcome in ("refused", "not_installed", "error"):
            assert Record(tool="t", outcome=outcome).result == outcome

    def test_a_long_argument_is_truncated_and_says_by_how_much(self):
        body = Record(tool="t", args=("x" * 500,)).as_dict()
        arg = body["args"][0]
        assert arg.startswith("x" * activity.MAX_ARG_CHARS)
        assert arg.endswith(f"…(+{500 - activity.MAX_ARG_CHARS})")

    def test_an_argument_list_is_capped(self):
        body = Record(tool="t", args=tuple(str(i) for i in range(100))).as_dict()
        assert len(body["args"]) == activity.MAX_ARGS


class TestTheLine:
    def test_an_ordinary_line_is_left_alone(self):
        body = Record(tool="omarchy_run", route="omarchy theme set", args=("tokyo-night",))
        assert json.loads(activity.line_of(body.as_dict()))["args"] == ["tokyo-night"]

    def test_an_oversized_line_elides_its_arguments(self):
        body = Record(tool="t", args=tuple("y" * 100 for _ in range(activity.MAX_ARGS)))
        line = activity.line_of(body.as_dict())
        assert len(line.encode()) <= activity.MAX_LINE_B

    def test_a_huge_argument_cannot_displace_the_history_around_it(self):
        body = {"ts": "t", "tool": "t", "args": ["z" * 4000], "outcome": "ok", "ms": 1}
        line = activity.line_of(body)
        assert len(line.encode()) <= activity.MAX_LINE_B
        assert json.loads(line)["args"][0].startswith("z" * activity.HARD_ARG_CHARS)


class TestWriting:
    def test_a_record_reaches_the_file(self, sink):
        sink.append(Record(tool="omarchy_run", route="omarchy theme set").as_dict())
        sink.stop()

        (written,) = lines(sink.path)
        assert written["tool"] == "omarchy_run"
        assert written["route"] == "omarchy theme set"

    def test_the_file_is_0600_in_a_0700_directory(self, tmp_path):
        """It states what an agent did, and its arguments can be text the user
        copied."""
        s = Sink(tmp_path / "sub" / "activity.jsonl", max_bytes=1 << 20, log=LOG)
        s.start()
        s.append(Record(tool="t").as_dict())
        s.stop()

        assert stat.S_IMODE(s.path.stat().st_mode) == 0o600
        assert stat.S_IMODE(s.path.parent.stat().st_mode) == 0o700

    def test_the_file_rotates_and_keeps_one_generation(self, tmp_path):
        s = Sink(tmp_path / "activity.jsonl", max_bytes=200, log=LOG)
        s.start()
        for i in range(40):
            s.append(Record(tool=f"tool{i}").as_dict())
        s.stop()

        assert activity.rotated(s.path).exists()
        assert s.path.stat().st_size < 200 * 3

    def test_appends_go_to_the_new_file_after_a_rotation(self, tmp_path):
        s = Sink(tmp_path / "activity.jsonl", max_bytes=120, log=LOG)
        s.start()
        s.append(Record(tool="first").as_dict())
        s.append(Record(tool="second").as_dict())
        s.stop()

        assert [r["tool"] for r in lines(activity.rotated(s.path))] == ["first"]
        assert [r["tool"] for r in lines(s.path)] == ["second"]


class TestLosingEvents:
    """A full queue must lose events rather than block a tool call -- and must
    never lose them quietly."""

    def test_append_never_blocks_or_raises_when_full(self, tmp_path):
        s = Sink(tmp_path / "activity.jsonl", max_bytes=1 << 20, log=LOG)
        s._q = queue.Queue(maxsize=2)
        for _ in range(50):
            s.append(Record(tool="t").as_dict())  # no thread draining it

    def test_a_gap_is_written_into_the_file(self, tmp_path):
        s = Sink(tmp_path / "activity.jsonl", max_bytes=1 << 20, log=LOG)
        s._q = queue.Queue(maxsize=2)
        for _ in range(5):
            s.append(Record(tool="lost").as_dict())

        s.start()
        s.stop()

        written = lines(s.path)
        assert _dropped(written)["n"] == 3
        assert [r["tool"] for r in written if "tool" in r] == ["lost", "lost"]


def _dropped(written: list[dict]) -> dict:
    return next(r for r in written if r.get("event") == "dropped")


class TestABrokenDisk:
    """The daemon's job is not the log. A log that cannot be written says so and
    gets out of the way."""

    def test_a_write_failure_does_not_reach_the_caller(self, tmp_path, monkeypatch):
        s = Sink(tmp_path / "activity.jsonl", max_bytes=1 << 20, log=LOG)
        monkeypatch.setattr(Sink, "_open", lambda self: (_ for _ in ()).throw(OSError("nope")))
        s.start()
        s.append(Record(tool="t").as_dict())
        s.append(Record(tool="t").as_dict())
        s.stop()  # no exception anywhere

    def test_the_user_is_told_once(self, tmp_path, monkeypatch, quiet_notifications):
        s = Sink(tmp_path / "activity.jsonl", max_bytes=1 << 20, log=LOG)
        monkeypatch.setattr(Sink, "_open", lambda self: (_ for _ in ()).throw(OSError("nope")))
        s.start()
        for _ in range(5):
            s.append(Record(tool="t").as_dict())
        s.stop()

        assert len(quiet_notifications) == 1


class TestTheWriterLifecycle:
    def test_switched_off_yields_no_sink_and_writes_nothing(self, tmp_path, monkeypatch):
        monkeypatch.setattr(activity, "STATE_DIR", tmp_path)
        with activity.writer(Config(activity=False), LOG) as sink:
            assert sink is None
        assert not any(tmp_path.iterdir())

    def test_a_session_is_bracketed_by_started_and_stopped(self, tmp_path, monkeypatch):
        monkeypatch.setattr(activity, "STATE_DIR", tmp_path)
        with activity.writer(Config(), LOG) as sink:
            assert sink is not None, "logging is on, so there is a sink"
            sink.append(Record(tool="omarchy_run").as_dict())

        written = lines(tmp_path / "activity.jsonl")
        assert written[0]["event"] == "started"
        assert written[-1]["event"] == "stopped"

    def test_the_log_is_always_inside_the_state_directory(self, tmp_path, monkeypatch):
        """A log in the plugin directory would reload the shell once per call."""
        monkeypatch.setattr(activity, "STATE_DIR", tmp_path)
        path = activity.path_for(Config(activity_file="elsewhere.jsonl"))
        assert path == tmp_path / "elsewhere.jsonl"


class TestReadingItBack:
    def test_tail_reads_across_a_rotation_oldest_first(self, tmp_path):
        """One generation is kept, so a tail spanning a rotation reads both."""
        s = Sink(tmp_path / "activity.jsonl", max_bytes=200, log=LOG)
        s.start()
        for i in range(4):
            s.append(Record(tool=f"tool{i}").as_dict())
        s.stop()

        assert activity.rotated(s.path).exists()
        assert [r["tool"] for r in activity.tail(10, s.path)] == [f"tool{i}" for i in range(4)]

    def test_tail_of_a_missing_file_is_empty(self, tmp_path):
        assert activity.tail(10, tmp_path / "nothing.jsonl") == []

    def test_a_corrupt_line_is_skipped_rather_than_fatal(self, tmp_path):
        path = tmp_path / "activity.jsonl"
        path.write_text('{"ts":"x","tool":"good"}\nnot json at all\n')
        assert [r["tool"] for r in activity.tail(10, path)] == ["good"]

    def test_tail_json_is_one_array_of_whole_records(self, tmp_path, monkeypatch, capsys):
        """What the bar panel reads. It parses once, or not at all."""
        from omarchy_mcp import __main__ as entry

        monkeypatch.setattr(activity, "STATE_DIR", tmp_path)
        s = Sink(tmp_path / "activity.jsonl", max_bytes=1 << 20, log=LOG)
        s.start()
        s.append(Record(tool="omarchy_theme", target="Tokyo Night", exit=0).as_dict())
        s.append(Record(tool="omarchy_run", route="omarchy theme set", outcome="refused").as_dict())
        s.stop()

        assert entry.main(["--tail", "5", "--json"]) == 0
        body = json.loads(capsys.readouterr().out)
        assert body["activity"] is True
        assert [r["tool"] for r in body["records"]] == ["omarchy_theme", "omarchy_run"]
        assert body["records"][1]["outcome"] == "refused"

    def test_tail_json_says_when_the_log_is_off(self, tmp_path, monkeypatch, capsys):
        """Otherwise an empty list reads as "nothing happened" to the panel."""
        from omarchy_mcp import __main__ as entry, config as config_module

        monkeypatch.setattr(activity, "STATE_DIR", tmp_path)
        monkeypatch.setattr(config_module, "load", lambda: Config(activity=False))

        assert entry.main(["--tail", "5", "--json"]) == 0
        body = json.loads(capsys.readouterr().out)
        assert body == {"activity": False, "records": []}

    def test_tail_without_json_still_renders_for_a_person(self, tmp_path, monkeypatch, capsys):
        from omarchy_mcp import __main__ as entry

        monkeypatch.setattr(activity, "STATE_DIR", tmp_path)
        s = Sink(tmp_path / "activity.jsonl", max_bytes=1 << 20, log=LOG)
        s.start()
        s.append(Record(tool="omarchy_theme", target="Tokyo Night", exit=0, ms=249).as_dict())
        s.stop()

        assert entry.main(["--tail", "5"]) == 0
        out = capsys.readouterr().out
        assert "omarchy_theme" in out and "Tokyo Night" in out
        assert not out.lstrip().startswith("[")

    def test_a_record_renders_as_one_readable_line(self):
        body = Record(
            tool="omarchy_run",
            route="omarchy theme set",
            args=("tokyo-night",),
            target="Tokyo Night",
            tier="guarded",
            consent="accepted",
            exit=0,
            ms=142,
        ).as_dict()
        rendered = activity.render(body)
        assert "omarchy theme set" in rendered
        assert "Tokyo Night" in rendered
        assert "consent=accepted" in rendered
        assert "142ms" in rendered


class TestStatsAsTheSeam:
    """One record per call, whatever the call does."""

    def test_a_call_is_recorded_once(self, sink):
        stats = Stats(sink=sink)
        with stats.call("omarchy_run") as rec:
            rec.route = "omarchy theme current"
        sink.stop()

        assert len(lines(sink.path)) == 1
        assert stats.snapshot()["calls"] == 1

    def test_a_raising_tool_is_recorded_as_an_error_and_still_raises(self, sink):
        stats = Stats(sink=sink)
        with pytest.raises(ZeroDivisionError):
            with stats.call("omarchy_run"):
                _ = 1 / 0
        sink.stop()

        (written,) = lines(sink.path)
        assert written["outcome"] == "error"
        assert stats.snapshot()["failures"] == 1

    def test_a_refusal_is_a_failure_rather_than_a_success(self, sink):
        """omarchy_run used to count before the gate ran, so every refusal was
        recorded as a successful call."""
        stats = Stats(sink=sink)
        with stats.call("omarchy_run") as rec:
            rec.route = "omarchy system reboot"
            rec.outcome = "refused"
            rec.tier = "guarded"
        sink.stop()

        (written,) = lines(sink.path)
        assert written["outcome"] == "refused"
        assert written["tier"] == "guarded"
        assert stats.snapshot()["failures"] == 1

    def test_a_call_is_timed(self, sink):
        stats = Stats(sink=sink)
        with stats.call("omarchy_run"):
            pass
        sink.stop()

        assert "ms" in lines(sink.path)[0]

    def test_no_sink_means_counters_only(self):
        stats = Stats()
        with stats.call("omarchy_run") as rec:
            rec.route = "omarchy theme current"
        assert stats.snapshot()["last_route"] == "omarchy theme current"


class TestWhatTheToolsWrite:
    """The properties that only hold end to end: what reaches the file when a
    real tool runs, and -- more importantly -- what does not."""

    @pytest.fixture
    def tools(self, monkeypatch, sink):
        from mcp.server.mcpserver import MCPServer

        from omarchy_mcp import desktop as desktop_layer
        from omarchy_mcp.tools import desktop as desktop_tools, generic

        monkeypatch.setattr(desktop_layer, "ocr", lambda **kw: "SECRET on the screen")
        monkeypatch.setattr(desktop_layer, "clipboard_read", lambda **kw: "SECRET copied")
        monkeypatch.setattr(desktop_layer, "clipboard_write", lambda text: None)

        mcp = MCPServer(name="t")
        stats = Stats(sink=sink)
        catalogue = Catalogue()
        settings = Settings(Config(), Permissions(guarded_default=Effect.DENY))
        desktop_tools.register(catalogue, settings, LOG, stats)
        generic.register(catalogue, settings, LOG, stats)
        catalogue.apply(mcp, Config())
        return mcp

    async def _log_of(self, mcp, sink, name, args):
        await mcp.call_tool(name, args)
        sink.stop()
        return sink.path.read_text()

    @pytest.mark.anyio
    async def test_ocr_text_never_reaches_the_file(self, tools, sink):
        """What is on the screen is the user's, not the agent's action."""
        written = await self._log_of(tools, sink, "omarchy_screen_text", {"target": "screen"})
        assert "SECRET" not in written
        assert '"tool": "omarchy_screen_text"' in written

    @pytest.mark.anyio
    async def test_clipboard_contents_never_reach_the_file(self, tools, sink):
        written = await self._log_of(tools, sink, "omarchy_clipboard_read", {})
        assert "SECRET" not in written

    @pytest.mark.anyio
    async def test_what_was_put_on_the_clipboard_does(self, tools, sink):
        """This one is the action: it is what a user would come back to find."""
        written = await self._log_of(
            tools, sink, "omarchy_clipboard_write", {"text": "ssh-rsa AAAA"}
        )
        assert json.loads(written.splitlines()[0])["args"] == ["ssh-rsa AAAA"]

    @pytest.mark.anyio
    async def test_a_refused_run_is_logged_as_refused(self, tools, sink):
        """A guarded route that was blocked is more interesting than a safe one
        that ran, and it must not be recorded as a success."""
        written = await self._log_of(
            tools, sink, "omarchy_run", {"route": "omarchy channel current"}
        )
        record = json.loads(written.splitlines()[0])
        assert record["outcome"] == "refused"
        assert record["tier"] == "guarded"
        assert record["route"] == "omarchy channel current"

    @pytest.mark.anyio
    async def test_a_route_that_does_not_exist_is_an_error_not_a_failure(self, tools, sink):
        written = await self._log_of(tools, sink, "omarchy_run", {"route": "omarchy nonsense"})
        assert json.loads(written.splitlines()[0])["outcome"] == "error"


class TestClosingOnShutdown:
    """Nothing at the end of `main` runs: uvicorn restores the default signal
    handler and re-raises, so the process dies by signal (F28). The lifespan
    shutdown is the last hook this daemon actually gets."""

    @pytest.mark.anyio
    async def test_the_lifespan_shutdown_closes_the_log(self, tmp_path):
        s = Sink(tmp_path / "activity.jsonl", max_bytes=1 << 20, log=LOG)
        s.start()
        s.append(Record(tool="omarchy_run").as_dict())

        await _lifespan(activity.Closing(_app(), s))

        written = lines(s.path)
        assert written[-1]["event"] == "stopped"
        assert s._thread is None, "the writer is stopped, not left running"

    @pytest.mark.anyio
    async def test_ordinary_traffic_passes_straight_through(self, tmp_path):
        s = Sink(tmp_path / "activity.jsonl", max_bytes=1 << 20, log=LOG)
        seen: list = []

        async def app(scope, receive, send):
            seen.append(scope["type"])

        await activity.Closing(app, s)({"type": "http"}, None, None)
        assert seen == ["http"]

    def test_closing_twice_writes_one_marker(self, tmp_path):
        """The contextmanager closes too, for anything driven without an ASGI
        server. Whichever runs first wins."""
        s = Sink(tmp_path / "activity.jsonl", max_bytes=1 << 20, log=LOG)
        s.start()
        s.close()
        s.close()

        assert [r["event"] for r in lines(s.path)] == ["stopped"]


def _app():
    async def app(scope, receive, send):
        await send({"type": "lifespan.startup.complete"})
        await send({"type": "lifespan.shutdown.complete"})

    return app


async def _lifespan(app):
    async def send(message):
        pass

    await app({"type": "lifespan"}, None, send)


class TestUnclosedSessions:
    """The daemon cannot always write its own `stopped`.

    On `omarchy restart shell` the whole shell is torn down and Quickshell reaps
    its children within milliseconds -- measured, and not something `Service.qml`
    can lengthen (ROADMAP F30). So the log legitimately holds a `started` with
    another `started` after it and nothing between, and the reader is what has to
    make sense of that.
    """

    def _marked(self, *events):
        records = [{"ts": f"2026-09-03T10:00:0{i}+02:00", "event": e} for i, e in enumerate(events)]
        return [bool(r.get("unclosed")) for r in activity.mark_unclosed(records)]

    def test_a_session_with_no_end_is_marked(self):
        assert self._marked("started", "started") == [True, False]

    def test_a_clean_session_is_not(self):
        assert self._marked("started", "stopped", "started") == [False, False, False]

    def test_the_last_session_is_never_marked(self):
        """It is the one that is probably still running. Calling it unclosed
        would be a lie in the other direction."""
        assert self._marked("started", "stopped", "started")[-1] is False
        assert self._marked("started")[0] is False

    def test_several_in_a_row(self):
        assert self._marked("started", "started", "started") == [True, True, False]

    def test_calls_between_them_do_not_confuse_it(self):
        records = [
            {"ts": "2026-09-03T10:00:00+02:00", "event": "started"},
            {"ts": "2026-09-03T10:00:01+02:00", "tool": "omarchy_theme", "outcome": "ok"},
            {"ts": "2026-09-03T10:00:02+02:00", "event": "started"},
        ]
        marked = activity.mark_unclosed(records)
        assert marked[0]["unclosed"] is True
        assert "unclosed" not in marked[1]
        assert "unclosed" not in marked[2]

    def test_nothing_is_written_to_disk(self, tmp_path):
        """Inventing a `stopped` row with a guessed timestamp would put a guess
        in an audit trail, which is the one place it must not go."""
        path = tmp_path / "activity.jsonl"
        path.write_text(
            '{"ts": "2026-09-03T10:00:00+02:00", "event": "started"}\n'
            '{"ts": "2026-09-03T10:00:05+02:00", "event": "started"}\n'
        )
        before = path.read_text()

        records = activity.tail(10, path)
        assert records[0]["unclosed"] is True
        assert path.read_text() == before, "the log is what happened, not what we inferred"

    def test_the_rendered_line_says_what_is_known_and_no_more(self):
        record = {"ts": "2026-09-03T10:00:00+02:00", "event": "started", "unclosed": True}
        line = activity.render(record)
        assert "no recorded end" in line
        assert "unclosed=True" not in line, "the derived flag is not a field to print"


class TestHowLoudARecordIs:
    """The Log tab's level column. Derived from the record's kind rather than
    stored, so an old log reads the same as a new one and the terminal cannot
    disagree with the panel about what counts as wrong."""

    def test_a_call_that_worked_is_information(self):
        assert activity.level(Record(tool="omarchy_theme", exit=0).as_dict()) == "i"

    def test_a_refusal_is_a_warning_rather_than_an_error(self):
        """The policy doing its job. Loud enough to find, not a fault."""
        assert activity.level(Record(tool="omarchy_run", outcome="refused").as_dict()) == "w"

    def test_a_failed_call_is_a_warning(self):
        assert activity.level(Record(tool="omarchy_run", exit=2).as_dict()) == "w"

    def test_an_unanswered_question_is_a_warning(self):
        assert activity.level(Record(tool="omarchy_run", timed_out=True).as_dict()) == "w"

    def test_a_missing_dependency_is_an_error(self):
        """A defect on the desktop, not something an agent did."""
        body = Record(tool="omarchy_screenshot", outcome="not_installed").as_dict()
        assert activity.level(body) == "e"

    def test_a_raised_exception_is_an_error(self):
        assert activity.level(Record(tool="omarchy_run", outcome="error").as_dict()) == "e"

    def test_an_ordinary_event_is_information(self):
        for event in ("started", "stopped", "reloaded", "acknowledged", "permission"):
            assert activity.level({"ts": "x", "event": event}) == "i", event

    def test_a_rejected_file_is_an_error(self):
        """The daemon is running something other than what the file says."""
        assert activity.level({"ts": "x", "event": "config_rejected"}) == "e"
        assert activity.level({"ts": "x", "event": "permissions_rejected"}) == "e"

    def test_dropped_records_are_an_error(self):
        assert activity.level({"ts": "x", "event": "dropped", "n": 12}) == "e"

    def test_a_session_with_no_recorded_end_is_a_warning(self):
        """Expected after `omarchy restart shell`, so not an error -- F30."""
        assert activity.level({"ts": "x", "event": "started", "unclosed": True}) == "w"
        assert activity.level({"ts": "x", "event": "started"}) == "i"

    def test_the_json_tail_carries_it(self, tmp_path, monkeypatch, capsys):
        from omarchy_mcp import __main__ as entry

        monkeypatch.setattr(activity, "STATE_DIR", tmp_path)
        sink = Sink(tmp_path / "activity.jsonl", max_bytes=1 << 20, log=LOG)
        sink.start()
        sink.append(Record(tool="omarchy_theme", exit=0).as_dict())
        sink.append(Record(tool="omarchy_run", outcome="refused").as_dict())
        sink.stop()

        assert entry.main(["--tail", "5", "--json"]) == 0
        records = json.loads(capsys.readouterr().out)["records"]
        assert [r["level"] for r in records] == ["i", "w"]
        assert records[0]["tool"] == "omarchy_theme", "the record is otherwise untouched"

    def test_it_is_not_written_to_the_file(self, tmp_path):
        """A derivation, not a field. The log's shape does not move for a UI."""
        path = tmp_path / "activity.jsonl"
        sink = Sink(path, max_bytes=1 << 20, log=LOG)
        sink.start()
        sink.append(Record(tool="omarchy_theme", exit=0).as_dict())
        sink.stop()
        assert "level" not in path.read_text()

    def test_the_rendered_line_is_unchanged_by_it(self):
        """`--tail` without --json is a person's view and predates this."""
        line = activity.render({"ts": "2026-01-05T10:04:00+00:00", "event": "started"})
        assert "level" not in line
