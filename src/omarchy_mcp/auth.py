"""Bearer authentication as pure ASGI.

This must not be a Starlette ``BaseHTTPMiddleware``. That class wraps the
receive channel, and the MCP streamable-HTTP transport runs a disconnect watcher
that calls ``receive()`` expecting ``http.disconnect``; through
``BaseHTTPMiddleware`` it gets ``http.request`` instead and every ``tools/call``
fails with a 500. Reading headers straight off the ASGI scope avoids the whole
problem. See ROADMAP.md finding F6.

DNS-rebinding protection is not implemented here -- the SDK's
``TransportSecuritySettings`` does it, and rejects a hostile ``Origin`` with 403.
"""

from __future__ import annotations

import hmac

UNAUTHORIZED = b'{"error":"unauthorized"}'


class BearerAuth:
    """Rejects any request to a guarded path without the exact token."""

    def __init__(self, app, token: str, *, open_paths: frozenset[str] = frozenset({"/health"})):
        self.app = app
        self._token = token
        self._open_paths = open_paths

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http" or scope.get("path") in self._open_paths:
            await self.app(scope, receive, send)
            return

        if not self._authorized(scope):
            await self._reject(send)
            return

        await self.app(scope, receive, send)

    def _authorized(self, scope) -> bool:
        for key, value in scope.get("headers") or ():
            if key == b"authorization":
                header = value.decode("latin-1")
                if not header.startswith("Bearer "):
                    return False
                # Constant time: the token is a secret and this endpoint is
                # reachable by anything that can open a local socket.
                return hmac.compare_digest(header[7:], self._token)
        return False

    async def _reject(self, send) -> None:
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
