"""MCP server exposing Omarchy to local agents.

Reading this package
--------------------

The daemon is a small HTTP server that a language model talks to, wrapped in
several layers of "may this actually happen?". Read the modules in roughly this
order; each one is a single idea:

===================  =======================================================
``paths.py``         where the plugin is allowed to put files
``config.py``        the user's ``config.toml``, parsed and frozen
``permissions.py``   the user's ``permissions.json``, parsed and frozen
``policy.py``        which tier a call falls into: allow / ask / blocked
``resolve.py``       turning a name in an argument into a real thing
``prompt.py``        asking the person at the desktop a yes/no question
``gate.py``          the four of those in order, for one call
``execute.py``       actually spawning a process, safely
``tools/``           the individual things an agent can ask for
``server.py``        wiring the above into an HTTP app
``__main__.py``      the command line: what runs when the daemon starts
===================  =======================================================

Python idioms used throughout
-----------------------------

If you are new to Python, these five turn up in nearly every file:

``from __future__ import annotations``
    Makes every type hint a string that is never evaluated at runtime. It is
    free, it lets a module mention a type it has not imported at runtime, and
    it removes any chance of a hint costing anything. It changes no behaviour;
    read past it.

``@dataclass``
    Generates ``__init__``, ``__repr__`` and ``__eq__`` from the attributes
    listed under the class, so the class body is just its fields. Adding
    ``frozen=True`` makes instances immutable -- assigning to a field raises --
    which is how the security boundary guarantees that a decision cannot be
    edited after it is made.

Type hints (``def f(x: str) -> bool``)
    Documentation that a checker can verify. Python itself ignores them
    entirely: nothing is enforced at runtime, and a wrong hint never raises.

``async def`` / ``await``
    An ``async def`` function does not run when called -- it returns a coroutine
    that a caller must ``await``. While it is awaiting, the single event-loop
    thread runs something else, which is how one client parked on an approval
    prompt does not freeze the others. Anything genuinely blocking (spawning a
    process, reading a file) is pushed onto a worker thread instead; see
    ``tools/_shared.py``'s ``offload``.

Decorators (``@something`` above a ``def``)
    A function that takes the function below it and returns a replacement. The
    tool registrations (``@tools.tool(...)``) and ``@threaded`` are both this.

One convention of this codebase's own: a comment starting ``#:`` documents the
constant on the *next* line, in the same way a docstring documents a function.
"""

__version__ = "0.1.0"
