"""Every curated tool this server knows how to offer, and which are on.

A tool used to be registered inside `if enabled(config, ...)`, so whether it
existed was decided by which decorators ran at startup and could not be revised
without building a new server. The catalogue separates the two questions:
`register()` declares what a tool *is*, `apply()` decides whether it is
*offered*.

The declaration is unchanged from the caller's side -- `@tools.tool(name=...)`
takes the same arguments as `@mcp.tool(...)` and returns the function untouched.
What changes is that nothing is registered at declaration time.

`apply()` is the only thing that ever adds or removes a tool, and it is called
the same way at startup and at reload, so there is no second code path for the
live case to drift from. It returns what changed, because two callers need it:
the reload decides whether to notify clients at all, and the bar widget shows
how many tools are on.

A disabled tool is *absent*, not refused: it is not in `tools/list`, and a
`tools/call` naming it gets the SDK's unknown-tool error. That is the property
the tool switch is for -- a tool the model cannot see is one prompt injection
cannot talk it into trying -- and refusing-but-present would give that away.

The Python idea underneath is that a decorator does not have to *do* anything
at the moment it runs. ``@tools.tool(...)`` records the function in a dict and
hands it back unchanged; `apply` reads that dict later. A decorator that
registered on the spot would tie the answer to import order forever.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from ..config import Config


@dataclass(frozen=True)
class Entry:
    """One declared tool: the function, and the arguments it is registered with."""

    name: str
    fn: Callable[..., Any]
    kwargs: dict[str, Any]


@dataclass(frozen=True)
class Change:
    """What one `apply()` did."""

    added: tuple[str, ...] = ()
    removed: tuple[str, ...] = ()

    def __bool__(self) -> bool:
        """Whether this apply changed anything, so ``if change:`` reads properly."""
        return bool(self.added or self.removed)


class Catalogue:
    """The declared tools, and the subset currently registered on a server."""

    def __init__(self) -> None:
        """An empty catalogue: nothing declared, nothing registered."""
        #: Every tool that exists, by name. Filled by the `tool` decorator.
        self._entries: dict[str, Entry] = {}
        #: The names currently registered on the server. A subset of the above.
        self._present: set[str] = set()

    def tool(self, **kwargs: Any) -> Callable[[Callable[..., Any]], Callable[..., Any]]:
        """Declare a tool. Same arguments as `MCPServer.tool`, same return."""

        # A decorator *factory*: ``tool(...)`` is called first and returns
        # ``decorate``, which Python then applies to the function underneath it.
        # That is what lets the decorator take arguments of its own.
        def decorate(fn: Callable[..., Any]) -> Callable[..., Any]:
            # ``fn.__name__`` is the function's own name, used when the caller
            # did not pass one.
            name = kwargs.get("name") or fn.__name__
            # ``dict(kwargs)`` copies, so a later edit by the caller cannot
            # change what this tool will be registered with.
            self._entries[name] = Entry(name, fn, dict(kwargs))
            # Returned unchanged: decorating a tool must not wrap it.
            return fn

        return decorate

    @property
    def declared(self) -> tuple[str, ...]:
        """Every tool that exists, on or off. Sorted, so it reads the same twice."""
        return tuple(sorted(self._entries))

    @property
    def present(self) -> tuple[str, ...]:
        """Every tool currently registered on the server."""
        return tuple(sorted(self._present))

    def wanted(self, config: Config) -> set[str]:
        """The tools this configuration asks for."""
        disabled = set(config.disabled_tools)
        # A set comprehension -- braces rather than brackets -- over the names
        # declared, minus the ones the config switched off.
        return {name for name in self._entries if name not in disabled}

    def apply(self, mcp, config: Config) -> Change:
        """Make the server offer exactly the tools this configuration asks for.

        Called once at startup and again on every reload. Tracking what is
        present here rather than reading it back off the server keeps this to
        the SDK's public surface, and this catalogue is the only thing that
        registers a tool on that server.
        """
        wanted = self.wanted(config)

        # Set difference both ways: present-but-unwanted comes off, wanted-but-
        # absent goes on, and a tool in both is left alone -- so re-registering
        # an unchanged tool never happens.
        removed = sorted(self._present - wanted)
        for name in removed:
            mcp.remove_tool(name)
            self._present.discard(name)

        added = sorted(wanted - self._present)
        for name in added:
            entry = self._entries[name]
            # ``**entry.kwargs`` spreads the stored dict back out into keyword
            # arguments -- the reverse of the ``**kwargs`` that collected them.
            mcp.add_tool(entry.fn, **entry.kwargs)
            self._present.add(name)

        return Change(added=tuple(added), removed=tuple(removed))
