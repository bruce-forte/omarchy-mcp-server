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


class TestTheHelperIsTheOnlyWriter:
    """`bin/omarchy-mcp-consent` validates the token before using it as a
    filename and is the one place the answer vocabulary is defined. QML shells
    out to it rather than writing the file, so there is one thing to get right
    rather than two -- and the second would be in a language with no tests here."""

    def test_the_helper_and_the_daemon_agree_on_the_verbs(self):
        import pathlib
        import re

        from omarchy_mcp import prompt

        helper = (pathlib.Path(__file__).resolve().parents[1] / "bin" / "omarchy-mcp-consent")
        body = helper.read_text()
        # `approve` is the helper's spelling of the daemon's `once`: it writes
        # the bare token, which is what this channel has always meant.
        assert re.search(r"^\s*approve\)", body, re.M)
        for verb in prompt.VERBS:
            if verb == "once":
                continue
            assert re.search(rf"^\s*{verb}\)", body, re.M), f"{verb} is not a helper verb"

    def test_the_qml_answers_through_the_helper(self):
        import pathlib

        service = pathlib.Path(__file__).resolve().parents[1] / "Service.qml"
        body = service.read_text()
        assert "bin/omarchy-mcp-consent" in body
        assert "CONSENT_DIR" not in body, "QML must not write the answer file itself"

    def test_the_token_never_reaches_the_state_file(self):
        """It is a live capability for the length of one question. The state
        file lands under $XDG_RUNTIME_DIR and the panel is in screenshots."""
        import pathlib
        import re

        service = pathlib.Path(__file__).resolve().parents[1] / "Service.qml"
        body = service.read_text()
        write_state = re.search(r"function writeState\(\) \{.*?\n  \}", body, re.S)
        assert write_state, "writeState() moved; this guard needs updating"
        assert "Token" not in write_state.group(0)
        assert "token" not in write_state.group(0)


def test_nothing_binds_all_commands_at_import():
    """`_pin_registry` patches `registry.all_commands`, so a module that did
    `from .registry import all_commands` keeps the real one and reads the
    installed Omarchy in every test -- silently, and only on a machine that has
    one. `__main__.py` did exactly that, and its `--permissions` output was
    computed from the live system while the assertions used the fixture."""
    import pathlib
    import re

    src = pathlib.Path(__file__).resolve().parents[1] / "src" / "omarchy_mcp"
    offenders = [
        path.name
        for path in sorted(src.rglob("*.py"))
        if re.search(r"^from \.{1,2}registry import .*\ball_commands\b", path.read_text(), re.M)
    ]
    assert offenders == [], (
        "call registry.all_commands() through the module so the fixture can pin it"
    )


def test_every_written_path_is_pinned():
    """`_pin_state_dir` patches bindings, not one constant, because
    `from .paths import X` copies the value at import. A module that starts
    writing somewhere new has to be added to `WRITTEN_PATHS` or the suite will
    write into the developer's own state directory -- which has now happened
    twice, once with the activity log and once with `registry-seen.json`."""
    import pathlib
    import re

    from .conftest import WRITTEN_PATHS

    pinned = {(module.rpartition(".")[2], attribute) for module, attribute, _ in WRITTEN_PATHS}
    src = pathlib.Path(__file__).resolve().parents[1] / "src" / "omarchy_mcp"
    # `ACTIVITY_FILE` is deliberately absent: it is a bare filename, joined onto
    # the state directory by its caller, so pinning it would pin nothing.
    written = (
        "STATE_DIR",
        "REGISTRY_SEEN_FILE",
        "CONSENT_DIR",
        "TOKEN_FILE",
        "PERMISSIONS_LOCAL_FILE",
    )

    missing = []
    for path in sorted(src.rglob("*.py")):
        if path.name == "paths.py":
            continue
        for match in re.finditer(r"^from \.{1,2}\w*\s+import\s+(.+)$", path.read_text(), re.M):
            for name in (n.strip() for n in match.group(1).split(",")):
                if name in written and (path.stem, name) not in pinned:
                    missing.append(f"{path.stem}.{name}")
    assert missing == [], (
        "add these to conftest.WRITTEN_PATHS so the suite cannot write to the real "
        f"state directory: {missing}"
    )


def test_the_notification_runs_a_summon_and_not_the_helper():
    """A click opens the panel; only a button answers. If these two ever agree
    again, a reflexive click on a toast grants something (F31)."""
    from omarchy_mcp import prompt
    from omarchy_mcp.paths import CONSENT_HELPER

    assert str(CONSENT_HELPER) not in " ".join(prompt.SUMMON)
    assert prompt.SUMMON[0] == "omarchy-shell"
    # `summon`, not `toggle`: a second click must not close the panel the first
    # one opened.
    assert "toggle" not in prompt.SUMMON
