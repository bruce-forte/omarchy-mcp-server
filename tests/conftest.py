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


@pytest.fixture(autouse=True)
def _pin_registry(monkeypatch, commands):
    """Every test reads the committed registry snapshot, never the installed one.

    Without this the suite silently depends on which Omarchy the machine has --
    it cannot run in CI at all, and locally it would change meaning the next
    time `omarchy update` renames something. Tests that care about drift
    between the snapshot and the live system say so explicitly.
    """
    import omarchy_mcp.registry as reg

    monkeypatch.setattr(reg, "all_commands", lambda: commands)


@pytest.fixture
def live_registry_groups(registry_payload):
    return {c["group"] for c in json.loads(registry_payload)["commands"]}


def pytest_configure(config):
    config.addinivalue_line("markers", "needs_omarchy: reads the installed Omarchy, not the fixture")


@pytest.fixture
def anyio_backend():
    """The MCP SDK is anyio-based; the async tool tests run on asyncio."""
    return "asyncio"
