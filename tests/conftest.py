import json
import pathlib
import sys

import pytest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "src"))

FIXTURES = pathlib.Path(__file__).parent / "fixtures"


@pytest.fixture(scope="session")
def registry_payload() -> str:
    """A snapshot of `omarchy commands --all --json` from a real Omarchy.

    Tests read this instead of the live system so they run anywhere, including
    CI, and do not change meaning when Omarchy is upgraded.
    """
    return (FIXTURES / "commands.json").read_text()


@pytest.fixture(scope="session")
def commands(registry_payload):
    from omarchy_mcp.registry import _parse

    return _parse(registry_payload)


@pytest.fixture(scope="session")
def ipc_listing() -> str:
    return (FIXTURES / "ipc-show.txt").read_text()


@pytest.fixture
def live_registry_groups(registry_payload):
    return {c["group"] for c in json.loads(registry_payload)["commands"]}


@pytest.fixture
def anyio_backend():
    """The MCP SDK is anyio-based; the async tool tests run on asyncio."""
    return "asyncio"
