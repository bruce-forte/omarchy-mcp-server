# Architecture

For someone opening this repository knowing nothing about it. `README.md` says
what it does for a user; this says how it works and why it is shaped this way.

## The constraint everything follows from

Omarchy plugins are QML, loaded into a long-running Quickshell process called
`omarchy-shell`. An MCP server is an HTTP server that spawns processes. QML is
the wrong place for both, so the server cannot be a plugin.

So this project is two halves:

- **A daemon** — `bin/omarchy-mcpd` and `src/omarchy_mcp/`. Serves MCP over HTTP
  on loopback and runs Omarchy commands.
- **A plugin** — two QML files hosted by `omarchy-shell`. Starts the daemon,
  watches it, and draws a bar icon saying whether it is serving.

The plugin *supervises* the daemon rather than containing it. That buys two real
things. The daemon starts with your session and stops with it, with no systemd
unit to enable and no second install step. And, less obviously, being a child of
`omarchy-shell` is what gives the daemon `WAYLAND_DISPLAY`,
`HYPRLAND_INSTANCE_SIGNATURE` and `DBUS_SESSION_BUS_ADDRESS` from the live
graphical session — every command that touches the desktop needs them, and a
systemd user unit would have to import them explicitly and would race the
session at boot.

## Why there is no build step

`omarchy plugin add` clones a git repository, validates the manifest, and copies
files. **It runs no build and no install hook, by design** — the installer
refuses to execute plugin code before you have enabled it.

So the environment is built lazily, on first run, by `bin/omarchy-mcpd`. It is a
bash wrapper that checks a stamp, builds a virtualenv if anything changed, and
then `exec`s into Python. Keeping it in bash rather than QML means the daemon is
also runnable by hand from a checkout, which is how you debug it.

## Where things are written

Omarchy watches `~/.config/omarchy/plugins/` and **reloads the shell on any file
write**. A plugin that writes inside its own directory reloads the shell every
time it does, which at worst is a loop. So:

| Kind of file | Goes in |
|--------------|---------|
| User config | `~/.config/omarchy/mcp/` |
| Virtualenv, bearer token, bootstrap stamp | `~/.local/state/io.github.bruce-forte.mcp-server/` |
| The state file the bar widget reads | `$XDG_RUNTIME_DIR/omarchy-mcp.state` |
| Never | the plugin directory |

This has two non-obvious consequences.

**The project is never installed into its own virtualenv.** An editable install
writes `.egg-info` and build artifacts into the plugin directory. So the wrapper
installs *dependencies only* (`uv sync --no-install-project`) and runs the code
by path with `PYTHONPATH=$PLUGIN_DIR/src`.

**The development virtualenv also lives outside the repository.** Not for
tidiness: `omarchy plugin validate` rejects symlinks anywhere inside a plugin
folder, and a virtualenv is largely symlinks, so a `.venv` here makes the plugin
fail validation. The `Makefile` sets `UV_PROJECT_ENVIRONMENT` so a bare
`uv run` cannot create one by accident.

## The pieces

```
  omarchy-shell (Quickshell)
    │
    ├── Service.qml ───spawns──> bin/omarchy-mcpd ───exec──> python -m omarchy_mcp
    │        │                     (bootstrap)                    │
    │        │<──── JSON state lines on stdout ───────────────────┤
    │        │<──── logs on stderr ──────────────────────────────-┘
    │        │
    │        ├── polls GET /health every 10s
    │        └── writes $XDG_RUNTIME_DIR/omarchy-mcp.state
    │
    └── BarWidget.qml ──reads──> the state file
```

The bar widget reads a file rather than asking the service, because **Omarchy
routes inter-plugin calls to panels and overlays but not to services**. A widget
cannot call a service, so the service publishes.

## Why a health probe and not a pid

`Process.running` tells you a process exists. It does not tell you the HTTP loop
is answering. A daemon whose event loop has wedged looks identical to a healthy
one from the outside, and the only client is a language model that will report
"I could not reach the tool" long after the fact. So `Service.qml` polls
`/health` and the widget distinguishes *serving* from *running*.

`/health` is the one route that does not require the bearer token. It carries no
secrets, and requiring a token would mean the supervising QML needed to read one.

## Reading the source

In dependency order, shallowest first:

| Module | Holds |
|--------|-------|
| `paths.py` | Every filesystem location, in one place, so the rules above are enforced by construction |
| `config.py` | TOML loading. Never raises: a broken file yields defaults plus a list of problems |
| `registry.py` | Parses `omarchy commands --json`, cached until Omarchy's version changes. Search and suggestions |
| `shell.py` | Parses `qs ipc show` into targets and typed method signatures |
| `policy.py` | The security boundary. Pure, takes the registry as an argument, tested against every route Omarchy ships |
| `execute.py` | `argv` only, never a shell. Timeouts, process-group termination, output caps, detaching |
| `token.py` | The bearer token, created `0600` |
| `auth.py` | Bearer authentication as **pure ASGI** — see below |
| `server.py` | Assembles the MCP server, transport security, `/health` |
| `tools/generic.py` | The four tools |

## Two traps worth knowing about

**Auth must not be a `BaseHTTPMiddleware`.** Starlette's `BaseHTTPMiddleware`
wraps the receive channel. The MCP streamable-HTTP transport runs a disconnect
watcher that calls `receive()` expecting `http.disconnect`; through
`BaseHTTPMiddleware` it gets `http.request` instead, and every `tools/call` fails
with a 500. `auth.py` reads headers straight off the ASGI scope for this reason.

**The SDK is `mcp` 2.x, where `FastMCP` was renamed `MCPServer`** and moved to
`mcp.server.mcpserver`. Almost every example online is 1.x and does not apply.
`pyproject.toml` pins `mcp>=2,<3`, and `tests/test_server.py` does a real
handshake so a breaking release fails here rather than in a client.

## Why the tool surface is small

Omarchy has several hundred commands. One MCP tool per command would put tens of
thousands of tokens of schema into every client's context before it did any work.
So the tools are discovery and dispatch — search to find out what exists, run to
do it — and the registry itself is the documentation. Nothing here needs updating
when Omarchy adds a command.

Curated tools are being added on top, but only where the generic path is
genuinely insufficient: where the result is not text (a screenshot), where the
data is not Omarchy's (hyprctl window state), or where a search round trip would
be wasteful for something called constantly. See `ROADMAP.md`.
