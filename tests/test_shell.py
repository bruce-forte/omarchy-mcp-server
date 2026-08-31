"""Parsing `qs ipc show`, which is the only documentation the shell's IPC has."""

from __future__ import annotations

from omarchy_mcp.shell import as_dict, call_argv, parse_targets


def test_all_targets_are_found(ipc_listing):
    targets = parse_targets(ipc_listing)
    assert len(targets) == ipc_listing.count("\ntarget ") + ipc_listing.startswith("target ")


def test_known_targets_are_present(ipc_listing):
    targets = parse_targets(ipc_listing)
    for name in ("shell", "media", "notifications", "osd", "nightlight"):
        assert name in targets


def test_zero_argument_method(ipc_listing):
    media = parse_targets(ipc_listing)["media"]
    play = next(m for m in media.methods if m.name == "playPause")
    assert play.params == ()
    assert play.returns == "string"
    assert play.signature == "playPause(): string"


def test_method_parameters_are_typed(ipc_listing):
    shell = parse_targets(ipc_listing)["shell"]
    summon = next(m for m in shell.methods if m.name == "summon")
    assert [p.name for p in summon.params] == ["id", "payloadJson"]
    assert all(p.type == "string" for p in summon.params)


def test_void_returns_are_kept(ipc_listing):
    targets = parse_targets(ipc_listing)
    assert any(m.returns == "void" for t in targets.values() for m in t.methods)


def test_methods_are_sorted(ipc_listing):
    for target in parse_targets(ipc_listing).values():
        names = [m.name for m in target.methods]
        assert names == sorted(names)


def test_empty_listing_yields_nothing():
    assert parse_targets("") == {}


def test_as_dict_is_json_serialisable(ipc_listing):
    import json

    json.dumps(as_dict(parse_targets(ipc_listing)["media"]))


def test_call_argv_never_builds_a_shell_string():
    argv = call_argv("media", "playPause", ["; rm -rf ~"])
    assert argv == ["omarchy-shell", "media", "playPause", "; rm -rf ~"]
