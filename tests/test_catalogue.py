"""What the catalogue offers, and what it takes away.

The tool switch is the half of the design that already worked: a disabled tool
is never registered, so it is not in `tools/list` and prompt injection cannot
talk a model into calling something it cannot see. These pin that the property
survives the move from registration-time guards to `apply()`, in both
directions -- because now it has to hold on a config that arrives after the
server was built.
"""

from __future__ import annotations

import logging

import pytest
from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError

from omarchy_mcp.config import Config
from omarchy_mcp.settings import Settings
from omarchy_mcp.stats import Stats
from omarchy_mcp.tools import control, desktop, feedback, generic, system
from omarchy_mcp.tools.catalogue import Catalogue

LOG = logging.getLogger("test")


def declared() -> Catalogue:
    catalogue = Catalogue()
    settings = Settings(Config())
    stats = Stats()
    generic.register(catalogue, settings, LOG, stats)
    desktop.register(catalogue, settings, LOG, stats)
    system.register(catalogue, settings, LOG, stats)
    feedback.register(catalogue, settings, LOG, stats)
    control.register(catalogue, settings, LOG, stats)
    return catalogue


async def names(mcp: MCPServer) -> set[str]:
    return {tool.name for tool in await mcp.list_tools()}


def test_declaring_registers_nothing():
    catalogue = declared()
    assert catalogue.declared, "the tools exist"
    assert catalogue.present == (), "and none of them are offered until apply()"


@pytest.mark.anyio
async def test_apply_offers_everything_the_config_does_not_disable():
    catalogue, mcp = declared(), MCPServer(name="t")
    catalogue.apply(mcp, Config())
    assert await names(mcp) == set(catalogue.declared)


@pytest.mark.anyio
async def test_a_disabled_tool_is_absent_not_refused():
    catalogue, mcp = declared(), MCPServer(name="t")
    catalogue.apply(mcp, Config(disabled_tools=("omarchy_screenshot",)))

    assert "omarchy_screenshot" not in await names(mcp)
    # Absent, not present-and-refusing: the model is never shown a door it is
    # only going to be turned away from, and calling the name anyway finds
    # nothing there.
    with pytest.raises(ToolError):
        await mcp.call_tool("omarchy_screenshot", {}, None)


@pytest.mark.anyio
async def test_a_second_apply_takes_a_tool_away():
    catalogue, mcp = declared(), MCPServer(name="t")
    catalogue.apply(mcp, Config())

    change = catalogue.apply(mcp, Config(disabled_tools=("omarchy_theme",)))

    assert change.removed == ("omarchy_theme",)
    assert change.added == ()
    assert "omarchy_theme" not in await names(mcp)


@pytest.mark.anyio
async def test_a_second_apply_gives_one_back():
    catalogue, mcp = declared(), MCPServer(name="t")
    catalogue.apply(mcp, Config(disabled_tools=("omarchy_theme", "omarchy_audio")))

    change = catalogue.apply(mcp, Config(disabled_tools=("omarchy_theme",)))

    assert change.added == ("omarchy_audio",)
    assert change.removed == ()
    assert "omarchy_audio" in await names(mcp)


@pytest.mark.anyio
async def test_a_tool_given_back_still_works():
    # Re-registering hands the SDK the same function object it had before, so
    # the schema it derives has to come out the same.
    catalogue, first = declared(), MCPServer(name="t")
    catalogue.apply(first, Config())
    before = {t.name: t.input_schema for t in await first.list_tools()}

    catalogue.apply(first, Config(disabled_tools=("omarchy_run",)))
    catalogue.apply(first, Config())
    after = {t.name: t.input_schema for t in await first.list_tools()}

    assert before == after


@pytest.mark.anyio
async def test_applying_the_same_config_twice_changes_nothing():
    catalogue, mcp = declared(), MCPServer(name="t")
    catalogue.apply(mcp, Config())

    change = catalogue.apply(mcp, Config())

    assert not change, "an unchanged config must not look like an edit"


def test_an_unknown_name_in_disabled_tools_is_ignored():
    # A typo switches nothing off and breaks nothing. The config file has no
    # way to know what the tool names are, and refusing to start over one would
    # be the daemon dying quietly.
    catalogue, mcp = declared(), MCPServer(name="t")
    catalogue.apply(mcp, Config(disabled_tools=("omarchy_nonexistent",)))
    assert set(catalogue.present) == set(catalogue.declared)
