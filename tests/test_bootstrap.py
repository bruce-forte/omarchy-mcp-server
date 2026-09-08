"""The bootstrap wrapper.

It is bash, so it gets read rather than imported. These pin the properties that
are invisible until something has already gone wrong on a live desktop.
"""

from __future__ import annotations

import pathlib
import re

import pytest

WRAPPER = pathlib.Path(__file__).resolve().parents[1] / "bin" / "omarchy-mcpd"


@pytest.fixture(scope="module")
def wrapper() -> str:
    return WRAPPER.read_text()


def test_is_executable():
    assert WRAPPER.stat().st_mode & 0o111


def test_sets_pycache_prefix_outside_the_plugin(wrapper):
    """Python writes __pycache__ beside the source it imports. The source is in
    the plugin directory, which Omarchy watches and reloads the shell on any
    write to -- so without a cache prefix the daemon makes the shell reload
    itself simply by starting."""
    assert "PYTHONPYCACHEPREFIX=" in wrapper
    found = re.search(r'PYTHONPYCACHEPREFIX="([^"]+)"', wrapper)
    assert found is not None
    prefix = found.group(1)
    assert "$STATE_DIR" in prefix
    assert "PLUGIN_DIR" not in prefix


def test_runs_from_source_rather_than_installing(wrapper):
    """An editable install writes build artifacts into the plugin directory,
    for the same reason as above."""
    assert "PYTHONPATH=" in wrapper
    assert "--no-install-project" in wrapper


def test_venv_is_built_in_the_state_directory(wrapper):
    assert 'UV_PROJECT_ENVIRONMENT="$VENV"' in wrapper
    assert 'VENV="$STATE_DIR/venv"' in wrapper


def test_uses_the_system_interpreter_by_absolute_path(wrapper):
    """`python3` on PATH is frequently a mise, brew, or conda shim, and a venv
    built against one stops working when that shim moves."""
    assert 'SYSTEM_PYTHON="/usr/bin/python3"' in wrapper
    assert '--python "$SYSTEM_PYTHON"' in wrapper


def test_uv_download_is_pinned_and_verified(wrapper):
    assert re.search(r'UV_VERSION="\d+\.\d+\.\d+"', wrapper)
    assert "sha256sum -c" in wrapper


def test_config_is_never_overwritten(wrapper):
    assert "[[ -f $CONFIG_DIR/config.toml ]] && return 0" in wrapper


def test_failures_notify(wrapper):
    """The caller is a QML service and the user is looking at a desktop, so a
    failure that is only logged is a failure nobody sees."""
    assert "omarchy notification send" in wrapper


#: Commands whose last argument is where they write.
WRITES_TO_LAST_ARG = ("cp", "mv", "install", "tee")
#: Commands where every argument is a target.
WRITES_TO_ANY_ARG = ("mkdir", "touch", "rm", "rmdir", "truncate")


def test_nothing_writes_into_the_plugin_directory(wrapper):
    """PLUGIN_DIR may be read from, never written to.

    Omarchy watches the plugin directory and reloads the shell on any write, so
    a write here is not untidiness -- it makes the shell restart the daemon that
    just wrote it.
    """
    offenders = []
    for line in wrapper.splitlines():
        stripped = line.strip()
        if stripped.startswith("#") or "PLUGIN_DIR" not in stripped:
            continue

        # A redirection into the plugin directory.
        if re.search(r'>>?\s*"?\$\{?PLUGIN_DIR', stripped):
            offenders.append(stripped)
            continue

        words = stripped.split()
        if not words:
            continue
        command = words[0]
        if command in WRITES_TO_ANY_ARG and any("$PLUGIN_DIR" in w for w in words[1:]):
            offenders.append(stripped)
        elif command in WRITES_TO_LAST_ARG and "$PLUGIN_DIR" in words[-1]:
            offenders.append(stripped)

    assert not offenders, f"these write into the plugin directory: {offenders}"


def test_reading_from_the_plugin_directory_is_fine(wrapper):
    """Guards the guard: the config template is copied out of the plugin
    directory, and that must not be mistaken for a write into it."""
    assert 'cp "$PLUGIN_DIR/config.example.toml" "$CONFIG_DIR/config.toml"' in wrapper
