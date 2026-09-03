"""Resolution: naming the target before the command runs.

Two properties carry the weight here and both are load-bearing for N4's
approval prompt:

- what resolves is exactly what Omarchy itself would accept, so a name this
  module takes cannot be a name the command then rejects;
- what does not resolve is refused, and the refusal says whether the name was
  wrong or whether nothing could be checked.

The candidate lists come from `tests/fixtures`, pinned in `conftest`.
"""

from __future__ import annotations

import json
import logging
import shutil

import pytest

from omarchy_mcp import resolve
from omarchy_mcp.config import Config
from omarchy_mcp.permissions import Permissions
from omarchy_mcp.stats import Stats
from omarchy_mcp.tools._shared import run_route

#: The unpinned source functions, captured at import time -- `conftest` replaces
#: them for every test, and one test below wants the real code path back.
_real_monitors = resolve._monitors
_real_themes = resolve._themes


class TestSlug:
    """`omarchy-theme-set` lowercases and dashes before it looks anything up."""

    def test_case_and_spaces_do_not_matter(self):
        assert resolve.slug("Tokyo Night") == resolve.slug("tokyo night") == "tokyo-night"

    def test_placeholders_are_stripped_as_omarchy_strips_them(self):
        assert resolve.slug("Tokyo <b>Night</b>") == "tokyo-night"


class TestTheme:
    def test_the_display_name_resolves(self):
        assert resolve.theme("Tokyo Night").value == "Tokyo Night"

    def test_case_and_spacing_resolve_to_the_installed_name(self):
        target = resolve.theme("tokyo night")
        assert target.value == "Tokyo Night"
        assert target.label == "Tokyo Night"

    def test_a_near_miss_is_refused_with_the_near_misses_named(self):
        with pytest.raises(resolve.Unresolvable) as caught:
            resolve.theme("Tokoy Night")
        assert "Tokyo Night" in caught.value.near
        assert caught.value.source_unavailable is False

    def test_a_prefix_is_not_a_match(self):
        """Convenient, and wrong: it applies a theme the caller did not name."""
        with pytest.raises(resolve.Unresolvable):
            resolve.theme("tokyo")

    def test_a_source_that_cannot_answer_is_not_a_missing_theme(self, monkeypatch):
        def down():
            raise resolve.Unresolvable(
                "theme", "could not list themes: exited 1", source_unavailable=True
            )

        monkeypatch.setattr(resolve, "_themes", down)
        with pytest.raises(resolve.Unresolvable) as caught:
            resolve.theme("Tokyo Night")
        assert caught.value.source_unavailable is True
        # Nothing to suggest: the list was never obtained.
        assert caught.value.near == ()


class TestMonitor:
    def test_a_connected_output_resolves_and_is_labelled(self):
        target = resolve.monitor("DP-1")
        assert target.value == "DP-1"
        assert target.label == "DP-1 (Dell Inc. DELL U2723QE)"

    def test_a_wrong_name_is_refused_with_the_connected_ones(self):
        with pytest.raises(resolve.Unresolvable) as caught:
            resolve.monitor("HDMI-1")
        assert "HDMI-A-1" in caught.value.near
        assert "DP-1" in caught.value.message

    def test_no_compositor_reports_that_rather_than_a_missing_monitor(self, monkeypatch):
        from omarchy_mcp import desktop

        def boom(*_args):
            raise desktop.DesktopError("`hyprctl` is not on PATH; is Hyprland running?")

        monkeypatch.setattr(desktop, "hyprctl", boom)
        monkeypatch.setattr(resolve, "_monitors", _real_monitors)
        with pytest.raises(resolve.Unresolvable) as caught:
            resolve.monitor("DP-1")
        assert caught.value.source_unavailable is True



class TestPaths:
    def test_a_tilde_is_expanded_because_no_shell_will_do_it(self, tmp_path, monkeypatch):
        monkeypatch.setenv("HOME", str(tmp_path))
        (tmp_path / "wall.png").write_bytes(b"png")
        target = resolve.existing_file("~/wall.png", "image")
        assert target.value == str(tmp_path / "wall.png")

    def test_a_background_that_is_not_there_is_refused(self, tmp_path):
        with pytest.raises(resolve.Unresolvable, match="no such file"):
            resolve.existing_file(str(tmp_path / "nope.png"), "image")

    def test_a_directory_is_not_a_background(self, tmp_path):
        with pytest.raises(resolve.Unresolvable, match="not a file"):
            resolve.existing_file(str(tmp_path), "image")

    def test_a_relative_path_is_refused_rather_than_guessed_at(self):
        """It would resolve against the daemon's cwd, which is nowhere the user is."""
        with pytest.raises(resolve.Unresolvable, match="relative"):
            resolve.existing_file("wall.png", "image")

    def test_an_editor_path_need_not_exist_yet(self, tmp_path):
        target = resolve.writable_path(str(tmp_path / "new.md"))
        assert target.value == str(tmp_path / "new.md")
        assert target.label == "new.md"

    def test_an_editor_path_in_a_missing_directory_is_refused(self, tmp_path):
        with pytest.raises(resolve.Unresolvable, match="no such directory"):
            resolve.writable_path(str(tmp_path / "nope" / "new.md"))


