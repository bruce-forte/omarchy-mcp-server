"""The suite must not be able to drive the machine it runs on.

This file exists because it once could. Three tests used `omarchy system reboot`
as their example of a guarded route, on the reasonable assumption that a guarded
route is refused and nothing happens. Changing what "guarded" defaults to turned
that refusal into a real approval notification on a real desktop; it was clicked,
in good faith, and the machine rebooted in the middle of the run.

So the guards in `conftest.py` are themselves tested. A guard nobody exercises is
a guard that quietly stops working on the next refactor, and this one is only
noticed when it is already too late.
"""

from __future__ import annotations

import pytest

from omarchy_mcp import execute, prompt


class TestNothingReachesTheRealDesktop:
    @pytest.mark.parametrize(
        "argv",
        [
            ["omarchy", "system", "reboot"],
            ["omarchy", "system", "shutdown"],
            ["omarchy", "theme", "set", "tokyo-night"],
            ["omarchy-shell", "io.github.bruce-forte.mcp-server", "stop"],
            ["hyprctl", "dispatch", "exit"],
        ],
    )
    def test_spawning_the_live_system_fails_loudly(self, argv):
        with pytest.raises(AssertionError) as exc:
            execute.run(argv, timeout_ms=1000, max_output_b=64)
        assert "real desktop" in str(exc.value)
        assert " ".join(argv) in str(exc.value), "the refusal names what was attempted"

    def test_a_redirected_resolver_is_left_alone(self, tmp_path, monkeypatch):
        """A test building its own fake `omarchy` is doing the right thing, and
        the guard must not be the reason it cannot."""
        binary = tmp_path / "omarchy"
        binary.write_text("#!/bin/sh\necho fake\n")
        binary.chmod(0o755)
        monkeypatch.setattr(execute, "SEARCH", (tmp_path,))

        result = execute.run(["omarchy", "whatever"], timeout_ms=5000, max_output_b=4096)
        assert result.stdout.strip() == "fake"

    def test_ordinary_binaries_still_run(self):
        """`test_execute.py` proves argv never reaches a shell by actually
        spawning things. That has to keep working."""
        result = execute.run(["echo", "hi"], timeout_ms=5000, max_output_b=4096)
        assert result.exit_code == 0
        assert result.stdout.strip() == "hi"


class TestNothingAsksAPerson:
    def test_a_notification_is_not_raised(self):
        """A notification the suite raised is a machine only pretending to ask,
        and answering it is how a human ends up inside a test run."""
        assert prompt.send("Approval needed", "body", token="tok") is None
        assert prompt.dismiss("(#1)") is None
