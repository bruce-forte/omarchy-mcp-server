# Working on this repo

An Omarchy plugin: a Python daemon serving MCP over loopback HTTP, plus two QML
surfaces hosted by `omarchy-shell`.

Read these first, in this order:

| File | For |
|------|-----|
| [`ARCHITECTURE.md`](ARCHITECTURE.md) | How it works and why it is shaped this way. **Start here before touching the source** |
| [`SECURITY.md`](SECURITY.md) | The threat model. Read before changing `policy.py`, `auth.py`, or `execute.py` |
| [`ROADMAP.md`](ROADMAP.md) | Decisions with their reasons, phases, and what was rejected — check before proposing a feature |
| `README.md` | The user-facing side |

This file is the working agreement.

## Conventions for this repo

- **Commit straight to `master`.** No branches.
- **No AI attribution in commit messages.** No `Co-Authored-By`, no
  `Generated with`, no session trailer.
- Run `make check` before committing. It runs the tests, `qmllint`, and
  `omarchy plugin validate`.
- **Use the `Makefile`, not bare `uv`.** It sets `UV_PROJECT_ENVIRONMENT` so the
  dev virtualenv lands outside the repository. A `.venv` here makes
  `omarchy plugin validate` fail, because it rejects symlinks inside a plugin
  folder.
- Regenerate `TOOLS.md` with `make tools` whenever a tool's name, description,
  schema, or annotations change. It is generated; never edit it by hand.

## The security boundary

`policy.py`, `auth.py` and `execute.py` are the boundary. Changes to them need
tests in the same commit, and the existing tests are the specification:

- Every sudo command classifies `blocked`, and no configuration can promote it.
- Guarded commands are refused with a reason that names the config file.
- `argv` never passes through a shell. `tests/test_execute.py` writes a canary
  file and asserts it survives an injection attempt.
- Missing, wrong, and truncated tokens are all rejected; `/health` is the only
  route without one.
- A foreign `Origin` gets 403, a foreign `Host` gets 421.

Do not add a config key for the listen address. See `SECURITY.md`.

## Never fail silently

The only client of this daemon is a language model, and the only person who
cares is looking at a desktop rather than a terminal. A daemon that dies quietly
looks exactly like one that was never installed.

- **Daemon-level faults notify** — bootstrap failed, port taken, config invalid —
  through `omarchy notification send`, naming the fix and the exact command.
- **Tool-call failures do not notify.** The agent already got the error in its
  response, and a wrong `omarchy_run` would toast constantly.
- Everything goes to stderr, which `omarchy-shell` inherits, so
  `journalctl --user -f` has it alongside the shell's own QML errors.
- Log every tool call at `info`: name, route, exit code. That is the audit trail
  of what an agent actually did to the desktop.

## Omarchy plugin conventions

House rules for Omarchy plugins generally, not just this one. Several are not
written down anywhere upstream.

### Never write inside the plugin directory

Omarchy watches `~/.config/omarchy/plugins/` and reloads the shell on any file
write. A plugin that writes into its own directory reloads the shell every time
it does.

| Kind of file | Goes in |
|--------------|---------|
| User config | `${XDG_CONFIG_HOME:-~/.config}/omarchy/<name>/` |
| State, caches, venvs, build output | `${XDG_STATE_HOME:-~/.local/state}/<plugin-id>/` |
| Pidfiles, sockets, anything that dies with the session | `$XDG_RUNTIME_DIR/` |
| Never | the plugin directory |

Whatever a plugin puts in the state and config directories must be named in the
README's uninstall section: `omarchy plugin remove` takes the plugin directory
only.

### There is no build step

`omarchy plugin add` clones, validates the manifest, and copies files. It runs no
install hook and no build, by design. So either ship something that runs from a
checkout, or build lazily on first run **into the state folder**.

### Register an IPC target

Quickshell registers an `IpcHandler` under the plugin's id, making it reachable
from a terminal:

```bash
omarchy-shell io.github.bruce-forte.mcp-server status
```

Discover every target and its signatures — this is how first-party plugins are
driven, and how a script or an agent finds out what a plugin can do:

```bash
qs ipc -n -p "$OMARCHY_PATH/shell" show
```

Because that listing *is* the discovery mechanism, treat the function names and
return types as documentation: name them for the caller, not the implementation,
and return a string rather than nothing when there is anything worth reporting.

Note `shell call <id> <method> <arg>` is a different, lower-level route that
reaches loaded panels and overlays. `IpcHandler` is the one that shows up in the
listing.

### Services cannot be called by widgets

Omarchy routes inter-plugin calls to panels and overlays, but not to services. A
bar widget cannot ask a service anything, so a service that has state a widget
needs must publish it to a file.

### Reload rules

- Editing Python: `omarchy-shell io.github.bruce-forte.mcp-server restart`
- Editing QML: `omarchy restart shell` — the shell holds the object it already
  instantiated, so neither saving the file nor `rescanPlugins` swaps it in
- Editing `~/.config/omarchy/mcp/config.toml`:
  `omarchy-shell io.github.bruce-forte.mcp-server reloadConfig`
