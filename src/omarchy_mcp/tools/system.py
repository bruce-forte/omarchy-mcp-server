"""One call for the questions that otherwise take five.

`omarchy_run` can fetch any of these individually. Asking five times to answer
"how is this machine doing" is five round trips through a language model, which
is the one thing worth spending a tool slot to avoid.
"""

from __future__ import annotations

import json
from concurrent.futures import ThreadPoolExecutor

from mcp.types import ToolAnnotations

from .. import execute
from ..config import Config
from ..stats import Stats

#: Each is cheap, read-only, and needs no arguments. Run together, they are the
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


def _parse(raw: str) -> object:
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


def register(mcp, config: Config, log, stats: Stats) -> None:
    if "omarchy_system_status" in set(config.disabled_tools):
        return

    @mcp.tool(
        name="omarchy_system_status",
        title="How is this machine doing",
        description=(
            "Report CPU and memory, battery, network, current theme and background, "
            "idle state, font, and focused monitor, in a single call. Every field is "
            "also available individually through omarchy_run; this exists so that "
            "answering a question about the machine does not take eight round trips. "
            "A field is null when the machine has no such thing, such as battery on a "
            "desktop."
        ),
        annotations=ToolAnnotations(
            readOnlyHint=True, destructiveHint=False, idempotentHint=True, openWorldHint=False
        ),
    )
    def omarchy_system_status() -> str:
        stats.record("omarchy_system_status")

        def probe(item):
            name, argv = item
            result = execute.run(
                argv, timeout_ms=PROBE_TIMEOUT_MS, max_output_b=PROBE_OUTPUT_B
            )
            if result.exit_code != 0:
                # A non-zero exit here usually means "not applicable" -- no
                # battery, no wifi -- rather than a failure worth reporting.
                return name, None
            return name, _parse(result.stdout)

        # Sequentially this is eight subprocess round trips; the probes are
        # independent and each is dominated by process startup.
        with ThreadPoolExecutor(max_workers=len(PROBES)) as pool:
            results = dict(pool.map(probe, PROBES.items()))

        return json.dumps(results, indent=2)
