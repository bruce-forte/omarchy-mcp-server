"""Parsing and searching the command registry."""

from __future__ import annotations

import pytest

from omarchy_mcp.registry import RegistryError, _parse, as_dict


def test_every_command_has_a_route(commands):
    assert all(c.route for c in commands.values())


def test_routes_split_into_argv(commands):
    assert commands["omarchy theme set"].argv_prefix == ["omarchy", "theme", "set"]


def test_sudo_flag_is_read(commands):
    assert any(c.requires_sudo for c in commands.values())
    assert commands["omarchy theme current"].requires_sudo is False


def test_hidden_flag_is_read(commands):
    assert any(c.hidden for c in commands.values())


def test_empty_payload_is_an_error():
    with pytest.raises(RegistryError):
        _parse('{"ok": true, "commands": []}')


def test_invalid_json_is_an_error():
    with pytest.raises(RegistryError):
        _parse("not json")


def test_as_dict_is_json_serialisable(commands):
    import json

    json.dumps(as_dict(commands["omarchy theme set"]))


class TestSearch:
    """Search is deliberately simple; these pin the ordering that makes it
    usable, because a model reads the first few rows and stops."""

    def test_exact_route_ranks_first(self):
        from omarchy_mcp.registry import search

        assert search("omarchy theme set")[0].route == "omarchy theme set"

    def test_route_without_prefix_ranks_first(self):
        from omarchy_mcp.registry import search

        assert search("theme set")[0].route == "omarchy theme set"

    def test_summary_matches_are_found(self):
        from omarchy_mcp.registry import search

        routes = [c.route for c in search("nightlight")]
        assert "omarchy toggle nightlight" in routes

    def test_hidden_excluded_by_default(self):
        from omarchy_mcp.registry import search

        assert all(not c.hidden for c in search("", limit=500))

    def test_hidden_included_on_request(self):
        from omarchy_mcp.registry import search

        assert any(c.hidden for c in search("", limit=500, include_hidden=True))

    def test_limit_is_respected(self):
        from omarchy_mcp.registry import search

        assert len(search("omarchy", limit=3)) == 3

    def test_suggest_falls_back_to_words(self):
        from omarchy_mcp.registry import suggest

        # No command contains this phrase, but "volume" is a real word in one.
        assert any("volume" in c.route for c in suggest("omarchy volume up"))

    def test_suggest_is_empty_when_nothing_is_close(self):
        from omarchy_mcp.registry import suggest

        assert suggest("omarchy zzzzzz") == []
