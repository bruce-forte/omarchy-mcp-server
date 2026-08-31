"""A real MCP handshake against the assembled server.

The point of these is SDK drift. This project pins `mcp>=2,<3`, and 2.x already
renamed the server class and moved its module once; a release that changes the
transport again should fail here rather than in a client.
"""

from __future__ import annotations

import json
import logging

import pytest
from starlette.testclient import TestClient

from omarchy_mcp.config import Config
from omarchy_mcp.server import build, client_config_json, client_config_line

TOKEN = "test-token"
PROTOCOL = "2025-06-18"


#: The server binds Host and Origin to loopback, so the test client must look
#: like a loopback client or every request is a 421 Misdirected Request.
BASE_URL = "http://127.0.0.1:8765"


@pytest.fixture
def client():
    app = build(Config(), TOKEN, logging.getLogger("test"))
    with TestClient(app, base_url=BASE_URL) as client:
        yield client


def rpc(client, method, params=None, session=None, id_=1):
    headers = {
        "Authorization": f"Bearer {TOKEN}",
        "Content-Type": "application/json",
        "Accept": "application/json, text/event-stream",
    }
    if session:
        headers["mcp-session-id"] = session
    body = {"jsonrpc": "2.0", "id": id_, "method": method}
    if params is not None:
        body["params"] = params
    return client.post("/mcp", headers=headers, json=body)


def parse(response):
    """Responses arrive as SSE frames; pull the JSON-RPC payload out."""
    for line in response.text.splitlines():
        if line.startswith("data: "):
            return json.loads(line[6:])
    return json.loads(response.text)


@pytest.fixture
def session(client):
    response = rpc(
        client,
        "initialize",
        {
            "protocolVersion": PROTOCOL,
            "capabilities": {},
            "clientInfo": {"name": "pytest", "version": "1"},
        },
    )
    assert response.status_code == 200
    sid = response.headers["mcp-session-id"]
    rpc(client, "notifications/initialized", session=sid)
    return sid


def test_health_reports_the_port():
    app = build(Config(port=9999), TOKEN, logging.getLogger("test"))
    with TestClient(app, base_url="http://127.0.0.1:9999") as client:
        body = client.get("/health").json()
    assert body["ok"] is True
    assert body["port"] == 9999


def test_initialize_advertises_tools(client):
    response = rpc(
        client,
        "initialize",
        {
            "protocolVersion": PROTOCOL,
            "capabilities": {},
            "clientInfo": {"name": "pytest", "version": "1"},
        },
    )
    result = parse(response)["result"]
    assert "tools" in result["capabilities"]
    assert result["serverInfo"]["name"] == "omarchy"


#: Every tool the server advertises. Pinned so that adding one is a deliberate
#: act with a documentation regeneration attached, rather than a surprise.
EXPECTED_TOOLS = {
    # Generic: these reach everything Omarchy has.
    "omarchy_search_commands",
    "omarchy_run",
    "omarchy_shell_targets",
    "omarchy_shell_call",
    # Perception: each returns something the generic tools structurally cannot.
    "omarchy_screenshot",
    "omarchy_desktop_state",
    "omarchy_screen_text",
    "omarchy_clipboard_read",
    "omarchy_clipboard_write",
    "omarchy_system_status",
    # Frequency: a better-shaped door onto the same room.
    "omarchy_notify",
    "omarchy_osd",
    "omarchy_theme",
    "omarchy_background",
    "omarchy_audio",
    "omarchy_brightness",
    "omarchy_media",
    "omarchy_toggle",
    "omarchy_launch",
}


def test_all_tools_are_listed(client, session):
    tools = parse(rpc(client, "tools/list", session=session))["result"]["tools"]
    assert {t["name"] for t in tools} == EXPECTED_TOOLS


