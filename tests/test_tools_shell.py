"""`omarchy_shell_call` is the second door onto this plugin's own supervisor.

`gate.authorize` closes the first -- the `omarchy shell` route, reached through
`omarchy_run` -- but this tool does not go through the gate, so the check has to
exist here too. A test that only covered `policy.shell_call_refusal` would pass
while the tool never called it.
"""

from __future__ import annotations

import json
import logging

import pytest

from omarchy_mcp import execute, shell
from omarchy_mcp.config import Config
from omarchy_mcp.paths import PLUGIN_ID
from omarchy_mcp.policy import SELF_READ_VERBS
from omarchy_mcp.settings import Settings
from omarchy_mcp.stats import Stats
from omarchy_mcp.tools import generic
from omarchy_mcp.tools.catalogue import Catalogue

OWN_METHODS = (
    "status", "clientConfig", "start", "stop", "recent",
    "copyClientConfig", "restart", "reloadConfig", "rebuild",
)


@pytest.fixture
def tools(monkeypatch, ipc_listing):
    """The registered tools, with the shell listing pinned and nothing spawned."""
    from mcp.server.mcpserver import MCPServer

    listing = ipc_listing + "target " + PLUGIN_ID + "\n" + "".join(
        f"  function {m}(): string\n" for m in OWN_METHODS
    )
    monkeypatch.setattr(shell, "_list_raw", lambda: listing)

    spawned: list[list[str]] = []

    def fake_run(argv, **kwargs):
        spawned.append(list(argv))
        return execute.Result(
            exit_code=0,
            stdout="{}",
            stderr="",
            timed_out=False,
            detached=False,
            executable="/usr/bin/omarchy-shell",
        )

    monkeypatch.setattr(generic.execute, "run", fake_run)

    mcp = MCPServer(name="t")
    catalogue = Catalogue()
    generic.register(catalogue, Settings(Config()), logging.getLogger("t"), Stats())
    catalogue.apply(mcp, Config())
    return mcp, spawned


async def call(mcp, target, method, args=None):
    result = await mcp.call_tool(
        "omarchy_shell_call",
        {"target": target, "method": method, **({"args": args} if args else {})},
    )
    return json.loads(result.content[0].text)


@pytest.mark.anyio
@pytest.mark.parametrize(
    "method", [m for m in OWN_METHODS if m not in SELF_READ_VERBS]
)
async def test_own_target_refuses_everything_but_the_read_verbs(tools, method):
    mcp, spawned = tools
    body = await call(mcp, PLUGIN_ID, method)
    assert "error" in body
    assert body["tier"] == "blocked"
    assert spawned == [], "nothing may be spawned for a refused self-call"


@pytest.mark.anyio
@pytest.mark.parametrize("method", sorted(SELF_READ_VERBS))
async def test_own_target_still_answers_the_read_verbs(tools, method):
    mcp, spawned = tools
    body = await call(mcp, PLUGIN_ID, method)
    assert "error" not in body
    assert spawned == [["omarchy-shell", PLUGIN_ID, method]]


@pytest.mark.anyio
async def test_other_targets_are_untouched(tools):
    mcp, spawned = tools
    target = next(t for t in shell.targets() if t != PLUGIN_ID)
    method = shell.targets()[target].methods[0].name
    body = await call(mcp, target, method)
    assert "error" not in body
    assert spawned == [["omarchy-shell", target, method]]


@pytest.mark.anyio
async def test_the_refusal_tells_the_agent_where_to_send_the_user(tools):
    mcp, _ = tools
    body = await call(mcp, PLUGIN_ID, "stop")
    assert "panel" in body["error"]
    assert PLUGIN_ID in body["error"]
