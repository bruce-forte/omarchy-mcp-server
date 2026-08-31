"""Configuration never stops the daemon from starting."""

from __future__ import annotations

from omarchy_mcp import config as config_module
from omarchy_mcp.config import DEFAULT_PORT, Config, load


def test_missing_file_yields_defaults(tmp_path):
    cfg = load(tmp_path / "absent.toml")
    assert cfg == Config()
    assert cfg.problems == ()


def test_broken_toml_yields_defaults_and_a_problem(tmp_path):
    path = tmp_path / "config.toml"
    path.write_text("[server\nport = ")
    cfg = load(path)
    assert cfg.port == DEFAULT_PORT
    assert cfg.problems, "a broken file must be reported, not swallowed"


def test_values_are_read(tmp_path):
    path = tmp_path / "config.toml"
    path.write_text(
        """
        [server]
        port = 9000
        timeout_ms = 5000

        [policy]
        allow = ["omarchy system reboot"]
        allow_groups = ["install"]
        deny = ["omarchy launch browser"]

        [tools]
        disabled = ["screen_text"]

        [log]
        level = "debug"
        """
    )
    cfg = load(path)
    assert cfg.port == 9000
    assert cfg.timeout_ms == 5000
    assert cfg.allow == ("omarchy system reboot",)
    assert cfg.allow_groups == ("install",)
    assert cfg.deny == ("omarchy launch browser",)
    assert cfg.disabled_tools == ("screen_text",)
    assert cfg.log_level == "debug"
    assert cfg.problems == ()


def test_out_of_range_port_falls_back(tmp_path):
    path = tmp_path / "config.toml"
    path.write_text("[server]\nport = 70000\n")
    cfg = load(path)
    assert cfg.port == DEFAULT_PORT
    assert any("port" in p for p in cfg.problems)


def test_wrong_types_fall_back(tmp_path):
    path = tmp_path / "config.toml"
    path.write_text('[policy]\nallow = "omarchy system reboot"\n')
    cfg = load(path)
    assert cfg.allow == ()
    assert cfg.problems


def test_unknown_log_level_falls_back(tmp_path):
    path = tmp_path / "config.toml"
    path.write_text('[log]\nlevel = "verbose"\n')
    cfg = load(path)
    assert cfg.log_level == "info"
    assert cfg.problems


def test_host_is_not_configurable(tmp_path):
    """Binding beyond loopback must not be reachable from a config file."""
    path = tmp_path / "config.toml"
    path.write_text('[server]\nhost = "0.0.0.0"\n')
    assert load(path).host == "127.0.0.1"


def test_example_config_parses_and_changes_nothing(tmp_path):
    """Every key in the shipped file is commented out, so loading it must be
    indistinguishable from having no file at all."""
    import pathlib

    example = pathlib.Path(__file__).resolve().parents[1] / "config.example.toml"
    cfg = load(example)
    assert cfg == Config(), "the shipped config must not pin any default"


def test_the_activity_log_is_on_and_can_be_switched_off(tmp_path):
    """On by default: it is the only record of what an agent did that outlives
    the daemon. Off is offered because it holds command arguments."""
    assert Config().activity is True

    path = tmp_path / "config.toml"
    path.write_text("[log]\nactivity = false\n")
    assert load(path).activity is False


def test_an_out_of_range_activity_cap_falls_back_and_says_so(tmp_path):
    path = tmp_path / "config.toml"
    path.write_text("[log]\nactivity_max_bytes = 12\n")
    cfg = load(path)
    assert cfg.activity_max_bytes == config_module.DEFAULT_ACTIVITY_MAX_B
    assert cfg.problems


def test_the_activity_file_is_a_name_not_a_path(tmp_path):
    """A log inside the plugin directory would make Omarchy reload the shell on
    every tool call, so a separator is refused rather than resolved."""
    path = tmp_path / "config.toml"
    path.write_text('[log]\nactivity_file = "../../.config/omarchy/plugins/x.jsonl"\n')
    cfg = load(path)
    assert cfg.activity_file == config_module.ACTIVITY_FILE
    assert cfg.problems

    path.write_text('[log]\nactivity_file = "calls.jsonl"\n')
    assert load(path).activity_file == "calls.jsonl"