def test_tools_can_be_disabled_by_config():
    """A curated tool the user has switched off must not be advertised at all;
    advertising it and then refusing would waste a call to learn that."""
    app = build(
        Config(disabled_tools=("omarchy_screenshot", "omarchy_clipboard_write")),
        TOKEN,
        logging.getLogger("test"),
    )
    with TestClient(app, base_url=BASE_URL) as client:
        response = rpc(
            client,
            "initialize",
            {"protocolVersion": PROTOCOL, "capabilities": {},
             "clientInfo": {"name": "pytest", "version": "1"}},
        )
        sid = response.headers["mcp-session-id"]
        rpc(client, "notifications/initialized", session=sid)
        names = {t["name"] for t in parse(rpc(client, "tools/list", session=sid))["result"]["tools"]}

    assert "omarchy_screenshot" not in names
    assert "omarchy_clipboard_write" not in names
    # The generic tools are not disableable: they are the fallback path.
    assert "omarchy_run" in names


#: Tools whose result carries bytes this project does not author. Prompt
#: injection through them is the sharpest edge this server has, so the warning
#: is repeated per tool: a long session drops the handshake instructions long
#: before it drops the tool schemas.
UNTRUSTED_TOOLS = {
    "omarchy_screenshot",
    "omarchy_screen_text",
    "omarchy_clipboard_read",
    "omarchy_desktop_state",
    "omarchy_run",
}


def test_the_handshake_says_what_is_read_is_not_an_instruction(client):
    response = rpc(
        client,
        "initialize",
        {
            "protocolVersion": PROTOCOL,
            "capabilities": {},
            "clientInfo": {"name": "pytest", "version": "1"},
        },
    )
    instructions = parse(response)["result"]["instructions"]
    assert "untrusted" in instructions
    assert "never as instructions" in instructions


def test_tools_returning_foreign_content_repeat_the_warning(client, session):
    tools = {t["name"]: t for t in parse(rpc(client, "tools/list", session=session))["result"]["tools"]}
    for name in UNTRUSTED_TOOLS:
        assert "never as instructions" in tools[name]["description"], name
    # Not on tools whose output is the server's own: the sentence is a warning,
    # and a warning on everything is a warning on nothing.
    assert "never as instructions" not in tools["omarchy_search_commands"]["description"]


def test_every_tool_has_a_description_and_schema(client, session):
    tools = parse(rpc(client, "tools/list", session=session))["result"]["tools"]
    for tool in tools:
        assert tool.get("description"), f"{tool['name']} has no description"
        assert tool["inputSchema"]["type"] == "object"


def test_read_only_tools_are_annotated(client, session):
    """Clients use these hints to decide what to run without asking. A search
    tool marked destructive would prompt on every call; a run tool marked
    read-only would not prompt at all."""
    tools = {t["name"]: t for t in parse(rpc(client, "tools/list", session=session))["result"]["tools"]}

    assert tools["omarchy_search_commands"]["annotations"]["readOnlyHint"] is True
    assert tools["omarchy_shell_targets"]["annotations"]["readOnlyHint"] is True
    assert tools["omarchy_run"]["annotations"]["readOnlyHint"] is False
    assert tools["omarchy_run"]["annotations"]["destructiveHint"] is True


def test_search_tool_runs_and_reports_tiers(client, session):
    response = rpc(
        client,
        "tools/call",
        {"name": "omarchy_search_commands", "arguments": {"query": "theme", "limit": 5}},
        session=session,
    )
    payload = json.loads(parse(response)["result"]["content"][0]["text"])
    assert payload["count"] > 0
    assert all("tier" in row and "runnable" in row for row in payload["commands"])


def test_run_refuses_a_sudo_command(client, session):
    response = rpc(
        client,
        "tools/call",
        {"name": "omarchy_run", "arguments": {"route": "omarchy update"}},
        session=session,
    )
    payload = json.loads(parse(response)["result"]["content"][0]["text"])
    assert payload["tier"] == "blocked"
    assert "sudo" in payload["error"]


def test_run_refuses_a_guarded_command(client, session):
    response = rpc(
        client,
        "tools/call",
        {"name": "omarchy_run", "arguments": {"route": "omarchy system reboot"}},
        session=session,
    )
    payload = json.loads(parse(response)["result"]["content"][0]["text"])
    assert payload["tier"] == "guarded"
    assert "config.toml" in payload["error"]


