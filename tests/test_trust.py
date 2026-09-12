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
