"""Which executables this daemon will run.

`trust.py` is a security boundary, so these are the specification for it. The
marketplace review found the hole they exist to close: the daemon inherited a
session `PATH`, and on the machine this was written on that `PATH` resolved
`curl` to a binary in a directory the user could write -- reached by the
bootstrap before any token, tier or consent prompt existed. See ROADMAP N20.

Nothing here reaches the desktop. The trusted cases read files the system
already has, and the refusals are built in a temporary directory.
"""

from __future__ import annotations

import os
import re
import shlex
import subprocess
from pathlib import Path

import pytest

from omarchy_mcp import execute, trust

#: A binary every system has, in the last directory of the allowlist.
SYSTEM_BINARY = Path("/usr/bin/true")


def _a_root_owned_symlink() -> Path | None:
    """Any root-owned symlink in /usr/bin, or None.

    Found rather than created: a test cannot make a root-owned anything, and
    the case matters -- `/usr/share/omarchy/bin/omarchy` is a symlink, so
    refusing links outright would refuse a healthy Omarchy.
    """
    for entry in sorted(Path("/usr/bin").iterdir()):
        try:
            if entry.is_symlink() and entry.lstat().st_uid == 0 and entry.resolve().is_file():
                return entry
        except OSError:
            continue
    return None


class TestWhatIsTrusted:
    def test_a_root_owned_system_binary_verifies(self):
        """Guards the guards: if this failed, every refusal below would pass
        for the wrong reason."""
        assert trust.verify(SYSTEM_BINARY) == str(SYSTEM_BINARY)

    def test_a_root_owned_symlink_is_followed_not_refused(self):
        link = _a_root_owned_symlink()
        if link is None:
            pytest.skip("no root-owned symlink in /usr/bin on this machine")
        assert trust.verify(link) == str(link)

    def test_the_allowlist_is_what_the_daemon_searches(self):
        """`execute.SEARCH` is an alias, so the list and the rule that guards it
        cannot drift apart."""
        assert tuple(execute.SEARCH) == tuple(trust.TRUSTED_DIRS)
        assert str(trust.TRUSTED_DIRS[-1]) == "/usr/bin"


class TestWhatIsRefused:
    def test_a_file_the_user_owns_is_refused(self, tmp_path):
        """The whole point. A binary this user can rewrite is a binary that
        says nothing about what will run.

        The reason reported is about the *directory*, not the file: `tmp_path`
        lives under `/tmp`, which is mode 1777, and the chain is walked from
        `/` downwards so the first thing wrong is the first thing said. Either
        refusal is correct and the file is equally not executed -- the rule
        about the file's own owner is pinned separately below, where a clean
        chain makes it the reason that fires.
        """
        mine = tmp_path / "curl"
        mine.write_text("#!/bin/sh\nexit 0\n")
        mine.chmod(0o755)
        with pytest.raises(trust.NotTrusted):
            trust.verify(mine)

    def test_a_file_not_owned_by_root_is_refused_on_its_own_account(self, tmp_path):
        """The leaf rule, reached directly.

        A test cannot build a root-owned directory to put a user-owned file in,
        so the chain check would always speak first. This asks the rule about
        the file itself, which is the one that refuses a user-writable binary
        sitting in an otherwise perfectly ordinary `/usr/local/bin`.
        """
        mine = tmp_path / "grim"
        mine.write_text("#!/bin/sh\nexit 0\n")
        mine.chmod(0o755)
        with pytest.raises(trust.NotTrusted) as caught:
            trust._check_leaf(mine)
        assert "not root" in str(caught.value)

    def test_a_relative_path_is_refused(self):
        with pytest.raises(trust.NotTrusted):
            trust.verify("usr/bin/true")

    def test_a_directory_is_not_an_executable(self, tmp_path):
        with pytest.raises(trust.NotTrusted):
            trust.verify(tmp_path)

    def test_something_that_is_not_there_is_refused(self):
        """As a refusal, not as an OSError.

        `resolve_binary` verifies an absolute path a caller supplied, so this
        is reachable with a name that points at nothing. Letting
        `FileNotFoundError` escape would carry it past every caller that knows
        how to explain a refusal.
        """
        with pytest.raises(trust.NotTrusted) as caught:
            trust.verify("/usr/bin/definitely-not-installed-here")
        assert "cannot be inspected" in str(caught.value)

    def test_the_refusal_names_the_file_and_the_reason(self, tmp_path):
        """A bootstrap that fails closed with no explanation reads exactly like
        a missing dependency, and sends the reader to their package manager."""
        mine = tmp_path / "thing"
        mine.write_text("")
        mine.chmod(0o755)
        with pytest.raises(trust.NotTrusted) as caught:
            trust._check_leaf(mine)
        assert str(mine) in str(caught.value)
        assert "refusing to execute" in str(caught.value)


