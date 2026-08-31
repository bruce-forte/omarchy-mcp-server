"""Every tool is async now, so nothing gets a worker thread for free.

`func_metadata` runs a *sync* tool through `anyio.to_thread.run_sync`. The
moment a tool becomes `async def` -- which N4 forced, because asking the user is
something to await -- that stops happening, and a blocking body runs on the
event loop instead. OCR is a thirty-second subprocess and a parked approval
prompt is meant to overlap with other clients' work, so this is the property the
refactor exists for rather than an implementation detail.
"""

from __future__ import annotations

import inspect
import time

import anyio
import pytest

from omarchy_mcp.tools._shared import threaded


@pytest.mark.anyio
async def test_a_threaded_body_does_not_stall_the_event_loop():
    """A blocking tool body must not stop other tasks from running.

    Written as a race rather than a timing assertion: the ticker only gets to
    run at all if the sleeping body is off the loop.
    """
    ticks = 0

    @threaded
    def slow_tool() -> str:
        time.sleep(0.3)
        return "done"

    async def tick():
        nonlocal ticks
        while True:
            await anyio.sleep(0.01)
            ticks += 1

    async with anyio.create_task_group() as tg:
        tg.start_soon(tick)
        assert await slow_tool() == "done"
        tg.cancel_scope.cancel()

    assert ticks > 5, "the loop was blocked while the tool body ran"


def test_the_wrapper_keeps_the_signature_the_schema_is_built_from():
    """`@mcp.tool` reads the wrapped function, so the schema must survive.

    TOOLS.md's staleness gate would also catch this, but it would report it as a
    documentation problem rather than as the tool losing its arguments.
    """

    @threaded
    def tool(action: str = "current", name: str = "") -> str:
        return action + name

    assert inspect.iscoroutinefunction(tool), "the SDK must not thread it a second time"
    params = inspect.signature(tool).parameters
    assert list(params) == ["action", "name"]
    assert params["action"].default == "current"
    # `from __future__ import annotations` makes these strings, which is what
    # the SDK resolves; the point is that the annotation survives at all.
    assert inspect.signature(tool).return_annotation == "str"
