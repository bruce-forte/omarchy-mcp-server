"""Resources, exercised through a real MCP session.

Resources are the half a person reads rather than the half an agent calls, so
what matters is that the listing is discoverable and that reading by URI
resolves -- including the templates, which Claude Code does not enumerate but
does resolve.
"""

from __future__ import annotations

import json
import logging

import pytest
from starlette.testclient import TestClient

from omarchy_mcp.config import Config
from omarchy_mcp.server import build

from .test_server import BASE_URL, PROTOCOL, TOKEN, parse, rpc

#: The five that appear in a client's `@` menu.
CONCRETE = {
    "omarchy://commands",
    "omarchy://permissions",
    "omarchy://shell/targets",
    "omarchy://desktop/state",
    "omarchy://system/status",
}

#: The three that cover every command and target without a listing of hundreds.
TEMPLATES = {
    "omarchy://command/{route}",
    "omarchy://commands/{group}",
    "omarchy://shell/target/{name}",
}


@pytest.fixture
def session_client():
    app = build(Config(), TOKEN, logging.getLogger("test"))
    with TestClient(app, base_url=BASE_URL) as client:
        response = rpc(
            client,
            "initialize",
            {"protocolVersion": PROTOCOL, "capabilities": {},
             "clientInfo": {"name": "pytest", "version": "1"}},
        )
        sid = response.headers["mcp-session-id"]
        rpc(client, "notifications/initialized", session=sid)
        yield client, sid


def read(client, sid, uri):
    response = rpc(client, "resources/read", {"uri": uri}, session=sid)
    payload = parse(response)
    if "error" in payload:
        return payload
    return json.loads(payload["result"]["contents"][0]["text"])


def test_concrete_resources_are_listed(session_client):
    client, sid = session_client
    listed = parse(rpc(client, "resources/list", session=sid))["result"]["resources"]
    assert {r["uri"] for r in listed} == CONCRETE


def test_templates_are_listed_separately(session_client):
    client, sid = session_client
    listed = parse(rpc(client, "resources/templates/list", session=sid))["result"]
    assert {t["uriTemplate"] for t in listed["resourceTemplates"]} == TEMPLATES


def test_every_resource_describes_itself(session_client):
    """These show up as `@` mentions, where the description is all the user has
    to go on."""
    client, sid = session_client
    listed = parse(rpc(client, "resources/list", session=sid))["result"]["resources"]
    for resource in listed:
        assert resource.get("name")
        assert resource.get("description")


def test_command_registry_carries_our_verdict(session_client):
    """The raw registry would leave the reader to work out which commands can
    actually be run, which is the interesting half."""
    client, sid = session_client
    body = read(client, sid, "omarchy://commands")
    assert body["count"] > 100
    assert all("tier" in row and "runnable" in row for row in body["commands"])
    assert any(row["tier"] == "blocked" for row in body["commands"])


def test_commands_are_sorted(session_client):
    client, sid = session_client
    routes = [row["route"] for row in read(client, sid, "omarchy://commands")["commands"]]
    assert routes == sorted(routes)


def test_command_template_resolves(session_client):
    client, sid = session_client
    body = read(client, sid, "omarchy://command/omarchy theme set")
    assert body["route"] == "omarchy theme set"
    assert body["tier"] == "safe"


def test_command_template_accepts_a_route_without_the_prefix(session_client):
    client, sid = session_client
    assert read(client, sid, "omarchy://command/theme set")["route"] == "omarchy theme set"


def test_unknown_command_suggests_alternatives(session_client):
    client, sid = session_client
    body = read(client, sid, "omarchy://command/omarchy volume up")
    assert "no such command" in body["error"]
    assert body["did_you_mean"]


def test_group_template_resolves(session_client):
    client, sid = session_client
    body = read(client, sid, "omarchy://commands/theme")
    assert body["count"] > 1
    assert all(row["route"].startswith("omarchy theme") for row in body["commands"])


def test_unknown_group_lists_the_real_ones(session_client):
    client, sid = session_client
    body = read(client, sid, "omarchy://commands/nonsense")
    assert "no such group" in body["error"]
    assert "theme" in body["groups"]


def test_shell_targets_carry_signatures(session_client):
    client, sid = session_client
    body = read(client, sid, "omarchy://shell/targets")
    if "error" in body:
        pytest.skip("omarchy-shell is not running")
    assert body["count"] > 0
    methods = body["targets"][0]["methods"]
    assert methods and "signature" in methods[0]


def test_shell_target_template_resolves(session_client):
    client, sid = session_client
    body = read(client, sid, "omarchy://shell/target/media")
    if "error" in body and "not on PATH" in body["error"]:
        pytest.skip("omarchy-shell is not running")
    assert body["target"] == "media"


def test_the_permissions_resource_explains_itself(session_client):
    """The half a person reads: which rule decided, and which file it came
    from. Read by URI rather than asserted on the function, so the wiring is
    covered too."""
    client, session = session_client
    response = rpc(
        client,
        "resources/read",
        {"uri": "omarchy://permissions"},
        session=session,
    )
    body = json.loads(parse(response)["result"]["contents"][0]["text"])

    assert body["precedence"] == ["deny", "ask", "allow"]
    assert body["guardedDefault"] in ("ask", "deny")
    assert body["counts"]["commands"] > 0
    # Every guarded route is accounted for; the safe majority and the sudo
    # majority are counted rather than listed.
    assert body["routes"], "the routes this document governs are the point"
    assert body["counts"]["listed"] == len(body["routes"])
    assert body["counts"]["listed"] < body["counts"]["commands"]