class TestUrl:
    @pytest.mark.parametrize("value", ["https://example.com", "http://localhost:1234/x"])
    def test_http_urls_pass(self, value):
        assert resolve.url(value).value == value

    @pytest.mark.parametrize(
        "value", ["file:///etc/shadow", "javascript:alert(1)", "example.com"]
    )
    def test_anything_else_is_refused(self, value):
        with pytest.raises(resolve.Unresolvable):
            resolve.url(value)


class TestRouteTable:
    def test_an_unlisted_route_passes_its_arguments_through_untouched(self):
        call = resolve.resolve_call("omarchy pkg aur add", ["some-aur-package"])
        assert call.args == ["some-aur-package"]
        assert call.target is None

    def test_a_theme_route_rewrites_the_argument_to_the_installed_name(self):
        call = resolve.resolve_call("omarchy theme set", ["tokyo night"])
        assert call.args == ["Tokyo Night"]
        assert call.target.kind == "theme"

    def test_theme_remove_with_no_name_still_opens_the_picker(self):
        """A picker is the user choosing, which is what consent is for."""
        assert resolve.resolve_call("omarchy theme remove", []).args == []

    def test_the_monitor_flag_is_found_wherever_it_sits(self):
        call = resolve.resolve_call("omarchy brightness display", ["--monitor", "DP-1", "+10%"])
        assert call.args == ["--monitor", "DP-1", "+10%"]
        assert call.target.value == "DP-1"

    def test_a_wrong_monitor_flag_refuses_the_whole_call(self):
        with pytest.raises(resolve.Unresolvable):
            resolve.resolve_call("omarchy brightness display", ["--monitor", "HDMI-1"])

    def test_brightness_without_the_flag_resolves_nothing(self):
        call = resolve.resolve_call("omarchy brightness display", ["+10%"])
        assert call.args == ["+10%"] and call.target is None

    def test_an_editor_switch_is_not_mistaken_for_the_path(self, tmp_path):
        call = resolve.resolve_call(
            "omarchy launch editor", ["--inline", str(tmp_path / "new.md")]
        )
        assert call.args == ["--inline", str(tmp_path / "new.md")]

    def test_launching_a_browser_with_no_url_is_fine(self):
        assert resolve.resolve_call("omarchy launch browser", []).args == []


class TestTheGate:
    """Both tool paths funnel through resolution; neither can skip it."""

    def _run(self, spawned):
        def fake_execute(argv, **_kw):
            spawned.append(argv)
            from omarchy_mcp.execute import Result

            return Result(exit_code=0, stdout="", stderr="", timed_out=False, detached=False)

        return fake_execute

    @pytest.mark.anyio
    async def test_a_curated_tool_refuses_before_spawning_anything(self, monkeypatch):
        spawned: list[list[str]] = []
        from omarchy_mcp import execute

        monkeypatch.setattr(execute, "run", self._run(spawned))

        payload = json.loads(
            await run_route(
                "omarchy theme set",
                ["Tokoy Night"],
                config=Config(),
                perms=Permissions(),
                stats=Stats(),
                log=logging.getLogger("test"),
                tool="omarchy_theme",
            )
        )
        assert payload["unresolved"] == "theme"
        assert payload["reason"] == "not_found"
        assert payload["did_you_mean"] == ["Tokyo Night"]
        assert spawned == [], "nothing may be executed for an unresolvable target"

    @pytest.mark.anyio
    async def test_a_resolved_call_reports_what_it_acted_on(self, monkeypatch):
        spawned: list[list[str]] = []
        from omarchy_mcp import execute

        monkeypatch.setattr(execute, "run", self._run(spawned))

        payload = json.loads(
            await run_route(
                "omarchy theme set",
                ["tokyo night"],
                config=Config(),
                perms=Permissions(),
                stats=Stats(),
                log=logging.getLogger("test"),
                tool="omarchy_theme",
            )
        )
        assert payload["target"] == "Tokyo Night"
        assert spawned == [["omarchy", "theme", "set", "Tokyo Night"]]


@pytest.mark.needs_omarchy
@pytest.mark.skipif(
    shutil.which("omarchy-theme-list") is None, reason="no installed Omarchy to compare against"
)
def test_the_theme_snapshot_still_resembles_this_machine(themes, monkeypatch):
    """The one test that notices the fixture rotting.

    Everything else reads the snapshot, which is what makes the suite run in
    CI. That is also what lets the snapshot drift away from any real Omarchy
    without anything saying so.
    """
    monkeypatch.setattr(resolve, "_themes", _real_themes)
    live = resolve._themes()
    assert live, "`omarchy theme list` returned nothing on a machine that has it"
    shared = {resolve.slug(t) for t in themes} & {resolve.slug(t) for t in live}
    assert shared, (
        "tests/fixtures/themes.txt shares no theme with this machine; "
        "refresh it with `omarchy theme list > tests/fixtures/themes.txt`"
    )
