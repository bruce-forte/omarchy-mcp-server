"""Telling an attached client that the tool list changed.

The reload itself is pinned in `test_reload.py`. These are about the wire: what
the handshake promises, and whether the promised frame actually arrives on a
client's back-channel while it is sitting there attached.

The capability assertion is the one that would otherwise ship silently. A
client is entitled to ignore a notification the server said it would never
send, and until this was built, this server said exactly that -- measured
against the running daemon: `"tools":{"listChanged":false}`.
"""

from __future__ import annotations

import json
import logging
import queue
import socket
import threading

# The SDK's own HTTP client, so this needs no dependency the daemon does not
# already have. Used for the one test that cannot go through `TestClient`.
import httpx2 as httpx
import pytest
import uvicorn
from starlette.testclient import TestClient

from omarchy_mcp.config import Config, load
from omarchy_mcp.server import build
from omarchy_mcp.settings import Settings

TOKEN = "test-token"
PROTOCOL = "2025-06-18"
BASE_URL = "http://127.0.0.1:8765"

#: The reload polls every 2s. This is how long a test waits for it, generously,
#: before calling the notification missing -- a hang here is a real failure and
#: is meant to be seen rather than skipped.
DEADLINE_S = 15


def headers(session: str | None = None, accept: str = "application/json, text/event-stream"):
    out = {"Authorization": f"Bearer {TOKEN}", "Accept": accept, "Content-Type": "application/json"}
    if session:
        out["mcp-session-id"] = session
    return out


def rpc(client, method, params=None, session=None, id_: int | None = 1):
    body = {"jsonrpc": "2.0", "method": method}
    if id_ is not None:
        body["id"] = id_
    if params is not None:
        body["params"] = params
    return client.post("/mcp", headers=headers(session), json=body)


def parse(response):
    for line in response.text.splitlines():
        if line.startswith("data: "):
            return json.loads(line[6:])
    return json.loads(response.text)


@pytest.fixture
def config_file(tmp_path):
    path = tmp_path / "config.toml"
    path.write_text("")
    return path


@pytest.fixture
def client(config_file):
    """A server watching a config file the test owns."""
    app = build(
        Settings(load(config_file)),
        TOKEN,
        logging.getLogger("test"),
        reload_from=config_file,
    )
    with TestClient(app, base_url=BASE_URL) as client:
        yield client


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


@pytest.fixture
def live_server(config_file):
    """A real uvicorn on a real port, watching `config_file`.

    The port is fixed before the app is built because the server binds Host and
    Origin to `127.0.0.1:<port>`: a client that talked to a different port than
    the one the app was told about would get a 421.
    """
    port = _free_port()
    app = build(
        Settings(Config(port=port)),
        TOKEN,
        logging.getLogger("test"),
        reload_from=config_file,
    )
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning"))
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    for _ in range(100):
        if server.started:
            break
        threading.Event().wait(0.05)
    else:  # pragma: no cover - a server that never starts fails the test anyway
        pytest.fail("the test server did not start")

    yield f"http://127.0.0.1:{port}", config_file

    server.should_exit = True
    thread.join(timeout=10)


@pytest.fixture
def session(client):
    response = rpc(
        client,
        "initialize",
        {"protocolVersion": PROTOCOL, "capabilities": {}, "clientInfo": {"name": "pytest", "version": "1"}},
    )
    assert response.status_code == 200
    sid = response.headers["mcp-session-id"]
    rpc(client, "notifications/initialized", session=sid, id_=None)
    return sid


def tool_names(client, session) -> set[str]:
    result = parse(rpc(client, "tools/list", {}, session=session, id_=2))["result"]
    return {tool["name"] for tool in result["tools"]}


def test_the_handshake_promises_list_changed(client):
    result = parse(
        rpc(
            client,
            "initialize",
            {
                "protocolVersion": PROTOCOL,
                "capabilities": {},
                "clientInfo": {"name": "pytest", "version": "1"},
            },
        )
    )["result"]

    assert result["capabilities"]["tools"]["listChanged"] is True


def test_a_disabled_tool_leaves_an_attached_session(client, session, config_file):
    assert "omarchy_screenshot" in tool_names(client, session)

    config_file.write_text('[tools]\ndisabled = ["omarchy_screenshot"]\n')

    deadline = threading.Event()
    for _ in range(DEADLINE_S * 4):
        if "omarchy_screenshot" not in tool_names(client, session):
            break
        deadline.wait(0.25)
    else:
        pytest.fail("the tool was still offered after the reload should have run")


def test_the_notification_reaches_a_listening_client(live_server):
    """The frame itself, on the standalone GET stream a client opens.

    Against a real uvicorn rather than `TestClient`, which cannot do this: its
    portal will not flush a streaming response to a second thread while the
    first one is waiting, so the notification only surfaced after the test had
    already given up. The thing under test is a server-initiated frame arriving
    while a client sits attached, and that needs a real socket.
    """
    base, config_file = live_server
    frames: queue.Queue = queue.Queue()

    with httpx.Client(base_url=base, timeout=10) as http:
        response = http.post(
            "/mcp",
            headers=headers(),
            json={
                "jsonrpc": "2.0",
                "id": 1,
                "method": "initialize",
                "params": {
                    "protocolVersion": PROTOCOL,
                    "capabilities": {},
                    "clientInfo": {"name": "pytest", "version": "1"},
                },
            },
        )
        session = response.headers["mcp-session-id"]
        http.post(
            "/mcp",
            headers=headers(session),
            json={"jsonrpc": "2.0", "method": "notifications/initialized"},
        )

        stop = threading.Event()

        def listen():
            try:
                with httpx.Client(base_url=base, timeout=None) as stream_client:
                    with stream_client.stream(
                        "GET", "/mcp", headers=headers(session, "text/event-stream")
                    ) as sse:
                        for line in sse.iter_lines():
                            if line.startswith("data: "):
                                frames.put(json.loads(line[6:]))
                            if stop.is_set():
                                return
            except Exception as exc:
                frames.put({"error": str(exc)})

        reader = threading.Thread(target=listen, daemon=True)
        reader.start()
        # Let the GET stream be established before the edit lands.
        stop.wait(0.5)

        config_file.write_text('[tools]\ndisabled = ["omarchy_theme"]\n')

        methods = []
        for _ in range(DEADLINE_S):
            try:
                frame = frames.get(timeout=1)
            except queue.Empty:
                continue
            methods.append(frame.get("method"))
            if frame.get("method") == "notifications/tools/list_changed":
                break
        else:
            pytest.fail(f"no tools/list_changed arrived; saw {methods}")
        stop.set()


def test_a_policy_edit_does_not_claim_the_tools_moved(client, session, config_file):
    """`tools/list_changed` is a claim about the tool list.

    A client that re-listed after a policy edit would be told the same thing it
    already knew. The tier a route reports does change -- that is a
    `omarchy_search_commands` answer, not a schema.
    """
    before = tool_names(client, session)

    config_file.write_text('[policy]\ndeny = ["omarchy theme set"]\n')
    threading.Event().wait(3)

    assert tool_names(client, session) == before