class TestResolution:
    def test_path_is_never_consulted(self, tmp_path, monkeypatch):
        """A shim directory at the front of PATH must not decide anything."""
        shim = tmp_path / "shims"
        shim.mkdir()
        (shim / "true").write_text("#!/bin/sh\nexit 1\n")
        (shim / "true").chmod(0o755)
        monkeypatch.setenv("PATH", str(shim) + os.pathsep + os.environ["PATH"])
        assert trust.resolve("true", trust.TRUSTED_DIRS) == str(SYSTEM_BINARY)

    def test_an_untrusted_candidate_is_skipped_and_the_search_continues(self, tmp_path):
        """Failing closed does not mean giving up: the untrusted file is never
        run, and the trusted one behind it still is. This is what lets a
        dev-linked OMARCHY_PATH under $HOME sit at the front of the list."""
        shadow = tmp_path / "shadow"
        shadow.mkdir()
        (shadow / "true").write_text("#!/bin/sh\nexit 1\n")
        (shadow / "true").chmod(0o755)
        assert trust.resolve("true", (shadow, Path("/usr/bin"))) == str(SYSTEM_BINARY)

    def test_nothing_trusted_anywhere_resolves_to_nothing(self, tmp_path):
        assert trust.resolve("true", (tmp_path,)) is None

    def test_a_refusal_is_explained_rather_than_reported_as_missing(self, tmp_path):
        """"Not installed" sends somebody to their package manager. "Found but
        not trusted" sends them to look at who owns a file."""
        shadow = tmp_path / "shadow"
        shadow.mkdir()
        (shadow / "grim").write_text("#!/bin/sh\nexit 0\n")
        (shadow / "grim").chmod(0o755)
        reasons = trust.describe("grim", (shadow,))
        assert len(reasons) == 1
        # Which component gets named depends on where this test directory
        # happens to live -- under `/tmp` the chain is wrong before the file is
        # reached, so the message is about `/tmp`. What is being pinned here is
        # that a refusal produces an explanation at all, against the case below
        # where a genuinely absent dependency produces none. Which rule fires is
        # pinned where the chain is clean enough for it to be the only one.
        assert reasons[0].startswith("refusing to execute ")
        assert ": " in reasons[0], "a refusal has to say why, not just that"

    def test_an_ordinary_missing_dependency_explains_nothing(self, tmp_path):
        assert trust.describe("nothing-by-this-name", (tmp_path,)) == []


#: Variables that decide what a program does before its first instruction.
HOSTILE_ENV = {
    "BASH_ENV": "/tmp/evil.sh",
    "ENV": "/tmp/evil.sh",
    "LD_PRELOAD": "/tmp/evil.so",
    "LD_LIBRARY_PATH": "/tmp",
    "PYTHONHOME": "/tmp",
    "PYTHONSTARTUP": "/tmp/evil.py",
    "PYTHONUSERBASE": "/tmp",
}

#: Variables the desktop helpers genuinely need. An allowlist that forgets these
#: is not strict, it is broken: `hyprctl` cannot find its socket without the
#: signature and the Wayland tools cannot find the display.
REQUIRED_ENV = {
    "HYPRLAND_INSTANCE_SIGNATURE": "sig",
    "WAYLAND_DISPLAY": "wayland-1",
    "DISPLAY": ":0",
    "DBUS_SESSION_BUS_ADDRESS": "unix:path=/run/user/1000/bus",
    "XDG_RUNTIME_DIR": "/run/user/1000",
}


