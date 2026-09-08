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
import anyio.to_thread
import httpx2
from mcp import ClientSession, types
from mcp.client.session import ClientRequestContext
from mcp.client.streamable_http import streamable_http_client

PLUGIN_ID = "io.github.bruce-forte.mcp-server"
STATE_DIR = pathlib.Path.home() / ".local/state" / PLUGIN_ID
CONFIG_FILE = pathlib.Path.home() / ".config/omarchy/mcp/config.toml"

#: With no arguments the script calls `omarchy_theme(action="set")` and names no
#: theme, which is the *form* demonstration: the server has a missing parameter
#: rather than a permission question, and asks which one with the installed
#: themes as an enum. Passing a route switches to the consent demonstration,
#: running it through `omarchy_run` instead.
FALLBACK_ROUTE = "omarchy theme set"


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


def _parse(argv: list[str]) -> tuple[bool, int | None, str | None, list[str]]:
    """``[--accept] [--port N] [route [args...]]``.

    No route means the form demonstration. ``--port`` targets a daemon other
    than the configured one, which is how you point this at a checkout running
    under ``make run`` while the installed plugin keeps serving.
    """
    accept = "--accept" in argv
    rest = [a for a in argv if a != "--accept"]
    port = None
    if "--port" in rest:
        at = rest.index("--port")
        try:
            port = int(rest[at + 1])
        except (IndexError, ValueError):
            sys.exit("--port needs a number")
        rest = rest[:at] + rest[at + 2:]
    if not rest:
        return accept, port, None, []
    return accept, port, rest[0], rest[1:]


def _choices(params: types.ElicitRequestParams) -> list[str]:
    """The enum a form is offering for its one field, if it offers one.

    The schema arrives as plain JSON Schema -- this client is not the server and
    has no pydantic model for it, which is the point of sending a schema at all.
    """
    schema = getattr(params, "requested_schema", None) or {}
    for prop in (schema.get("properties") or {}).values():
        if prop.get("enum"):
            return [str(v) for v in prop["enum"]]
    return []


def _leaves(exc: BaseException):
    """Every non-group exception inside a possibly-nested ExceptionGroup."""
    if isinstance(exc, BaseExceptionGroup):
        for sub in exc.exceptions:
            yield from _leaves(sub)
    else:
        yield exc


def _field_name(params) -> str:
    """The single field the form is asking for."""
    schema = getattr(params, "requested_schema", None) or {}
    names = list((schema.get("properties") or {}).keys())
    return names[0] if names else "value"


def _ask_at_the_terminal(params: types.ElicitRequestParams) -> dict | None:
    """Render the form as a numbered list and read an answer. None is a decline.

    Blocking, and called through ``anyio.to_thread.run_sync`` for that reason.
    ``input()`` on the event-loop thread stalls the whole client while somebody
    reads a list of themes -- it cannot service its own connection, and the
    session is torn down underneath the answer it is waiting for. The daemon
    does the same thing for the same reason; see ``tools/_shared.py``.
    """
    options = _choices(params)
    if not options:
        typed = input(f"{_field_name(params)}: ").strip()
        return {_field_name(params): typed} if typed else None

    for n, option in enumerate(options, 1):
        print(f"  {n:>2}. {option}")
    raw = input(f"choose 1-{len(options)} (enter to decline): ").strip()
    if not raw.isdigit() or not 1 <= int(raw) <= len(options):
        return None
    return {_field_name(params): options[int(raw) - 1]}


