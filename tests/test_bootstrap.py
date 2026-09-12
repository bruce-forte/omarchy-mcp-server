"""The bootstrap wrapper.

It is bash, so it gets read rather than imported. These pin the properties that
are invisible until something has already gone wrong on a live desktop.
"""

from __future__ import annotations

import hashlib
import io
import pathlib
import re
import shlex
import shutil
import subprocess
import tarfile

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
    assert "sha256sum" in wrapper


def test_the_expected_checksum_is_committed_rather_than_fetched(wrapper):
    """A checksum from the host that served the archive authenticates nothing.

    Release assets are mutable, so whoever can replace the archive can replace
    the `.sha256` lying beside it and the check passes on both. The expected
    digest has to be a constant in reviewed source, one per architecture.
    """
    digests = re.findall(r'UV_SHA256_[A-Z0-9_]+="([0-9a-f]{64})"', wrapper)
    assert len(digests) == 2, "one committed digest per supported architecture"
    assert len(set(digests)) == 2, "the two architectures cannot share a digest"
    # Comments are stripped: the rationale above the constants names the
    # `.sha256` file precisely to say why it is not used.
    code = "\n".join(
        line for line in wrapper.splitlines() if not line.lstrip().startswith("#")
    )
    assert ".sha256" not in code, "the checksum must not be fetched"


def test_the_download_is_bounded(wrapper):
    """An unbounded response is a full disk before anything has checked it."""
    assert "--max-filesize" in wrapper
    assert "--max-time" in wrapper
    assert "--proto '=https'" in wrapper, "a redirect must not leave https"


def test_only_the_uv_member_is_extracted(wrapper):
    """`tar -xzf ... -- "$dir/uv"` and nothing else: uvx is never written."""
    assert '-- "$dir/uv" \\' in wrapper or '-- "$dir/uv"' in wrapper
    assert "--no-same-owner" in wrapper
    assert "--no-same-permissions" in wrapper


def test_installation_is_atomic(wrapper):
    """Staged beside the destination, then renamed onto it.

    Same directory means the same filesystem, so the rename is atomic and
    nothing ever executes a half-written uv.
    """
    assert 'staged="$UV_BIN_DIR/.uv.$$"' in wrapper
    assert '"${TRUSTED[mv]}" -f -- "$staged" "$UV_BIN_DIR/uv"' in wrapper


def test_the_binary_is_checked_again_before_it_is_executed(wrapper):
    """find_uv may return one installed on an earlier run, so the check that
    matters is the one on the line before it is used."""
    body = wrapper[wrapper.index("ensure_venv() {"):]
    check = body.index('usable_uv "$uv"')
    use = body.index('"$uv" sync')
    assert check < use


# --- the archive checks, exercised rather than read ------------------------
#
# `verify_uv_archive` is split out of `install_uv` precisely so these can run
# with no network: the archives below are built here, in a temporary
# directory, and every one of them is a shape a compromised release could
# take. Reading the source for the right flags proves the flag is spelled
# correctly; only running it proves tar is refused the input.

#: The directory a real uv archive unpacks into, and its two members.
ARCHIVE_DIR = "uv-x86_64-unknown-linux-gnu"


def _library(tmp_path: pathlib.Path) -> pathlib.Path:
    """The wrapper's functions, without the tail that runs the daemon.

    Written into a ``bin/`` directory beside a copy of `omarchy-mcp-trust`,
    because the head of the wrapper now works out its own plugin directory and
    sources that library from it. Mirroring the real layout rather than stubbing
    the library means these tests exercise the real one -- a stub would agree
    with a bug in it.
    """
    binaries = tmp_path / "bin"
    binaries.mkdir(exist_ok=True)
    shutil.copy(WRAPPER.parent / "omarchy-mcp-trust", binaries / "omarchy-mcp-trust")
    library = binaries / "lib.sh"
    text = WRAPPER.read_text()
    library.write_text(text[: text.index("seed_config() {")])
    return library


def _regular(tar: tarfile.TarFile, name: str, size: int = 16) -> None:
    info = tarfile.TarInfo(name)
    info.size = size
    info.mode = 0o755
    tar.addfile(info, io.BytesIO(b"\0" * size))


def _directory(tar: tarfile.TarFile, name: str) -> None:
    info = tarfile.TarInfo(name)
    info.type = tarfile.DIRTYPE
    info.mode = 0o755
    tar.addfile(info)


def _special(tar: tarfile.TarFile, name: str, kind: bytes, target: str) -> None:
    info = tarfile.TarInfo(name)
    info.type = kind
    info.linkname = target
    tar.addfile(info)


def _archive(tmp_path: pathlib.Path, build) -> pathlib.Path:
    path = tmp_path / "uv.tar.gz"
    with tarfile.open(path, "w:gz") as tar:
        build(tar)
    return path


def _good(tar: tarfile.TarFile) -> None:
    _directory(tar, f"{ARCHIVE_DIR}/")
    _regular(tar, f"{ARCHIVE_DIR}/uv")
    _regular(tar, f"{ARCHIVE_DIR}/uvx")


