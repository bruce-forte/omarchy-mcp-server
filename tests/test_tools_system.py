"""The status aggregator's parsing.

Omarchy's status commands emit three different shapes and this normalises all
of them, so each shape is pinned.
"""

from __future__ import annotations

from omarchy_mcp.tools.system import PROBES, _parse


def test_json_output_is_parsed():
    parsed = _parse('{"enabled":false,"class":"disabled"}')
    assert parsed == {"enabled": False, "class": "disabled"}


def test_tab_separated_pairs_become_a_mapping():
    parsed = _parse("cpu\t13%\nmemory\t19.0GB / 29GB")
    assert parsed == {"cpu": "13%", "memory": "19.0GB / 29GB"}


def test_trailing_empty_fields_survive():
    """`omarchy network status` pads with empty tab-separated fields."""
    assert _parse("ethernet\tenp1s0\t\t")["ethernet"] == "enp1s0"


def test_single_line_becomes_a_string():
    assert _parse("Catppuccin Latte") == "Catppuccin Latte"


def test_empty_output_is_null():
    """A machine with no battery prints nothing, which is not an error."""
    assert _parse("") is None
    assert _parse("   \n  ") is None


def test_malformed_json_falls_back_to_text():
    """Better to hand back what the command said than to lose it."""
    assert _parse('{"broken": ') == '{"broken":'


def test_multiline_prose_is_kept_whole():
    assert _parse("line one\nline two") == "line one\nline two"


def test_every_probe_is_a_read_only_omarchy_command():
    """The aggregate must stay safe to call: nothing here may change state."""
    for name, argv in PROBES.items():
        assert argv[0] == "omarchy", name
        assert not {"set", "install", "remove", "reboot"} & set(argv), name
