"""Bearer authentication as pure ASGI.

This must not be a Starlette ``BaseHTTPMiddleware``. That class wraps the
receive channel, and the MCP streamable-HTTP transport runs a disconnect watcher
that calls ``receive()`` expecting ``http.disconnect``; through
``BaseHTTPMiddleware`` it gets ``http.request`` instead and every ``tools/call``
fails with a 500. Reading headers straight off the ASGI scope avoids the whole
problem. See ROADMAP.md finding F6.

DNS-rebinding protection is not implemented here -- the SDK's
``TransportSecuritySettings`` does it, and rejects a hostile ``Origin`` with 403.

What "pure ASGI" means, if this is your first one
-------------------------------------------------

ASGI is the calling convention every async Python web server speaks. An
application is anything callable as ``await app(scope, receive, send)``:

``scope``
    A plain dict describing the request -- its ``"type"``, ``"path"``, and
    ``"headers"`` as a list of ``(name, value)`` pairs, all as raw ``bytes``
    because HTTP is a byte protocol and the header encoding is not guaranteed.
``receive``
    An awaitable to pull the next event from the client (body chunks, and the
    ``http.disconnect`` mentioned above).
``send``
    An awaitable to push an event back: one ``http.response.start`` carrying the
    status and headers, then one or more ``http.response.body``.

Middleware is just an app that holds another app: it inspects the request, and
either answers itself or calls the one it wraps. That is the whole of the class
below -- ``BearerAuth`` wraps the MCP application, and either passes the call
through or writes a 401 itself.
"""

from __future__ import annotations

import hmac

#: The 401 body, pre-encoded. The ``b`` prefix makes it ``bytes`` rather than a
#: string, which is what ASGI wants to send; building it once avoids re-encoding
#: the same eight words on every rejected request.
UNAUTHORIZED = b'{"error":"unauthorized"}'


class BearerAuth:
    """Rejects any request to a guarded path without the exact token."""

    def __init__(self, app, token: str, *, open_paths: frozenset[str] = frozenset({"/health"})):
        """Wrap ``app``, letting only ``open_paths`` through unauthenticated.

        The bare ``*`` means every argument after it must be passed by name --
        ``BearerAuth(app, token, open_paths=...)`` -- so a second positional
        string can never be mistaken for the token.

        A ``frozenset`` is an immutable set: fast membership tests, and safe as a
        default argument because it cannot be mutated by a caller. A plain
        ``set()`` default would be shared by every instance and is the classic
        Python trap.
        """
        self.app = app
        self._token = token
        self._open_paths = open_paths

    async def __call__(self, scope, receive, send):
        """The ASGI entry point: what ``await app(...)`` reaches.

        Defining ``__call__`` is what makes an *instance* of this class callable
        like a function, which is what ASGI requires.
        """
        # Lifespan and websocket scopes are not requests and carry no headers to
        # check; ``/health`` is deliberately open so a supervisor can probe it.
        if scope["type"] != "http" or scope.get("path") in self._open_paths:
            await self.app(scope, receive, send)
            return

        if not self._authorized(scope):
            await self._reject(send)
            return

        # Authorised: hand the untouched call to the application underneath.
        await self.app(scope, receive, send)

    def _authorized(self, scope) -> bool:
        """True only for an ``Authorization: Bearer <exact token>`` header."""
        # ASGI header names arrive lowercased and as bytes, so the comparison is
        # against ``b"authorization"`` rather than a string.
        for key, value in scope.get("headers") or ():
            if key == b"authorization":
                header = value.decode("latin-1")
                if not header.startswith("Bearer "):
                    return False
                # Constant time: the token is a secret and this endpoint is
                # reachable by anything that can open a local socket. ``==``
                # stops at the first differing character, so how long it takes
                # leaks how much of the token an attacker guessed correctly;
                # ``compare_digest`` always looks at everything.
                # ``header[7:]`` drops the "Bearer " prefix -- 7 characters.
                return hmac.compare_digest(header[7:], self._token)
        # No Authorization header at all.
        return False

    async def _reject(self, send) -> None:
        """Write the 401 directly, without ever reaching the wrapped app."""
        await send(
            {
                "type": "http.response.start",
                "status": 401,
                "headers": [
                    (b"content-type", b"application/json"),
                    (b"content-length", str(len(UNAUTHORIZED)).encode()),
                    (b"www-authenticate", b'Bearer realm="omarchy-mcp"'),
                ],
            }
        )
        await send({"type": "http.response.body", "body": UNAUTHORIZED})
