"""Assembling the MCP server."""

from __future__ import annotations

import json
import logging

from mcp.server.mcpserver import MCPServer
from mcp.server.transport_security import TransportSecuritySettings
from starlette.responses import JSONResponse

from . import __version__
from .auth import BearerAuth
from .config import Config
from .stats import Stats
from .tools import desktop, generic, system

SERVER_NAME = "omarchy"


def _transport_security(port: int) -> TransportSecuritySettings:
    """Bound Host and Origin to loopback.

    Protects against DNS rebinding: a hostile page cannot make a browser send a
    request that this server will accept, even though the request itself
    genuinely originates from this machine.
    """
    hosts = [f"127.0.0.1:{port}", f"localhost:{port}", f"[::1]:{port}"]
    origins = [f"http://{h}" for h in hosts]
    return TransportSecuritySettings(
        enable_dns_rebinding_protection=True,
        allowed_hosts=hosts,
        allowed_origins=origins,
    )


def build(config: Config, token: str, log: logging.Logger, *, stats: Stats | None = None):
    """Build the ASGI application for the MCP server."""
    stats = stats or Stats()
    mcp = MCPServer(
        name=SERVER_NAME,
        title="Omarchy",
        version=__version__,
        instructions=(
            "This server drives an Omarchy desktop. Discover what is available with "
            "omarchy_search_commands, then act with omarchy_run. Anything belonging to "
            "the running shell -- bar, OSD, notifications, media, plugins -- is reached "
            "through omarchy_shell_targets and omarchy_shell_call instead. Commands "
            "requiring sudo cannot be run, and commands that change the system in ways "
            "that are hard to undo are refused unless the user has allowed them."
        ),
    )

    generic.register(mcp, config, log, stats)
    desktop.register(mcp, config, log, stats)
    system.register(mcp, config, log, stats)

    @mcp.custom_route("/health", methods=["GET"])
    async def health(_request):
        # Reached without a token: it carries no secrets and it is how the
        # supervising QML tells "serving" from "process exists".
        return JSONResponse(
            {
                "ok": True,
                "server": SERVER_NAME,
                "version": __version__,
                "port": config.port,
                **stats.snapshot(),
            }
        )

    app = mcp.streamable_http_app(transport_security=_transport_security(config.port))
    return BearerAuth(app, token)


def client_config_line(port: int, token: str) -> str:
    """The one command a user runs to point Claude Code at this server."""
    return (
        f"claude mcp add --transport http {SERVER_NAME} "
        f"http://127.0.0.1:{port}/mcp "
        f'--header "Authorization: Bearer {token}"'
    )


def client_config_json(port: int, token: str) -> str:
    """The same thing, for clients configured by file rather than by CLI."""
    return json.dumps(
        {
            "mcpServers": {
                SERVER_NAME: {
                    "type": "http",
                    "url": f"http://127.0.0.1:{port}/mcp",
                    "headers": {"Authorization": f"Bearer {token}"},
                }
            }
        },
        indent=2,
    )
