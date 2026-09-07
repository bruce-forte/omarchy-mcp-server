import json
import pathlib
import sys

import pytest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "src"))

from omarchy_mcp import execute, prompt  # noqa: E402  -- after the path insert

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


#: Every module-level binding of a path the daemon writes to, and the module it
#: is bound in. Bindings rather than one constant, because
#: `from .paths import X` copies the value at import: patching `paths.X` after
#: that reaches nobody. `test_conftest_guards.py` fails if a new one appears.
WRITTEN_PATHS = (
    ("omarchy_mcp.activity", "STATE_DIR", "state"),
    ("omarchy_mcp.__main__", "REGISTRY_SEEN_FILE", "registry-seen.json"),
    ("omarchy_mcp.reload", "REGISTRY_SEEN_FILE", "registry-seen.json"),
    ("omarchy_mcp.prompt", "CONSENT_DIR", "consent"),
    ("omarchy_mcp.reload", "CONSENT_DIR", "consent"),
    # The bearer token. `main()` writes it, and `test_shutdown` calls `main()`:
    # unpinned, the suite would rotate the token the user's clients are using.
    ("omarchy_mcp.token", "STATE_DIR", "token-dir"),
    ("omarchy_mcp.token", "TOKEN_FILE", "token-dir/token"),
)


@pytest.fixture(autouse=True)
def _pin_state_dir(monkeypatch, tmp_path_factory):
    """No test writes into the real state directory.

    Same reasoning as `_pin_registry`, one layer down: a test that drives the
    daemon end to end -- `test_shutdown` calls `main()` -- would otherwise
    append to the user's own activity log, on their own machine, every time the
    suite ran. Caught only because the file appeared there.

    It happened again with `registry-seen.json`, which is why this now pins
    every such path rather than the one that had already gone wrong. Pinning is
    per *binding*: `from .paths import REGISTRY_SEEN_FILE` copies the value at
    import time, so patching `paths` would have reached nobody.
    """
    import importlib

    root = tmp_path_factory.mktemp("state")
    for module_name, attribute, leaf in WRITTEN_PATHS:
        module = importlib.import_module(module_name)
        if not hasattr(module, attribute):
            continue
        monkeypatch.setattr(module, attribute, root / leaf)


#: What `execute.SEARCH` is when nobody has redirected it: the real Omarchy.
#: Captured at import so `_no_real_omarchy` can tell "this test pointed the
#: resolver at a fixture directory" from "this test is about to drive the
#: machine the suite is running on".
REAL_SEARCH = tuple(execute.SEARCH)

#: Spawning either of these against the live system reaches the desktop the
#: suite is running on. `omarchy system reboot` is the one that taught us.
DESKTOP_BINARIES = frozenset({"omarchy", "omarchy-shell", "hyprctl", "qs", "wl-copy"})


@pytest.fixture(autouse=True)
def _no_real_omarchy(monkeypatch, request):
    """No test drives the developer's own desktop.

    This exists because the suite once did. Three tests used
    `omarchy system reboot` as their example of a guarded route, on the
    reasonable assumption that a guarded route is refused and nothing happens.
    A change to what "guarded" defaults to turned that refusal into a real
    approval notification on a real desktop; it was clicked, in good faith, and
    the machine rebooted mid-run.

    The lesson is not "pick a gentler route" -- it is that a test suite must not
    be one behaviour change away from executing whatever it names. So the
    resolver is the thing that is guarded, not the route:

    - a test that redirects `execute.SEARCH` at a fixture directory is building
      its own fake binary and is left alone
    - a test marked `needs_omarchy` has declared that it reads the installed
      system, which is the existing opt-in for exactly this
    - anything else spawning `omarchy` and friends fails loudly, naming the argv

    `test_execute.py` and `test_binaries.py` keep spawning `echo`, `sleep` and
    their own fakes, which is what they are for.
    """
    if request.node.get_closest_marker("needs_omarchy") is not None:
        # Declared drift checks against the installed Omarchy. They read; they
        # are skipped in CI; and the marker is the opt-in.
        return

    real_run = execute.run

    def guarded(argv, **kwargs):
        if tuple(execute.SEARCH) == REAL_SEARCH and argv and argv[0] in DESKTOP_BINARIES:
            raise AssertionError(
                "a test tried to run this against the real desktop: "
                + " ".join(map(str, argv))
                + ". Mock it, or point execute.SEARCH at a fixture directory. "
                "Nothing in the suite may reach the machine it runs on."
            )
        return real_run(argv, **kwargs)

    monkeypatch.setattr(execute, "run", guarded)


@pytest.fixture(autouse=True)
def _no_desktop_prompts(monkeypatch):
    """No test puts a question in front of a person.

    A notification the suite raised is a machine only pretending to ask, and
    answering it is how a human ends up inside a test run. Tests that care what
    was asked patch these themselves and see their own recorder instead.
    """
    monkeypatch.setattr(prompt, "send", lambda *a, **k: None)
    monkeypatch.setattr(prompt, "dismiss", lambda *a, **k: None)


@pytest.fixture(scope="session")
def themes() -> list[str]:
    """A snapshot of `omarchy theme list`."""
    return [line.strip() for line in (FIXTURES / "themes.txt").read_text().splitlines() if line.strip()]


@pytest.fixture(scope="session")
def monitors() -> list[dict]:
    """A snapshot of `hyprctl -j monitors`, trimmed to the fields anything reads.

    Two monitors, because one cannot show that the wrong name is refused rather
    than quietly taken as the only candidate. The serial number is dropped: it
    identifies a physical panel and nothing here needs it.
    """
    return json.loads((FIXTURES / "monitors.json").read_text())


@pytest.fixture(autouse=True)
def _pin_resolver_sources(monkeypatch, themes, monitors):
    """Resolution reads the committed snapshots, never this machine.

    Same reasoning as `_pin_registry`: CI has neither an Omarchy nor a
    compositor, and locally the suite would otherwise assert against whichever
    themes happen to be installed today.
    """
    import omarchy_mcp.resolve as resolve

    monkeypatch.setattr(resolve, "_themes", lambda: list(themes))
    monkeypatch.setattr(resolve, "_monitors", lambda: list(monitors))


@pytest.fixture
def live_registry_groups(registry_payload):
    return {c["group"] for c in json.loads(registry_payload)["commands"]}


def pytest_configure(config):
    config.addinivalue_line("markers", "needs_omarchy: reads the installed Omarchy, not the fixture")


@pytest.fixture
def anyio_backend():
    """The MCP SDK is anyio-based; the async tool tests run on asyncio."""
    return "asyncio"
