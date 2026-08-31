# Architecture

For someone opening this repository knowing nothing about it. `README.md` says
what it does for a user; this says how it works and why it is shaped this way.
`ROADMAP.md` says what was decided and what is still to come.

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

The stamp covers the lockfile, the interpreter version, and the wrapper's own
hash, so any of the three changing rebuilds the environment and nothing else
does. `uv` itself is downloaded at a pinned version and checksum-verified rather
than through the upstream one-line installer: other people install this plugin,
and fetching an unpinned script from a third-party host would run whatever that
host happened to be serving that day.

## Where things are written

Omarchy watches `~/.config/omarchy/plugins/` and **reloads the shell on any file
write**. A plugin that writes inside its own directory reloads the shell every
time it does, which at worst is a loop. So:

| Kind of file | Goes in |
|--------------|---------|
| User config | `~/.config/omarchy/mcp/` |
| Virtualenv, bearer token, bootstrap stamp, bytecode cache | `~/.local/state/io.github.bruce-forte.mcp-server/` |
| The state file the bar widget reads | `$XDG_RUNTIME_DIR/omarchy-mcp.state` |
| Never | the plugin directory |

`paths.py` holds every one of these locations, so the rules are enforced by
construction rather than by remembering them at each call site.

This has three non-obvious consequences.

**The project is never installed into its own virtualenv.** An editable install
writes `.egg-info` and build artifacts into the plugin directory. So the wrapper
installs *dependencies only* (`uv sync --no-install-project`) and runs the code
by path with `PYTHONPATH=$PLUGIN_DIR/src`.

**Bytecode goes to the state directory.** Python caches `__pycache__` next to
the source it imports, and the source is in the watched directory — so without
`PYTHONPYCACHEPREFIX` every interpreter start made the shell reload itself, and
the reload restarted the daemon. This shipped broken; see finding F19.
`tests/test_bootstrap.py` reads the wrapper and fails if anything writes into
the plugin directory, so it cannot come back quietly.

**The development virtualenv also lives outside the repository.** Not for
tidiness: `omarchy plugin validate` rejects symlinks anywhere inside a plugin
folder, and a virtualenv is largely symlinks, so a `.venv` here makes the plugin
fail validation. The `Makefile` sets `UV_PROJECT_ENVIRONMENT` so a bare
`uv run` cannot create one by accident. Use the `Makefile`, not bare `uv`.

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
    │        ├── writes $XDG_RUNTIME_DIR/omarchy-mcp.state
    │        └── IpcHandler: status, start, stop, restart,
    │                        reloadConfig, rebuild, clientConfig
    │
    └── BarWidget.qml ──reads──> the state file
```

The daemon prints one JSON line to stdout per state change — `listening` with
the port, or `failed` with the reason. Everything else goes to stderr, which
`omarchy-shell` inherits, so `journalctl --user -f` carries the daemon's log
alongside the shell's own QML errors.

The bar widget reads a file rather than asking the service, because **Omarchy
routes inter-plugin calls to panels and overlays but not to services**. A widget
cannot call a service, so the service publishes. This is a house rule for
Omarchy plugins generally and it constrains anything that later wants the widget
to *send* rather than only display — see `ROADMAP.md` N6.

## The IPC surface is documentation

`Service.qml` registers an `IpcHandler` under the plugin id, so the plugin is
drivable from a terminal:

```bash
omarchy-shell io.github.bruce-forte.mcp-server status
qs ipc -n -p "$OMARCHY_PATH/shell" show     # every target and signature
```

That listing is how a script or an agent discovers what a plugin can do, and it
is the only documentation these interfaces have. So the function names are part
of the public surface: name them for the caller rather than the implementation,
and return a string rather than nothing when there is anything worth reporting.

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
| `desktop.py` | Hyprland's monitors, workspaces, windows and focus, via `hyprctl -j` |
| `status.py` | The system-status aggregate: several probes gathered into one answer |
| `stats.py` | In-memory call counters, published through `/health` for the bar tooltip |
| `policy.py` | The security boundary. Pure, takes the registry as an argument, tested against every route Omarchy ships |
| `resolve.py` | Turns an identifier an agent supplied into the thing it names, or refuses. See below |
| `execute.py` | `argv` only, never a shell. Timeouts, process-group termination, output caps, detaching |
| `token.py` | The bearer token, created `0600` |
| `auth.py` | Bearer authentication as **pure ASGI** — see below |
| `resources.py` | The 4 concrete resources and 3 URI templates |
| `server.py` | Assembles the MCP server, transport security, `/health` |

The tools are a package, split by what they are for:

| Module | Tools |
|--------|-------|
| `tools/generic.py` | 4 — search, run, shell targets, shell call |
| `tools/desktop.py` | 5 — screenshot, desktop state, screen text, clipboard read and write |
| `tools/control.py` | 7 — theme, background, audio, brightness, media, toggle, launch |
| `tools/feedback.py` | 2 — notify, OSD |
| `tools/system.py` | 1 — system status |
| `tools/_shared.py` | `run_route`, the one path every curated tool takes to reach the executor |

`_shared.run_route` matters more than its size suggests. Every curated tool goes
through the same `policy.decide`, the same `resolve.resolve_call` and the same
`execute.run` as `omarchy_run`: a curated tool is a better-shaped door onto the
same room, never a way around the lock. It is also the single place where a
future consent check hooks in (`ROADMAP.md` N4).

## Resolution: naming the target before doing anything

A call passes three gates, in this order:

```
  policy.decide   may this route run at all?          -> tier, and a reason
  resolve         does its argument name anything?    -> Target, or a refusal
  execute.run     argv, no shell, bounded             -> Result
