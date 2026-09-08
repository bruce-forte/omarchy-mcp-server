"""Ask this server for something guarded, and answer in the terminal.

Run it with ``make elicit``. What it demonstrates is the consent path that
**Claude Code cannot take**, and the reason is worth stating before the code:

MCP elicitation is a *server-initiated request* -- the server asks the client a
question in the middle of handling a tool call. That needs a channel pointing
back from the server to the client, and whether one exists is decided by the
protocol revision the two of them negotiate:

===================================  ==========================================
``initialize()``  (2025-11-25 and    the streamable-HTTP transport sets
older, the *handshake* era)          ``can_send_request = not
                                     is_json_response_enabled`` -- so with SSE
                                     there **is** a back-channel, and this
                                     script gets the question
``server/discover`` (2026-07-28,     ``can_send_request`` is a field defaulting
the *modern* era)                    to ``False`` that every construction site
                                     passes ``False`` to. There is **no**
                                     back-channel at all
===================================  ==========================================

Claude Code sends ``server/discover`` and lands in the second row, so
``ctx.elicit`` fails inside the daemon before anything reaches the wire, and the
question goes to a desktop notification instead. That is `ROADMAP.md` finding
F23, and it is why the notification is the primary surface rather than a nicety
beside it.

This script calls ``initialize()`` and lands in the first row. Nothing about the
server changes -- it serves both eras and answers each connection in the era
that connection negotiated.

**It declines by default**, so running it changes nothing. Pass ``--accept`` to
say yes and let the command actually run.
"""

from __future__ import annotations

import pathlib
import sys
import tomllib

import anyio
import httpx2
from mcp import ClientSession, types
from mcp.client.streamable_http import streamable_http_client

PLUGIN_ID = "io.github.bruce-forte.mcp-server"
STATE_DIR = pathlib.Path.home() / ".local/state" / PLUGIN_ID
CONFIG_FILE = pathlib.Path.home() / ".config/omarchy/mcp/config.toml"

#: The default call. Guarded routes ask on their own; `omarchy theme set` is
#: safe and asks only if you have written an `ask` rule for it -- which is what
#: the README's getting-started walkthrough has you do, and why it is the
#: default here. Override by passing a route and its arguments.
DEFAULT_ROUTE = "omarchy theme set"
DEFAULT_ARGS = ["Tokyo Night"]


def _port() -> int:
    """The port the daemon is on, from the same config file it reads."""
    try:
        return int(tomllib.loads(CONFIG_FILE.read_text()).get("server", {}).get("port", 8765))
    except (OSError, ValueError, tomllib.TOMLDecodeError):
        return 8765


def _token() -> str:
    """The bearer token. Read from the file, never printed."""
    try:
        return (STATE_DIR / "token").read_text().strip()
    except OSError:
        sys.exit(
            f"No token at {STATE_DIR / 'token'}.\n"
            f"The daemon writes it on first run -- start it from the bar panel."
        )


def _parse(argv: list[str]) -> tuple[bool, str, list[str]]:
    """``[--accept] [route [args...]]``, with the defaults above."""
    accept = "--accept" in argv
    rest = [a for a in argv if a != "--accept"]
    if not rest:
        return accept, DEFAULT_ROUTE, list(DEFAULT_ARGS)
    return accept, rest[0], rest[1:]


async def main() -> int:
    accept, route, args = _parse(sys.argv[1:])
    url = f"http://127.0.0.1:{_port()}/mcp"

    async def on_elicit(_ctx, params):
        """Called by the SDK when the server asks this client a question.

        Passing this callback at all is what makes the client *declare*
        elicitation in its capabilities; without it the SDK advertises none and
        the server would never try.
        """
        print("\n--- the server is asking -------------------------------------")
        print(params.message)
        print("--------------------------------------------------------------")
        if accept:
            print(">>> accepting\n")
            return types.ElicitResult(action="accept", content={})
        print(">>> declining (pass --accept to say yes)\n")
        return types.ElicitResult(action="decline")

    # Headers go on the HTTP client: the transport takes a configured client
    # rather than headers of its own.
    async with httpx2.AsyncClient(headers={"Authorization": f"Bearer {_token()}"}) as http:
        async with streamable_http_client(url, http_client=http) as (read, write):
            async with ClientSession(read, write, elicitation_callback=on_elicit) as session:
                # `initialize()` negotiates the handshake era only. The modern
                # era is reached by `discover()`, which is the call this script
                # deliberately does not make.
                init = await session.initialize()
                print(f"negotiated protocol : {init.protocol_version}")
                print(f"calling             : {route} {args}")

                result = await session.call_tool(
                    "omarchy_run", {"route": route, "args": args}
                )
                print("\n--- what the agent would have been told ----------------------")
                for block in result.content:
                    print(getattr(block, "text", block))
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(anyio.run(main))
    except* Exception as group:
        # The transport wraps failures in an ExceptionGroup, and a connection
        # refused is the ordinary case here rather than a bug worth a traceback.
        for exc in group.exceptions:
            print(f"\ncould not talk to the daemon: {exc}", file=sys.stderr)
        print(
            "Is it serving? omarchy-shell io.github.bruce-forte.mcp-server status",
            file=sys.stderr,
        )
        raise SystemExit(1) from None