async def main() -> int:
    accept, port, route, args = _parse(sys.argv[1:])
    url = f"http://127.0.0.1:{port or _port()}/mcp"

    #: Whether the server asked us anything at all. A one-element list rather
    #: than a plain name because a nested function assigning to a name would
    #: make it local; `nonlocal` would work too, and this is the smaller change.
    asked: list[bool] = []

    async def on_elicit(
        context: ClientRequestContext, params: types.ElicitRequestParams
    ) -> types.ElicitResult:
        """Called by the SDK when the server asks this client a question.

        Passing this callback at all is what makes the client *declare*
        elicitation in its capabilities; without it the SDK advertises none and
        the server would never try.

        Two shapes arrive here, and the schema is what tells them apart. A
        consent question carries a schema with nothing required, so any accept
        will do. A *form* carries the field the server is missing -- here, an
        enum of the themes installed on this machine -- and an accept has to
        fill it in or the SDK rejects the reply before it is sent.
        """
        asked.append(True)
        print("\n--- the server is asking -------------------------------------")
        print(params.message)
        options = _choices(params)
        if options:
            print()
            content = await anyio.to_thread.run_sync(_ask_at_the_terminal, params)
            if content is None:
                print(">>> declining\n")
                return types.ElicitResult(action="decline")
            print(f">>> answering {content}\n")
            return types.ElicitResult(action="accept", content=content)

        print("--------------------------------------------------------------")
        if accept:
            print(">>> accepting\n")
            return types.ElicitResult(action="accept", content={})
        print(">>> declining (pass --accept to say yes)\n")
        return types.ElicitResult(action="decline")

    # Headers go on the HTTP client: the transport takes a configured client
    # rather than headers of its own.
    #
    # The timeout has to be set here too, and it is the whole reason a question
    # can be answered at human speed. Passing our own client means the SDK does
    # not build one, so its SSE-friendly defaults (30s connect, **300s read**)
    # are not applied and httpx's own 5s read timeout stands -- which tears the
    # stream down while somebody is still reading the list of themes, and
    # surfaces as `MCPError: SSE stream ended without a response`.
    timeout = httpx2.Timeout(30.0, read=300.0)
    async with httpx2.AsyncClient(
        headers={"Authorization": f"Bearer {_token()}"},
        timeout=timeout,
        follow_redirects=True,
    ) as http:
        async with streamable_http_client(url, http_client=http) as (read, write):
            async with ClientSession(read, write, elicitation_callback=on_elicit) as session:
                # `initialize()` negotiates the handshake era only. The modern
                # era is reached by `discover()`, which is the call this script
                # deliberately does not make.
                init = await session.initialize()
                print(f"negotiated protocol : {init.protocol_version}")

                if route is None:
                    # The form demonstration: ask for a theme switch without
                    # saying which theme. The server has a missing parameter,
                    # not a permission question, and asks which one.
                    print('calling             : omarchy_theme(action="set")'
                          " -- deliberately naming no theme")
                    result = await session.call_tool(
                        "omarchy_theme", {"action": "set"}
                    )
                else:
                    print(f"calling             : {route} {args}")
                    result = await session.call_tool(
                        "omarchy_run", {"route": route, "args": args}
                    )
                print("\n--- what the agent would have been told ----------------------")
                text = "\n".join(str(getattr(b, "text", b)) for b in result.content)
                print(text)

                if route is None and not asked and "needs a theme name" in text:
                    # The server had every reason to ask -- this client declares
                    # elicitation and negotiated an era that can carry the
                    # question -- and did not. Overwhelmingly the daemon is
                    # older than the checkout, which is what `make elicit`
                    # spawning its own daemon exists to avoid.
                    print(
                        "\nThe server did not ask, which means it is a build "
                        "without the form.\n"
                        "`make elicit` starts a daemon from this checkout; if "
                        "you pointed this at\n"
                        "an installed plugin, update it:\n"
                        "  omarchy plugin update io.github.bruce-forte.mcp-server\n"
                        "  omarchy-shell io.github.bruce-forte.mcp-server restart",
                        file=sys.stderr,
                    )
                    return 1
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(anyio.run(main))
    except* Exception as group:  # noqa: BLE001 -- the transport raises anything
        # The transport wraps failures in an ExceptionGroup, and a connection
        # refused is the ordinary case here rather than a bug worth a traceback.
        # Groups nest, so the leaves are what carry the message worth printing:
        # the outer one only ever says "unhandled errors in a TaskGroup".
        for exc in _leaves(group):
            print(f"\ncould not talk to the daemon: {type(exc).__name__}: {exc}", file=sys.stderr)
        print(
            "Is it serving? omarchy-shell io.github.bruce-forte.mcp-server status",
            file=sys.stderr,
        )
        raise SystemExit(1) from None
