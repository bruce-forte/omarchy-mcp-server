"""System status, gathered once and shared.

Both the `omarchy_system_status` tool and the `omarchy://system/status` resource
answer the same question, so they answer it with the same code.
"""

from __future__ import annotations

import json
from concurrent.futures import ThreadPoolExecutor

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


def parse(raw: str) -> object:
    """Omarchy's status commands emit JSON, tab-separated pairs, or bare text."""
    text = raw.strip()
    if not text:
        return None

    if text[0] in "{[":
        try:
            return json.loads(text)
        except json.JSONDecodeError:
            pass

    lines = [line for line in text.splitlines() if line.strip()]
    if all("\t" in line for line in lines):
        pairs = {}
        for line in lines:
            key, _, value = line.partition("\t")
            pairs[key.strip()] = value.strip()
        return pairs

    return text if len(lines) > 1 else lines[0]


def gather() -> dict[str, object]:
    """Run every probe. A field is null when the machine has no such thing."""

    def probe(item):
        name, argv = item
        result = execute.run(argv, timeout_ms=PROBE_TIMEOUT_MS, max_output_b=PROBE_OUTPUT_B)
        if result.exit_code != 0:
            # A non-zero exit here usually means "not applicable" -- no battery,
            # no wifi -- rather than a failure worth reporting.
            return name, None
        return name, parse(result.stdout)

    # Sequentially this is eight process startups; the probes are independent
    # and each is dominated by that startup.
    with ThreadPoolExecutor(max_workers=len(PROBES)) as pool:
        return dict(pool.map(probe, PROBES.items()))
