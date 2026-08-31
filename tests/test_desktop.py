"""The desktop layer.

Split deliberately: the parsing and argument-building is pure and always runs,
while anything that needs a compositor is skipped without one so the suite still
works in CI.
"""

from __future__ import annotations

import os

import pytest

from omarchy_mcp import desktop
from omarchy_mcp.desktop import DesktopError

needs_wayland = pytest.mark.skipif(
    not os.environ.get("WAYLAND_DISPLAY"), reason="needs a running Wayland session"
)


class TestGrimArguments:
    """Targeting is pure, so it is tested without a compositor."""

    def test_region_is_passed_through(self):
        assert desktop._grim_args("region", "", "0,0 800x600") == ["-g", "0,0 800x600"]

    def test_region_without_geometry_is_an_error(self):
        with pytest.raises(DesktopError, match="region"):
            desktop._grim_args("region", "", "")

    def test_named_monitor(self):
        assert desktop._grim_args("monitor", "HDMI-A-1", "") == ["-o", "HDMI-A-1"]

    def test_monitor_without_a_name_is_an_error(self):
        with pytest.raises(DesktopError, match="monitor name"):
            desktop._grim_args("monitor", "", "")

    def test_window_reads_geometry_from_hyprland(self, monkeypatch):
        monkeypatch.setattr(
            desktop, "hyprctl", lambda *a: {"address": "0x1", "at": [10, 20], "size": [800, 600]}
        )
        assert desktop._grim_args("window", "", "") == ["-g", "10,20 800x600"]

    def test_window_with_nothing_focused_is_an_error(self, monkeypatch):
        monkeypatch.setattr(desktop, "hyprctl", lambda *a: {})
        with pytest.raises(DesktopError, match="no window is focused"):
            desktop._grim_args("window", "", "")

    def test_window_with_no_size_is_an_error(self, monkeypatch):
        monkeypatch.setattr(
            desktop, "hyprctl", lambda *a: {"address": "0x1", "at": [0, 0], "size": [0, 0]}
        )
        with pytest.raises(DesktopError, match="no size"):
            desktop._grim_args("window", "", "")

    def test_screen_uses_the_focused_monitor(self, monkeypatch):
        monkeypatch.setattr(
            desktop,
            "hyprctl",
            lambda *a: [{"name": "DP-1", "focused": False}, {"name": "DP-2", "focused": True}],
        )
        assert desktop._grim_args("screen", "", "") == ["-o", "DP-2"]

    def test_screen_falls_back_to_the_first_monitor(self, monkeypatch):
        """Hyprland can report no monitor focused, briefly, during a change."""
        monkeypatch.setattr(desktop, "hyprctl", lambda *a: [{"name": "DP-1", "focused": False}])
        assert desktop._grim_args("screen", "", "") == ["-o", "DP-1"]

    def test_no_monitors_is_an_error(self, monkeypatch):
        monkeypatch.setattr(desktop, "hyprctl", lambda *a: [])
        with pytest.raises(DesktopError, match="no monitors"):
            desktop._grim_args("screen", "", "")


class TestState:
    def test_compacts_hyprland_records(self, monkeypatch):
        """hyprctl's client records carry three dozen fields each. Twenty
        windows of that buries the answer in the question."""
        replies = {
            ("monitors",): [
                {"name": "DP-1", "width": 2560, "height": 1440, "x": 0, "y": 0,
                 "scale": 1.0, "refreshRate": 143.998, "focused": True,
                 "activeWorkspace": {"name": "3"}}
            ],
            ("workspaces",): [
                {"id": 3, "name": "3", "monitor": "DP-1", "windows": 2},
                {"id": 1, "name": "1", "monitor": "DP-1", "windows": 0},
            ],
            ("clients",): [
                {"address": "0x1", "class": "foot", "title": "shell", "pid": 42,
                 "workspace": {"name": "3"}, "monitor": 0, "at": [0, 0],
                 "size": [800, 600], "floating": False, "fullscreen": False,
                 "mapped": True, "swallowing": None, "xwayland": False},
                {"address": "0x2", "class": "ghost", "title": "unmapped",
                 "mapped": False, "workspace": {"name": "3"}},
            ],
            ("activewindow",): {"address": "0x1", "class": "foot", "title": "shell",
                                "workspace": {"name": "3"}},
        }
        monkeypatch.setattr(desktop, "hyprctl", lambda *a: replies[a])

        state = desktop.state()

        assert state["monitors"][0]["resolution"] == "2560x1440"
        assert state["monitors"][0]["refresh_hz"] == 144.0
        # Unmapped windows are not on screen and are noise in the answer.
        assert [w["address"] for w in state["windows"]] == ["0x1"]
        assert state["focused_window"]["class"] == "foot"
        # Sorted, so a listing is stable between calls.
        assert [w["id"] for w in state["workspaces"]] == [1, 3]

    def test_reports_no_focused_window(self, monkeypatch):
        replies = {("monitors",): [], ("workspaces",): [], ("clients",): [],
                   ("activewindow",): {}}
        monkeypatch.setattr(desktop, "hyprctl", lambda *a: replies[a])
        assert desktop.state()["focused_window"] is None


class TestClipboard:
    @pytest.mark.parametrize(
        "stderr",
        [
            "Nothing is copied",          # the actual wl-paste wording
            "No suitable type of content copied",
            "",
        ],
    )
    def test_unreadable_clipboard_reads_as_empty(self, monkeypatch, stderr):
        """wl-paste exits non-zero for an empty clipboard, for content it cannot
        render as text, and for a selection that has gone away. To a caller
        those are one answer -- there is no text -- and none is a failure."""
        import subprocess

        class Reply:
            returncode = 1
            stdout = ""

        Reply.stderr = stderr
        monkeypatch.setattr(subprocess, "run", lambda *a, **k: Reply())
        assert desktop.clipboard_read() == ""

    def test_write_passes_text_as_an_argument(self, monkeypatch):
        import subprocess

        seen = {}

        class Reply:
            returncode = 0
            stdout = ""
            stderr = ""

        def fake(argv, **kwargs):
            seen["argv"] = argv
            return Reply()

        monkeypatch.setattr(subprocess, "run", fake)
        desktop.clipboard_write("--not-a-flag")
        # The `--` matters: text starting with a dash must not become a flag.
        assert seen["argv"] == ["wl-copy", "--", "--not-a-flag"]


@needs_wayland
class TestAgainstARealSession:
    def test_state_reports_this_desktop(self):
        state = desktop.state()
        assert state["monitors"], "a running session has at least one monitor"

    def test_capture_returns_a_png_within_the_cap(self):
        shot = desktop.capture(target="screen", max_width=320)
        assert shot.png[:8] == b"\x89PNG\r\n\x1a\n"
        assert 0 < shot.width <= 320

    def test_capture_does_not_enlarge_a_small_region(self):
        """grim takes logical coordinates and writes physical pixels, so on a
        fractionally scaled monitor the result is larger than the region asked
        for. What matters is that it was not blown up to the cap."""
        scale = desktop.focused_monitor().get("scale") or 1.0
        shot = desktop.capture(target="region", region="0,0 100x50", max_width=1568)
        assert shot.width == pytest.approx(100 * scale, abs=2)