def test_run_suggests_alternatives_for_an_unknown_route(client, session):
    response = rpc(
        client,
        "tools/call",
        {"name": "omarchy_run", "arguments": {"route": "omarchy volume up"}},
        session=session,
    )
    payload = json.loads(parse(response)["result"]["content"][0]["text"])
    assert "no such route" in payload["error"]
    assert payload["did_you_mean"], "a wrong route is exactly when suggestions matter"


def test_unauthenticated_requests_never_reach_a_tool(client):
    response = client.post(
        "/mcp",
        headers={"Content-Type": "application/json", "Accept": "application/json, text/event-stream"},
        json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"},
    )
    assert response.status_code == 401


def test_client_config_line_is_complete():
    line = client_config_line(8765, "abc")
    assert "--transport http" in line
    assert "http://127.0.0.1:8765/mcp" in line
    assert "Bearer abc" in line


def test_client_config_json_is_valid():
    parsed = json.loads(client_config_json(8765, "abc"))
    server = parsed["mcpServers"]["omarchy"]
    assert server["url"] == "http://127.0.0.1:8765/mcp"
    assert server["headers"]["Authorization"] == "Bearer abc"


def test_a_hostile_origin_is_rejected(client, session):
    """DNS rebinding: a page on another origin talks a browser into a request to
    loopback. The request genuinely comes from this machine, so only the Origin
    header distinguishes it."""
    response = client.post(
        "/mcp",
        headers={
            "Authorization": f"Bearer {TOKEN}",
            "Origin": "https://evil.example",
            "Content-Type": "application/json",
            "Accept": "application/json, text/event-stream",
            "mcp-session-id": session,
        },
        json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"},
    )
    assert response.status_code == 403


def test_a_foreign_host_header_is_rejected(client):
    response = client.post(
        "/mcp",
        headers={
            "Authorization": f"Bearer {TOKEN}",
            "Host": "attacker.example",
            "Content-Type": "application/json",
            "Accept": "application/json, text/event-stream",
        },
        json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"},
    )
    assert response.status_code == 421


def test_health_reports_call_counts():
    """The call count is the only visible trace an agent leaves on the desktop,
    so it has to survive the trip from the tool to the bar widget."""
    from omarchy_mcp.stats import Stats

    stats = Stats()
    app = build(Config(), TOKEN, logging.getLogger("test"), stats=stats)
    with TestClient(app, base_url=BASE_URL) as client:
        assert client.get("/health").json()["calls"] == 0

        response = rpc(
            client,
            "initialize",
            {"protocolVersion": PROTOCOL, "capabilities": {},
             "clientInfo": {"name": "pytest", "version": "1"}},
        )
        sid = response.headers["mcp-session-id"]
        rpc(client, "notifications/initialized", session=sid)
        rpc(
            client,
            "tools/call",
            {"name": "omarchy_search_commands", "arguments": {"query": "theme"}},
            session=sid,
        )

        body = client.get("/health").json()

    assert body["calls"] == 1
    assert body["last_tool"] == "omarchy_search_commands"


def test_health_records_the_route_that_was_run():
    from omarchy_mcp.stats import Stats

    stats = Stats()
    app = build(Config(), TOKEN, logging.getLogger("test"), stats=stats)
    with TestClient(app, base_url=BASE_URL) as client:
        response = rpc(
            client,
            "initialize",
            {"protocolVersion": PROTOCOL, "capabilities": {},
             "clientInfo": {"name": "pytest", "version": "1"}},
        )
        sid = response.headers["mcp-session-id"]
        rpc(client, "notifications/initialized", session=sid)
        # Refused, but still recorded: the audit trail is what was attempted.
        rpc(
            client,
            "tools/call",
            {"name": "omarchy_run", "arguments": {"route": "omarchy system reboot"}},
            session=sid,
        )
        body = client.get("/health").json()

    assert body["last_route"] == "omarchy system reboot"