def _verify(tmp_path, archive: pathlib.Path, digest: str | None = None, **bounds):
    """Run the wrapper's own `verify_uv_archive` against one archive.

    `fail` is redefined after sourcing so that nothing raises a desktop
    notification: the real one calls `omarchy notification send`, and a suite
    that puts a question on somebody's screen is the thing conftest's guards
    exist to stop. A stub `omarchy` on PATH is the second belt.
    """
    if digest is None:
        digest = hashlib.sha256(archive.read_bytes()).hexdigest()

    stub = tmp_path / "stub"
    stub.mkdir(exist_ok=True)
    (stub / "omarchy").write_text("#!/bin/sh\nexit 0\n")
    (stub / "omarchy").chmod(0o755)

    overrides = "".join(f"{key}={value}\n" for key, value in bounds.items())
    script = f"""
      source {shlex.quote(str(_library(tmp_path)))}
      fail() {{ printf 'REFUSED: %s\n' "$*" >&2; exit 1; }}
      {overrides}
      verify_uv_archive {shlex.quote(str(archive))} {shlex.quote(ARCHIVE_DIR)} {shlex.quote(digest)}
      echo ACCEPTED
    """
    return subprocess.run(
        ["bash", "-c", script],
        capture_output=True,
        text=True,
        env={"PATH": f"{stub}:/usr/bin:/bin", "HOME": str(tmp_path)},
    )


def test_a_well_formed_archive_is_accepted(tmp_path):
    """Guards the guards: if this failed, every refusal below would be
    meaningless, because nothing would ever be accepted."""
    result = _verify(tmp_path, _archive(tmp_path, _good))
    assert "ACCEPTED" in result.stdout, result.stderr


def test_a_wrong_checksum_is_refused(tmp_path):
    result = _verify(tmp_path, _archive(tmp_path, _good), digest="0" * 64)
    assert result.returncode != 0
    assert "checksum committed in this plugin" in result.stderr


def test_a_symlink_member_is_refused(tmp_path):
    """Named exactly what a real archive names it, so only the type check can
    catch this one. tar would otherwise write through it, out of the
    extraction directory."""

    def build(tar):
        _directory(tar, f"{ARCHIVE_DIR}/")
        _regular(tar, f"{ARCHIVE_DIR}/uv")
        _special(tar, f"{ARCHIVE_DIR}/uvx", tarfile.SYMTYPE, "/etc/passwd")

    result = _verify(tmp_path, _archive(tmp_path, build))
    assert result.returncode != 0
    assert "link or a special file" in result.stderr


def test_a_hard_link_member_is_refused(tmp_path):
    def build(tar):
        _directory(tar, f"{ARCHIVE_DIR}/")
        _regular(tar, f"{ARCHIVE_DIR}/uv")
        _special(tar, f"{ARCHIVE_DIR}/uvx", tarfile.LNKTYPE, f"{ARCHIVE_DIR}/uv")

    result = _verify(tmp_path, _archive(tmp_path, build))
    assert result.returncode != 0
    assert "link or a special file" in result.stderr


def test_a_fifo_member_is_refused(tmp_path):
    def build(tar):
        _directory(tar, f"{ARCHIVE_DIR}/")
        _regular(tar, f"{ARCHIVE_DIR}/uv")
        _special(tar, f"{ARCHIVE_DIR}/uvx", tarfile.FIFOTYPE, "")

    result = _verify(tmp_path, _archive(tmp_path, build))
    assert result.returncode != 0
    assert "link or a special file" in result.stderr


def test_a_traversing_member_is_refused(tmp_path):
    def build(tar):
        _directory(tar, f"{ARCHIVE_DIR}/")
        _regular(tar, f"{ARCHIVE_DIR}/uv")
        _regular(tar, "../../../../tmp/pwned")

    result = _verify(tmp_path, _archive(tmp_path, build))
    assert result.returncode != 0
    assert "exactly the files this plugin expects" in result.stderr


def test_an_absolute_member_is_refused(tmp_path):
    def build(tar):
        _directory(tar, f"{ARCHIVE_DIR}/")
        _regular(tar, f"{ARCHIVE_DIR}/uv")
        _regular(tar, "/etc/cron.d/pwned")

    result = _verify(tmp_path, _archive(tmp_path, build))
    assert result.returncode != 0
    assert "exactly the files this plugin expects" in result.stderr


def test_an_unexpected_member_is_refused(tmp_path):
    """An allowlist, so a member nobody anticipated is refused by default --
    the same shape as SAFE_GROUPS in policy.py, and for the same reason."""

    def build(tar):
        _good(tar)
        _regular(tar, f"{ARCHIVE_DIR}/install.sh")

    result = _verify(tmp_path, _archive(tmp_path, build))
    assert result.returncode != 0
    assert "exactly the files this plugin expects" in result.stderr


