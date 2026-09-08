"""The perception tools, which reach the compositor rather than a route.

They do not pass through `run_route`, so they resolve a monitor name
themselves. The thing worth pinning is that a name which is not connected is
refused here rather than handed to grim, and that a name is only resolved where
it means anything.
"""

from __future__ import annotations

import json
import logging

import pytest

from omarchy_mcp import desktop as desktop_layer
from omarchy_mcp.config import Config
from omarchy_mcp.settings import Settings
from omarchy_mcp.stats import Stats
from omarchy_mcp.tools import desktop as desktop_tools
from omarchy_mcp.tools.catalogue import Catalogue


@pytest.fixture
def tools(monkeypatch):
    """The registered tools, with the compositor replaced by a recorder."""
    from mcp.server.mcpserver import MCPServer

    captured: list[dict] = []

    def fake_capture(*, target, monitor, region, max_width):
        captured.append({"target": target, "monitor": monitor, "region": region})
        return desktop_layer.Capture(png=b"\x89PNG", width=800, height=600)

    def fake_ocr(*, target, monitor, region, lang):
        captured.append({"target": target, "monitor": monitor, "region": region})
        return "text on the screen"

    monkeypatch.setattr(desktop_layer, "capture", fake_capture)
    monkeypatch.setattr(desktop_layer, "ocr", fake_ocr)

    mcp = MCPServer(name="t")
    catalogue = Catalogue()
    desktop_tools.register(catalogue, Settings(Config()), logging.getLogger("t"), Stats())
    catalogue.apply(mcp, Config())
    return mcp, captured


async def _call(mcp, name, args):
    result = await mcp.call_tool(name, args)
    return result.content


@pytest.mark.anyio
async def test_a_connected_monitor_is_captured_and_named(tools):
    mcp, captured = tools
    content = await _call(
        mcp, "omarchy_screenshot", {"target": "monitor", "monitor": "DP-1"}
    )
    assert captured == [{"target": "monitor", "monitor": "DP-1", "region": ""}]
    # The text part beside the image says which screen this actually was.
    assert "DP-1 (Dell Inc. DELL U2723QE)" in content[-1].text


@pytest.mark.anyio
async def test_a_monitor_that_is_not_connected_never_reaches_grim(tools):
    mcp, captured = tools
    content = await _call(
        mcp, "omarchy_screenshot", {"target": "monitor", "monitor": "HDMI-1"}
    )
    payload = json.loads(content[0].text)
    assert payload["unresolved"] == "monitor"
    assert payload["did_you_mean"][0] == "HDMI-A-1"
    assert captured == [], "nothing may be captured for a monitor that is not there"


@pytest.mark.anyio
async def test_ocr_refuses_the_same_name_the_same_way(tools):
    mcp, captured = tools
    args = {"target": "monitor", "monitor": "HDMI-1"}
    payload = json.loads((await _call(mcp, "omarchy_screen_text", args))[0].text)
    assert payload["reason"] == "not_found"
    assert captured == []


@pytest.mark.anyio
async def test_ocr_reports_which_monitor_it_read(tools):
    mcp, _ = tools
    payload = json.loads(
        (await _call(mcp, "omarchy_screen_text", {"target": "monitor", "monitor": "DP-1"}))[0].text
    )
    assert payload["text"] == "text on the screen"
    assert payload["target"] == "DP-1 (Dell Inc. DELL U2723QE)"


@pytest.mark.anyio
async def test_a_monitor_name_is_ignored_where_it_has_no_effect(tools):
    """`target="screen"` captures the focused output; refusing the whole call
    over an argument that is never read would be a refusal about nothing."""
    mcp, captured = tools
    content = await _call(
        mcp, "omarchy_screenshot", {"target": "screen", "monitor": "HDMI-1"}
    )
    assert captured == [{"target": "screen", "monitor": "HDMI-1", "region": ""}]
    assert content[0].type == "image"


@pytest.mark.anyio
async def test_a_compositor_that_is_not_answering_says_so(tools, monkeypatch):
    from omarchy_mcp import resolve

    def down():
        raise resolve.Unresolvable(
            "monitor", "could not list monitors: hyprctl is not reachable", source_unavailable=True
        )

    monkeypatch.setattr(resolve, "_monitors", down)
    mcp, captured = tools
    payload = json.loads(
        (await _call(mcp, "omarchy_screenshot", {"target": "monitor", "monitor": "DP-1"}))[0].text
    )
    assert payload["reason"] == "source_unavailable"
    assert "did_you_mean" not in payload
    assert captured == []