class TestTheChildEnvironment:
    def test_path_is_replaced_rather_than_prepended(self, monkeypatch):
        """A session PATH left on the end is a second list that was never
        checked, one process along."""
        monkeypatch.setenv("PATH", "/home/someone/.local/bin")
        env = trust.child_env()
        assert env["PATH"] == os.pathsep.join(str(d) for d in trust.TRUSTED_DIRS)
        assert "/home/someone/.local/bin" not in env["PATH"]

    def test_an_override_still_wins_for_other_keys(self, monkeypatch):
        env = trust.child_env({"OMARCHY_MCP_TEST": "1"})
        assert env["OMARCHY_MCP_TEST"] == "1"

    @pytest.mark.parametrize("name", sorted(HOSTILE_ENV))
    def test_a_variable_that_runs_code_is_not_passed_on(self, name, monkeypatch):
        """BASH_ENV is sourced by bash before the body of any script it runs --
        and `/usr/bin/omarchy` is a bash script, so this reaches every `omarchy`
        call. LD_PRELOAD is mapped into every ELF helper before `main`.
        PYTHONHOME relocates an interpreter's standard library. See N21."""
        monkeypatch.setenv(name, HOSTILE_ENV[name])
        assert name not in trust.child_env()

    @pytest.mark.parametrize("name", sorted(REQUIRED_ENV))
    def test_what_the_desktop_helpers_need_survives(self, name, monkeypatch):
        monkeypatch.setenv(name, REQUIRED_ENV[name])
        assert trust.child_env()[name] == REQUIRED_ENV[name]

    def test_an_unlisted_variable_is_dropped(self, monkeypatch):
        """An allowlist, not a denylist: the list of ways to influence a program
        through its environment is not one anybody finishes writing."""
        monkeypatch.setenv("SOMETHING_NOBODY_LISTED", "1")
        assert "SOMETHING_NOBODY_LISTED" not in trust.child_env()

    def test_locale_survives_by_pattern(self, monkeypatch):
        monkeypatch.setenv("LC_TIME", "en_GB.UTF-8")
        assert trust.child_env()["LC_TIME"] == "en_GB.UTF-8"


class TestTheBashSanitiser:
    """The same rule, in the language the bootstrap is written in."""

    def test_it_drops_the_unlisted_and_keeps_the_needed(self):
        library = Path(__file__).resolve().parents[1] / "bin" / "omarchy-mcp-trust"
        script = f"""
          export EVIL=1 LD_PRELOAD=/tmp/x.so BASH_ENV=/tmp/e.sh PYTHONHOME=/tmp
          export HOME=/home/u WAYLAND_DISPLAY=wayland-1 LC_ALL=C PATH=/usr/bin
          export HYPRLAND_INSTANCE_SIGNATURE=sig
          source {shlex.quote(str(library))}
          trust_sanitize_env
          compgen -e | sort
        """
        result = subprocess.run(
            ["bash", "-c", script],
            capture_output=True,
            text=True,
            env={"PATH": "/usr/bin:/bin"},
        )
        survivors = set(result.stdout.split())
        assert not survivors & {"EVIL", "LD_PRELOAD", "BASH_ENV", "PYTHONHOME"}
        needed = {
            "HOME",
            "WAYLAND_DISPLAY",
            "HYPRLAND_INSTANCE_SIGNATURE",
            "LC_ALL",
            "PATH",
        }
        assert needed <= survivors


class TestTheQmlBoundary:
    """`Service.qml` is where clearing has to happen.

    A wrapper cannot defend itself against `BASH_ENV` -- bash has already
    sourced it by the time the script's first line runs -- so the parent is the
    only place that can stop it. These read the file because there is no test
    harness for QML here, and an unguarded `Process` would be silent.
    """

    @staticmethod
    def _service() -> str:
        return (Path(__file__).resolve().parents[1] / "Service.qml").read_text()

    def test_every_process_clears_and_rebuilds_the_environment(self):
        body = self._service()
        blocks = len(re.findall(r"^\s*Process\s*\{\s*$", body, re.M))
        assert blocks > 0, "no Process blocks found; this guard needs updating"
        assert body.count("clearEnvironment: true") >= blocks
        assert body.count("environment: root.childEnv") == blocks

    def test_the_rebuilt_environment_carries_the_desktop_variables(self):
        body = self._service()
        for name in ("WAYLAND_DISPLAY", "HYPRLAND_INSTANCE_SIGNATURE", "XDG_RUNTIME_DIR"):
            assert f'"{name}"' in body

    def test_the_session_path_is_not_handed_over(self):
        """PATH is a fixed floor; the wrappers replace it with the allowlist."""
        assert '"PATH": "/usr/local/bin:/usr/bin"' in self._service()


class TestWhatTheDaemonReports:
    def test_a_shadowed_binary_is_not_reported_as_missing(self, tmp_path):
        """`NotInstalled` carries the refusals, so the message distinguishes a
        dependency that is absent from one that was found and declined."""
        shadow = tmp_path / "shadow"
        shadow.mkdir()
        (shadow / "grim").write_text("#!/bin/sh\nexit 0\n")
        (shadow / "grim").chmod(0o755)
        exc = execute.NotInstalled("grim", (shadow,), trust.describe("grim", (shadow,)))
        assert "Refused:" in str(exc)
        assert exc.refused
