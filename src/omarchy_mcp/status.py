"""System status, gathered once and shared.

Both the `omarchy_system_status` tool and the `omarchy://system/status` resource
answer the same question, so they answer it with the same code.
"""

from __future__ import annotations

import json
from concurrent.futures import ThreadPoolExecutor
from typing import Any

from . import execute

#: Each is cheap, read-only, and needs no arguments. Together they are the
#: answer to "what is the state of this machine".
PROBES: dict[str, list[str]] = {
    "cpu_and_memory": ["omarchy", "system", "stats"],
    "battery": ["omarchy", "battery", "status"],
    "network": ["omarchy", "network", "status"],
    "theme": ["omarchy", "theme", "current"],
    "background": ["omarchy", "theme", "bg", "current"],
    "idle": ["omarchy", "toggle", "idle", "status"],
    "font": ["omarchy", "font", "current"],
    "monitor": ["omarchy", "hyprland", "monitor", "focused"],
}

PROBE_TIMEOUT_MS = 8000
PROBE_OUTPUT_B = 16 * 1024


def parse(raw: str) -> Any:
    """Omarchy's status commands emit JSON, tab-separated pairs, or bare text.

    Returns whichever Python value fits: a dict or list from JSON, a dict from
    the pairs, a string otherwise, and ``None`` for no output at all. The return
    hint is ``Any`` -- "the checker cannot say" -- because all four are possible
    and the shape is only known to whoever asked for a particular probe.
    """
    text = raw.strip()
    if not text:
        return None

    # Cheap first guess at JSON: it can only start with an object or an array
    # here. Actually parsing is the confirmation.
    if text[0] in "{[":
        try:
            return json.loads(text)
        except json.JSONDecodeError:
            # Not JSON after all. Fall through to the other two shapes rather
            # than failing -- ``pass`` means "do nothing", not "ignore forever".
            pass

    # A list comprehension: the same as a for-loop appending to a list, with the
    # ``if`` acting as a filter. This one drops blank lines.
    lines = [line for line in text.splitlines() if line.strip()]
    # ``all(...)`` is True when every line qualifies -- and also when there are
    # no lines, which the empty check above has already ruled out.
    if all("\t" in line for line in lines):
        pairs = {}
        for line in lines:
            # ``partition`` splits on the *first* tab and always returns three
            # parts, so a value containing a tab survives intact. The middle one
            # is the separator itself, which is not wanted: ``_`` is the usual
            # name for "deliberately unused".
            key, _, value = line.partition("\t")
            pairs[key.strip()] = value.strip()
        return pairs

    # Multi-line prose comes back whole; a single line comes back without the
    # trailing newline the command emitted.
    return text if len(lines) > 1 else lines[0]


def gather() -> dict[str, Any]:
    """Run every probe. A field is null when the machine has no such thing."""

    # Defined inside ``gather`` because it exists only to be mapped over the
    # probes below, and because being nested lets it read ``PROBES`` and the
    # constants without them being passed in.
    def probe(item: tuple[str, list[str]]) -> tuple[str, Any]:
        # ``pool.map`` passes one argument, so the (name, argv) pair arrives as a
        # single tuple and is unpacked here.
        name, argv = item
        result = execute.run(argv, timeout_ms=PROBE_TIMEOUT_MS, max_output_b=PROBE_OUTPUT_B)
        if result.exit_code != 0:
            # A non-zero exit here usually means "not applicable" -- no battery,
            # no wifi -- rather than a failure worth reporting.
            return name, None
        return name, parse(result.stdout)

    # Sequentially this is eight process startups; the probes are independent
    # and each is dominated by that startup. One worker per probe means they all
    # run at once, and ``with`` waits for every one of them before leaving.
    with ThreadPoolExecutor(max_workers=len(PROBES)) as pool:
        # ``map`` calls ``probe`` once per item and yields the results in order;
        # each is a (name, value) pair, which is exactly what ``dict`` builds
        # from.
        return dict(pool.map(probe, PROBES.items()))
