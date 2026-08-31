"""The auth boundary, exercised through a real ASGI stack.

These are the guarantees a regression would silently remove, so they are tested
against the middleware itself rather than by inspection.
"""

from __future__ import annotations

import pytest
from starlette.applications import Starlette
from starlette.responses import PlainTextResponse
from starlette.routing import Route
from starlette.testclient import TestClient

from omarchy_mcp.auth import BearerAuth

TOKEN = "correct-horse-battery-staple"


@pytest.fixture
def client():
    async def ok(_request):
        return PlainTextResponse("reached")

    inner = Starlette(routes=[Route("/mcp", ok, methods=["GET", "POST"]),
                              Route("/health", ok)])
    return TestClient(BearerAuth(inner, TOKEN))


def test_missing_token_is_rejected(client):
    assert client.post("/mcp").status_code == 401


def test_wrong_token_is_rejected(client):
    r = client.post("/mcp", headers={"Authorization": "Bearer wrong"})
    assert r.status_code == 401


def test_token_prefix_is_not_enough(client):
    """A truncated token must not pass; compare_digest, not startswith."""
    r = client.post("/mcp", headers={"Authorization": f"Bearer {TOKEN[:-1]}"})
    assert r.status_code == 401


def test_token_without_bearer_scheme_is_rejected(client):
    assert client.post("/mcp", headers={"Authorization": TOKEN}).status_code == 401


def test_other_schemes_are_rejected(client):
    r = client.post("/mcp", headers={"Authorization": f"Basic {TOKEN}"})
    assert r.status_code == 401


def test_correct_token_passes(client):
    r = client.post("/mcp", headers={"Authorization": f"Bearer {TOKEN}"})
    assert r.status_code == 200
    assert r.text == "reached"


def test_health_needs_no_token(client):
    """The supervising QML polls this to tell serving from merely running."""
    assert client.get("/health").status_code == 200


def test_rejection_names_the_scheme(client):
    assert "Bearer" in client.post("/mcp").headers.get("www-authenticate", "")