```

`resolve.py` is the middle one. It exists because an unchecked argument fails
inside a subprocess, where the failure arrives as somebody else's stderr — and
because N4 will put a call in front of a person for approval, where *"an agent
wants to switch the theme"* is not consent if the user cannot see which theme.
So an identifier is resolved against live system state before anything is
spawned, and what comes back is a `Target`: the value the command receives, and
a human name for it, which lands in the log and the response today and in the
approval prompt later.

Three properties are load-bearing:

- **It matches the way Omarchy matches.** `omarchy-theme-set` lowercases its
  argument and turns spaces into dashes before looking for the directory, so
  `resolve.slug` does the same. Anything looser would accept a name the command
  then rejects. A near miss is refused with the near misses named, never
  corrected into a different theme.
- **Not found and could not look are different answers.** A wrong name is worth
  retrying; a compositor that is not answering is not. The refusal says which,
  in `reason`, because they imply different next moves for the agent.
- **The table is small on purpose.** A route resolves its arguments only where
  there is cheap, authoritative local truth: themes, monitors, image paths,
  URLs. Package names have none — refusing one that is not installed yet would
  refuse every install — so they pass through untouched.

Resolution is validation, not policy, so it has no configuration key. It refuses
exactly the calls that would have failed anyway, one step earlier and with a
better message.

## Three traps worth knowing about

**Auth must not be a `BaseHTTPMiddleware`.** Starlette's `BaseHTTPMiddleware`
wraps the receive channel. The MCP streamable-HTTP transport runs a disconnect
watcher that calls `receive()` expecting `http.disconnect`; through
`BaseHTTPMiddleware` it gets `http.request` instead, and every `tools/call` fails
with a 500. `auth.py` reads headers straight off the ASGI scope for this reason.

**The SDK is `mcp` 2.x, where `FastMCP` was renamed `MCPServer`** and moved to
`mcp.server.mcpserver`. Almost every example online is 1.x and does not apply.
`pyproject.toml` pins `mcp>=2,<3`, and `tests/test_server.py` does a real
handshake so a breaking release fails here rather than in a client.

**QML failures are invisible to `qmllint`.** Every finding in phase 2 was a
component that linted clean and behaved differently in a live shell — a
collector read before its stream finished, a probe that reported down for a full
interval after every start, a state file written from a property handler before
the rest of the properties were set. Run it in a real shell before believing it.

## Why the tool surface is small

Omarchy has several hundred commands. One MCP tool per command would put tens of
thousands of tokens of schema into every client's context before it did any work.
So the four generic tools are discovery and dispatch — search to find out what
exists, run to do it — and the registry itself is the documentation. Nothing here
needs updating when Omarchy adds a command.

Fifteen curated tools sit on top, and each earns its place one of two ways:
either the generic path structurally cannot produce the result (an image; window
state that is `hyprctl`'s rather than Omarchy's; the clipboard, which is not an
Omarchy command at all), or the thing is asked for constantly and a search round
trip before every volume change is a bad trade. Anything a curated tool does
stays reachable through `omarchy_run`, and any of them can be switched off in
the config — a disabled tool is never registered, so it does not appear in
`tools/list` at all.

`TOOLS.md` is generated from the server's own schemas and `make check` fails if
it is stale. Never edit it by hand.

## Resources, and who they are for

Seven, and they are not a second tool surface. Tools are how an agent acts;
resources are how a person reads. In Claude Code they appear as `@` mentions the
user types, so they are chosen for what someone building an Omarchy plugin would
want to pull into a conversation — above all `omarchy://shell/targets`, the
shell's IPC surface with full signatures, which is documented nowhere upstream.

Four are concrete and appear in the `@` menu. Three are URI templates covering
every command and every target without putting several hundred entries in a
listing. Claude Code never enumerates templates (finding F8), so the concrete
four are the discoverable set; templates still resolve when read by URI, and
other clients may list them.

## What the tests pin

`policy.py`, `auth.py` and `execute.py` are the security boundary, and the
existing tests are its specification — every sudo command classifies `blocked`
and no configuration can promote it, `argv` never reaches a shell, a foreign
`Origin` gets 403 and a foreign `Host` gets 421. Changes there need tests in the
same commit.

The suite reads `tests/fixtures/commands.json`, a committed snapshot of
`omarchy commands --all --json`, rather than the installed Omarchy. An autouse
fixture enforces it. That is what lets the tests run in CI at all, and it stops
them quietly changing meaning the next time `omarchy update` renames a route
(finding F21). Refresh the snapshot deliberately, in its own commit.
