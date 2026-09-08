"""Reaching the clients that are already attached.

A reload that changes the tool set has to say so, or a session started before
the edit keeps offering a tool that is gone and keeps hiding one just granted.
The spec has one word for this -- `notifications/tools/list_changed` -- and two
unrelated ways to deliver it, because the transport changed between protocol
eras:

- **2026-07-28 and later** have no standing server-to-client stream. A client
  opts in with `subscriptions/listen`, and the SDK fans events out over a
  `SubscriptionBus` we hand to `MCPServer(subscriptions=...)`. Publishing to
  that bus is the whole job.
- **2025-06-18 and earlier** carry server-initiated messages on the standalone
  `GET` SSE stream, one per connection. The SDK holds those inside the
  transport and offers no way to enumerate them, so this keeps its own
  register: a middleware sees every inbound request -- `initialize` included --
  and remembers the connection it arrived on.

The register holds `Connection`, not `ServerSession`. A `ServerSession` is
built fresh for every inbound message and is garbage the moment its handler
returns; the `Connection` is the thing that lives as long as the client is
attached and owns the standalone channel a notification travels on. Reaching it
means one private attribute, `ctx.session._connection`, because the middleware
seam hands out `ServerRequestContext` rather than the `Context` that exposes
`connection` publicly. `tests/test_live_tools.py` asserts a real frame arrives
over a real handshake, so an SDK release that moves this fails there rather
than in somebody's client.

Both are best-effort by construction. A notification travels on a channel the
client opened; a legacy client that never opens the GET stream cannot be told,
and the SDK drops the message with a log line rather than raising. That is the
right shape for this: the tool list is already correct, and the notification
only saves the client from finding out at its next `tools/list`.

The register holds **weak** references. A session outlives no more than its
connection, and this must not be the thing that keeps a disconnected client's
objects alive for the life of the daemon.

Python frees an object once nothing refers to it any more. A *weak* reference
does not count: a ``WeakSet`` can see its members while something else holds
them, and they vanish from it by themselves once that something else lets go.
Holding connections in an ordinary set would mean every client that ever
attached stayed in memory until the daemon exited.
"""

from __future__ import annotations

import weakref


class Clients:
    """The sessions currently attached, and how to tell them something changed."""

    def __init__(self, log) -> None:
        """Start with nothing attached. ``log`` is the daemon's logger."""
        self._log = log
        self._connections: weakref.WeakSet = weakref.WeakSet()

    async def observe(self, ctx, call_next):
        """Server middleware: remember the connection, then get out of the way.

        Runs for every inbound request and notification, before validation and
        before the handshake gate, so a client is registered from its very first
        message rather than from its first tool call.
        """
        # ``getattr(obj, name, default)`` reads an attribute without raising when
        # it is absent. Nested twice because either half may be missing on an
        # SDK object this code does not own, and a middleware that raised would
        # take the request down with it.
        connection = getattr(getattr(ctx, "session", None), "_connection", None)
        if connection is not None:
            self._connections.add(connection)
        return await call_next(ctx)

    @property
    def count(self) -> int:
        """How many clients could be reached. Weak, so it shrinks on its own."""
        return len(self._connections)

    async def tools_changed(self) -> None:
        """Tell every attached legacy session the tool list moved.

        A send that fails means that client is gone or has no back-channel open.
        Neither is this daemon's problem to fix, and neither may be allowed to
        interrupt a reload: the configuration has already been applied, and the
        client will see the new list when it next asks.
        """
        # ``list(...)`` copies first: the loop discards from the set as it goes,
        # and mutating a collection while iterating it raises.
        for connection in list(self._connections):
            if not getattr(connection, "has_standalone_channel", True):
                # A client that never opened the back-channel cannot be told.
                continue
            try:
                await connection.send_tool_list_changed()
            except Exception as exc:
                self._log.debug("could not notify a client (%s); dropping it", exc)
                self._connections.discard(connection)
