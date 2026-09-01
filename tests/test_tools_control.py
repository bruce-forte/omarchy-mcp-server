"""Argument shaping for the frequently-used tools.

These tools exist to save a search round trip, so the thing worth testing is
that they translate a caller's intent into the arguments each route actually
accepts -- which the routes disagree about.
"""

from __future__ import annotations

import json

import pytest

from omarchy_mcp.tools import control


class TestToggleStates:
    """The toggle routes are not consistent with each other, and the mapping is
    read from the registry rather than hardcoded so it cannot drift."""

    def test_on_off_routes_use_on_and_off(self):
        assert control.state_argument("omarchy toggle bar", "on") == "on"
        assert control.state_argument("omarchy toggle bar", "off") == "off"

    def test_idle_uses_its_own_words(self):
        assert control.state_argument("omarchy toggle idle", "on") == "stay-awake"
        assert control.state_argument("omarchy toggle idle", "off") == "allow-idle"

    def test_idle_supports_status(self):
        assert control.state_argument("omarchy toggle idle", "status") == "status"

    def test_argumentless_routes_toggle_with_no_argument(self):
        """`omarchy toggle screensaver` takes nothing; passing 'toggle' would
        be an error, and passing 'on' is not possible at all."""
        assert control.state_argument("omarchy toggle screensaver", "toggle") == ""
        assert control.state_argument("omarchy toggle screensaver", "on") is None
        assert control.state_argument("omarchy toggle notification silencing", "on") is None

    def test_nightlight_only_toggles(self):
        assert control.state_argument("omarchy toggle nightlight", "toggle") == ""
        assert control.state_argument("omarchy toggle nightlight", "on") is None

    def test_every_named_toggle_exists_in_omarchy(self, commands):
        """Catches a route Omarchy has renamed out from under the tool."""
        for feature, route in control.TOGGLES.items():
            assert route in commands, f"{feature} -> {route} no longer exists"

    def test_every_named_toggle_can_at_least_toggle(self):
        for route in control.TOGGLES.values():
            assert control.state_argument(route, "toggle") is not None


class TestVolumeSteps:
    @pytest.mark.parametrize("value", ["+10", "-5", "+100"])
    def test_signed_steps_accepted(self, value):
        assert control._is_step(value)

    @pytest.mark.parametrize("value", ["10", "raise", "+", "-", "+abc", "", "++5"])
    def test_everything_else_rejected(self, value):
        assert not control._is_step(value)


class TestToolBehaviour:
    """Exercised through the registered tools, so the wiring is covered too."""

    @pytest.fixture
    def tools(self, monkeypatch):
        import logging

        from mcp.server.mcpserver import MCPServer

        from omarchy_mcp.config import Config
        from omarchy_mcp.settings import Settings
        from omarchy_mcp.stats import Stats

        ran = []

        async def fake_run(route, args, **kwargs):
            ran.append((route, args))
            return json.dumps({"command": route, "args": args})

        monkeypatch.setattr(control, "run_route", fake_run)
        mcp = MCPServer(name="t")
        control.register(mcp, Settings(Config()), logging.getLogger("t"), Stats())
        return mcp, ran

    async def _call(self, mcp, name, args):
        """call_tool returns a CallToolResult; the tool's own JSON is the text
        of its first content block."""
        result = await mcp.call_tool(name, args)
        return json.loads(result.content[0].text)

    @pytest.mark.anyio
    async def test_theme_set_requires_a_name(self, tools):
        mcp, _ = tools
        assert "error" in await self._call(mcp, "omarchy_theme", {"action": "set"})

    @pytest.mark.anyio
    async def test_unknown_theme_action_is_rejected(self, tools):
        mcp, _ = tools
        assert "error" in await self._call(mcp, "omarchy_theme", {"action": "delete"})

    @pytest.mark.anyio
    async def test_volume_rejects_a_bare_number(self, tools):
        """'10' is ambiguous: raise by ten, or set to ten? Omarchy's own command
        takes a signed step, so an unsigned one is a mistake worth catching."""
        mcp, _ = tools
        assert "error" in await self._call(
            mcp, "omarchy_audio", {"action": "volume", "level": "10"}
        )

    @pytest.mark.anyio
    async def test_volume_accepts_a_step(self, tools):
        mcp, ran = tools
        await self._call(mcp, "omarchy_audio", {"action": "volume", "level": "+10"})
        assert ran[-1] == ("omarchy audio output volume", ["+10"])

    @pytest.mark.anyio
    async def test_mute_maps_to_mute_toggle(self, tools):
        mcp, ran = tools
        await self._call(mcp, "omarchy_audio", {"action": "mute"})
        assert ran[-1] == ("omarchy audio output volume", ["mute-toggle"])

    @pytest.mark.anyio
    async def test_unknown_toggle_lists_the_known_ones(self, tools):
        mcp, _ = tools
        reply = await self._call(mcp, "omarchy_toggle", {"feature": "wifi"})
        assert "known" in reply and "idle" in reply["known"]

    @pytest.mark.anyio
    async def test_toggle_that_cannot_be_forced_says_so(self, tools):
        mcp, _ = tools
        reply = await self._call(
            mcp, "omarchy_toggle", {"feature": "screensaver", "state": "on"}
        )
        assert "only toggles" in reply["error"]

    @pytest.mark.anyio
    async def test_media_rejects_an_unknown_action(self, tools):
        mcp, _ = tools
        assert "error" in await self._call(mcp, "omarchy_media", {"action": "rewind"})

    @pytest.mark.anyio
    async def test_launch_editor_needs_a_path(self, tools):
        mcp, _ = tools
        assert "error" in await self._call(mcp, "omarchy_launch", {"what": "editor"})

    @pytest.mark.anyio
    async def test_launch_browser_without_a_url_is_fine(self, tools):
        mcp, ran = tools
        await self._call(mcp, "omarchy_launch", {"what": "browser"})
        assert ran[-1] == ("omarchy launch browser", [])