def test_a_missing_member_is_refused(tmp_path):
    """The allowlist is an equality, not a subset test."""

    def build(tar):
        _directory(tar, f"{ARCHIVE_DIR}/")
        _regular(tar, f"{ARCHIVE_DIR}/uv")

    result = _verify(tmp_path, _archive(tmp_path, build))
    assert result.returncode != 0
    assert "exactly the files this plugin expects" in result.stderr


def test_too_many_members_is_refused(tmp_path):
    """The bound is lowered rather than the archive made huge -- which also
    proves the bound is the thing being consulted."""

    def build(tar):
        _good(tar)

    result = _verify(tmp_path, _archive(tmp_path, build), UV_MEMBERS_MAX=2)
    assert result.returncode != 0
    assert "more members than this plugin accepts" in result.stderr


def test_an_oversized_member_is_refused(tmp_path):
    result = _verify(tmp_path, _archive(tmp_path, _good), UV_MEMBER_MAX_BYTES=8)
    assert result.returncode != 0
    assert "oversized or unreadable member" in result.stderr


def test_an_oversized_archive_is_refused(tmp_path):
    """Checked against what actually landed, because --max-filesize acts on a
    declared length that a chunked response does not carry."""
    result = _verify(tmp_path, _archive(tmp_path, _good), UV_ARCHIVE_MAX_BYTES=8)
    assert result.returncode != 0
    assert "outside what this plugin accepts" in result.stderr


def test_a_symlink_at_the_destination_is_not_executed(tmp_path):
    """`-f` is true for a symlink pointing at a regular file, so the `-L` test
    in `usable_uv` is what stops anything that can write into the state
    directory from redirecting the build at another binary."""
    real = tmp_path / "real"
    real.write_text("#!/bin/sh\n")
    real.chmod(0o755)
    planted = tmp_path / "uv"
    planted.symlink_to(real)

    script = f"""
      source {shlex.quote(str(_library(tmp_path)))}
      if usable_uv {shlex.quote(str(planted))}
        then echo ACCEPTED
        else echo REFUSED
      fi
      if usable_uv {shlex.quote(str(real))}
        then echo REGULAR_ACCEPTED
        else echo REGULAR_REFUSED
      fi
    """
    result = subprocess.run(
        ["bash", "-c", script],
        capture_output=True,
        text=True,
        env={"PATH": "/usr/bin:/bin", "HOME": str(tmp_path)},
    )
    assert "REFUSED" in result.stdout, result.stderr
    assert "REGULAR_ACCEPTED" in result.stdout, result.stderr


def test_config_is_never_overwritten(wrapper):
    assert "[[ -f $CONFIG_DIR/config.toml ]] && return 0" in wrapper


def test_failures_notify(wrapper):
    """The caller is a QML service and the user is looking at a desktop, so a
    failure that is only logged is a failure nobody sees.

    Through the allowlist like everything else. A failure path is the worst
    place to resolve a command on an unchecked PATH, and this one can be
    reached before binding has succeeded -- so it is guarded rather than bare.
    """
    assert '"${TRUSTED[omarchy]}" notification send' in wrapper
    assert "${TRUSTED[omarchy]:-}" in wrapper, "reachable before binding; must not assume"


#: Commands whose last argument is where they write.
WRITES_TO_LAST_ARG = ("cp", "mv", "install", "tee")
#: Commands where every argument is a target.
WRITES_TO_ANY_ARG = ("mkdir", "touch", "rm", "rmdir", "truncate")


def command_name(word: str) -> str:
    """The command a first word names, whichever way it is spelled.

    Every spawn in the wrapper now goes through the allowlist, so `cp` is
    written `"${TRUSTED[cp]}"`. Without this the guard below stops recognising
    a single write in the file **and keeps passing** -- which is the worst
    outcome available to a test whose whole job is to notice one.
    """
    found = re.fullmatch(r'"?\$\{TRUSTED\[([a-z0-9-]+)\]\}"?', word)
    return found.group(1) if found else word


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
        command = command_name(words[0])
        if command in WRITES_TO_ANY_ARG and any("$PLUGIN_DIR" in w for w in words[1:]):
            offenders.append(stripped)
        elif command in WRITES_TO_LAST_ARG and "$PLUGIN_DIR" in words[-1]:
            offenders.append(stripped)

    assert not offenders, f"these write into the plugin directory: {offenders}"


def test_the_write_guard_sees_through_a_verified_spelling():
    """Guards the guard. `test_nothing_writes_into_the_plugin_directory` went on
    passing when every command in the wrapper was rewritten, because it no
    longer recognised any of them as a command."""
    assert command_name('"${TRUSTED[cp]}"') == "cp"
    assert command_name('"${TRUSTED[rm]}"') == "rm"
    assert command_name("cp") == "cp"
    assert command_name('"$VENV/bin/python"') == '"$VENV/bin/python"'


def test_reading_from_the_plugin_directory_is_fine(wrapper):
    """Guards the guard: the config template is copied out of the plugin
    directory, and that must not be mistaken for a write into it."""
    assert (
        '"${TRUSTED[cp]}" "$PLUGIN_DIR/config.example.toml" "$CONFIG_DIR/config.toml"'
    ) in wrapper
