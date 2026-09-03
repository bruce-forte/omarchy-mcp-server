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
| `config.toml`, and both permissions files | `~/.config/omarchy/mcp/` |
| Virtualenv, bearer token, bootstrap stamp, bytecode cache, activity log | `~/.local/state/io.github.bruce-forte.mcp-server/` |
| The state file the bar widget falls back to | `$XDG_RUNTIME_DIR/omarchy-mcp.state` |
| Whether a Stop survives a restart | `~/.local/state/io.github.bruce-forte.mcp-server/autostart-off` |
| A pending approval's one-time token | `$XDG_RUNTIME_DIR/io.github.bruce-forte.mcp-server/consent/` |
| Never | the plugin directory |

`paths.py` holds every one of these locations, so the rules are enforced by
construction rather than by remembering them at each call site.

**Both permissions files are config, not state**, even though the daemon writes
one of them. `permissions.json` is the user's, hand-edited, and meant to be
checked into a dotfiles repository; `permissions.local.json` is what the daemon
appends when somebody answers *always* at the desk, and belongs in
`.gitignore`. Putting the second in the state directory would have followed the
table above more literally and been worse: a person opening
`~/.config/omarchy/mcp/` has to see **everything that decides what an agent may
do**, and splitting the two halves of one answer across two directories is how
somebody reads half their permissions and believes it is all of them.

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
    └── BarWidget.qml ──serviceFor()──> the service object
                        ──reads──────────> the state file, until it resolves
```

The daemon prints one JSON line to stdout per state change — `listening` with
the port, or `failed` with the reason — and one per tool call, carrying the tool
name and how it ended. Everything else goes to stderr, which `omarchy-shell`
inherits, so `journalctl --user -f` carries the daemon's log alongside the
shell's own QML errors. `frames.py` owns that channel.

The call frame exists because the bar otherwise learns of a tool call from the
`/health` poll up to ten seconds later, by which time the desktop has already
changed in front of the user. **It carries no arguments**, for the same reason
the activity log keeps them behind a `0600` file: a clipboard write's argument
is the clipboard, and this one ends up on a bar.

### The widget reaches the service directly

`shell call <id> <method>` routes through the shell's panel loader map, which
holds panel, overlay and menu plugins only — so it reaches neither services nor
bar widgets. That is true, and it is what this project believed was the whole
story for two phases.

It is not. `shell.serviceFor(pluginId)` returns the live service object, has no
first-party restriction, and both kinds load into one QML engine, so the widget
holds the real `Service.qml` root and calls `stop()` on it. The state file
remains because **the widget is constructed before the service exists**: the
first evaluation of `serviceFor` is null on every startup, and it resolves only
because the binding re-runs when the shell's service map changes. Verified on a
live desktop; see `ROADMAP.md` N6.

**This is where new user-facing state and controls go.** The panel is what a
person can find, and the service object is how it reaches the daemon — so
anything the user should see or change is a property on `Service.qml` and a row
or a button in the panel. The state file is not the pattern; it carries the four
fields the widget needs before `serviceFor` resolves, and it stays that size.

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
| `config.py` | TOML loading. Never raises: a broken file yields defaults plus a list of problems, and a `parsed` flag saying which it is |
| `settings.py` | The holder that says which `Config` is in force, so a reload is one assignment rather than a walk over every closure |
| `notify.py` | Daemon-level facts the person at the desktop has no other way to learn |
| `registry.py` | Parses `omarchy commands --json`, cached until Omarchy's version changes. Search and suggestions |
| `shell.py` | Parses `qs ipc show` into targets and typed method signatures |
| `desktop.py` | Hyprland's monitors, workspaces, windows and focus, via `hyprctl -j` |
| `status.py` | The system-status aggregate: several probes gathered into one answer |
| `stats.py` | The one seam every tool call passes through: counters for `/health`, and a record for the activity log |
| `activity.py` | One JSON line per call on disk, written by a thread nothing waits for. See below |
| `frames.py` | The one thing this process says on stdout: lifecycle and per-call frames the shell parses |
| `policy.py` | The security boundary: what kind of command this is. Pure, takes the registry as an argument, tested against every route Omarchy ships |
| `permissions.py` | The other half: which guarded commands actually run. Three rule lists read `deny` → `ask` → `allow`, from `permissions.json`. Also `explain()`, which expands the rules against the live registry |
| `resolve.py` | Turns an identifier an agent supplied into the thing it names, or refuses. See below |
| `consent.py` | The six ways a call can fail to get a yes, and a wait that fails closed. See below |
| `gate.py` | Where policy, resolution and consent meet and a call runs or does not. Both tool paths come through it |
| `prompt.py` | The two ways a question reaches a person, and the notification that outlives neither |
| `execute.py` | `argv` only, never a shell. Timeouts, process-group termination, output caps, detaching, and which file a bare command name runs |
| `token.py` | The bearer token, created `0600` |
| `auth.py` | Bearer authentication as **pure ASGI** — see below |
| `resources.py` | The 4 concrete resources and 3 URI templates |
| `clients.py` | The connections currently attached, and the two ways to tell them the tool list moved |
| `reload.py` | Re-reads `config.toml` and the permissions files while serving, and refuses to when either does not parse. See below |
| `server.py` | Assembles the MCP server, transport security, `/health` |

The tools are a package, split by what they are for:

| Module | Tools |
|--------|-------|
| `tools/generic.py` | 4 — search, run, shell targets, shell call |
| `tools/desktop.py` | 5 — screenshot, desktop state, screen text, clipboard read and write |
| `tools/control.py` | 7 — theme, background, audio, brightness, media, toggle, launch |
| `tools/feedback.py` | 2 — notify, OSD |
| `tools/system.py` | 1 — system status |
| `tools/catalogue.py` | Every tool that exists, and which of them are offered right now |
| `tools/_shared.py` | `run_route`, the one path every curated tool takes to reach the executor |

`_shared.run_route` matters more than its size suggests. Every curated tool goes
through the same `permissions.decide`, the same `resolve.resolve_call` and the
same `execute.run` as `omarchy_run`: a curated tool is a better-shaped door onto
the same room, never a way around the lock. It is also the one place the consent
check hooks in.

## Resolution: naming the target before doing anything

A call passes these gates, in this order:

```
  policy.self_refusal  would this call silence the daemon?  -> a refusal, or None
  policy.base_tier     what kind of command is this?         -> blocked|guarded|safe
  permissions.decide   what do the user's rules do with it?  -> deny | ask | allow
  resolve              does its argument name anything?      -> Target, or a refusal
  consent.ask          does the user say yes, in time?       -> Answer
  execute.run          argv, no shell, bounded               -> Result
```

`resolve.py` is the middle one. It exists because an unchecked argument fails
inside a subprocess, where the failure arrives as somebody else's stderr — and
because a call is put in front of a person for approval, where *"an agent wants
to switch the theme"* is not consent if the user cannot see which theme.
So an identifier is resolved against live system state before anything is
spawned, and what comes back is a `Target`: the value the command receives, and
a human name for it, which lands in the log, the response, and the approval
prompt.

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

## The activity log, and what a tool call waits for

`stats.py` counts calls in memory; `/health` publishes the counters. That says
*is it serving* and nothing else — it cannot say what an agent did ten minutes
ago, and it dies with the process. `activity.py` appends one JSON object per
tool call to `activity.jsonl` in the state directory.

**One seam.** `stats.call(tool)` is a context manager. A tool opens one, fills
in what it learns — the route, the resolved target, the tier, how the user
answered, the exit code — and exactly one record is written when it leaves,
including when it leaves by raising. That shape is what makes two long-standing
bugs unrepresentable: `omarchy_run` used to count *before* the gate ran, so
every refusal was recorded as a success, and the desktop tools recorded once per
branch.

**A tool call never waits for the disk.** `append()` puts the record on a
bounded queue and returns; one writer thread drains it. Because that thread is
the file's only writer, there is no lock on the file at all — the queue is the
serialisation. It is started in `__main__` around `uvicorn.run` and stopped with
a sentinel and a bounded join, which has to fit between uvicorn's own three
second grace and `Service.qml`'s SIGKILL five seconds after SIGTERM.

**It is closed from the ASGI lifespan shutdown, not from the end of `main`.**
Uvicorn restores the default signal handler and re-raises the signal that
stopped it, so this process dies *by signal* — exit status 143 — and nothing
after `uvicorn.run()` runs: not a `finally`, not `atexit`, not a non-daemon
thread. `activity.Closing` is a pure-ASGI wrapper, for the same reason `auth.py`
is one, that closes the log while uvicorn is still waiting for the lifespan to
complete. Finding F28, and the reason to run a daemon before believing its
shutdown path.

**Loss is bounded and never silent.** A full queue drops the record, counts it,
warns once on stderr, and the writer emits `{"event":"dropped","n":N}` into the
file as soon as it catches up. An unexplained gap in an audit trail is worse
than no audit trail. A write that fails outright says so on stderr every time,
raises one notification, and keeps serving: the daemon's job is not the log.

**Output is never written.** Arguments are, truncated — they are what the agent
asked for. OCR text and clipboard reads are the contents of the user's screen,
and a record of those is a different and much worse artefact than a record of
actions. The file is still `0600` in a `0700` directory, because an argument can
be text the user copied.

**The filename is configurable; the directory is not.** `log.activity_file` is a
name with no separator in it, always joined onto the state directory. A log
inside the plugin directory would make Omarchy reload the shell once per tool
call.

## Consent, and what silence means

`consent.py` is the third gate. It owns the *rule* and not the mechanism: it
takes anything that can be awaited for an answer, which is what let it be
written and tested before any client was involved — and what saved it when the
mechanism had to change. MCP elicitation was the plan; it turned out that the
protocol revision Claude Code negotiates carries no server-initiated requests at
all, so `ctx.elicit` fails inside this process before reaching the wire. The
question goes to a desktop notification instead, and not a line of the rule
changed.

Six outcomes, and exactly one of them runs the command:

| Outcome | The agent is told |
|---------|-------------------|
| `accepted` | — proceeds, carrying whatever the user typed |
| `declined` | The user refused this specific call |
| `cancelled` | The user dismissed the prompt without deciding |
| `timed_out` | Nobody answered; assume nobody is at the desk |
| `unsupported` | This client cannot ask anyone; write an `allow` rule in `permissions.json` |
| `unreachable` | The client disconnected mid-question |

They are distinct because each implies a different next move. An agent that
cannot tell *the user said no* from *nobody was there* will either give up on a
call the user would have allowed, or keep re-asking an empty room.

Two properties are load-bearing, and neither should be relaxed when N4 lands:

- **The deadline is the decision.** At `askTimeoutSeconds` (60s by default,
  bounded 5–600) the awaitable is cancelled and its result is never read. A
  click that arrives a second late has nowhere to go: the agent has already been
  told the call was refused and may have done something else since. This daemon
  starts with the session and outlives whoever walked away, so a prompt that
  granted on expiry would grant to an empty room.
- **A client that cannot ask is not a client that said yes.** No capability
  means refuse, never hang and never assume. What counts as *can be asked* had
  to be loosened once: Claude Code declares a bare `elicitation: {}`, naming
  neither sub-mode, and requiring `form` refused the client this project exists
  for. A client naming only `url` is still refused — url mode answers in a
  browser tab, and the person here is at a desktop.

An exception from the asker is caught, not propagated: a broken prompt surfacing
as a tool-call traceback would bury the reason the command did not run.

## Asking at call time

`gate.py` is where `policy.py`, `permissions.py`, `resolve.py` and `consent.py`
meet. Both tool paths — `_shared.run_route` for the curated tools, and `omarchy_run` — reduce to
one `await` on it, because a check that one path applies and the other skips is
worse than no check at all.

The order is the part worth stating:

| Outcome | What happens |
|---------|--------------|
| `allow` | resolve, run |
| `ask` — a rule, or the guarded default | resolve, **then** ask, run only on an accept |
| `deny` | refuse; nothing is resolved and nobody is asked |

Resolution comes before the question and only on the ask path. Before, because a
prompt reading *"set theme Tokyo Night"* is consent and one reading *"run
omarchy theme set"* is not — the user cannot tell what they are approving. Only
on that path, because a refusal nobody will be asked about should not spend a
subprocess on a resolver, and because a question answered *yes* and then refused
as unresolvable has spent something scarcer than a subprocess.

`deny` covers both refusals that must never become questions: `blocked`, because
no answer makes a sudo command runnable, and a `deny` rule, because that refusal
is a decision the user already took by hand. They are one `Effect` rather than
two flags precisely so a caller cannot handle one and forget the other.

Ahead of all of it sits `policy.self_refusal`, which reads the call's
*arguments* rather than its route: `omarchy shell <this plugin> stop` and
`omarchy plugin remove <this plugin>` are refused before a tier is even
computed. It cannot be part of the ladder, because the same route is fine or
refused depending on what it names, and the ladder never sees arguments.

The question itself goes wherever it can reach a person. If the client declares
elicitation *and* the transport can carry a server-initiated request, it goes
there. Otherwise it becomes a critical notification whose `--exec` runs a helper
when clicked, which writes a one-time token the parked call is watching for.
That surface has exactly one action, so a click is *yes* and silence is *no* —
the mechanism and the rule agree by construction.

Every tool is `async` for this reason, and every blocking call therefore has to
be handed to a worker thread explicitly: `func_metadata` only threads a tool it
finds to be *sync*, so an `async` body doing subprocess work would stall every
other client — including one parked on a question, which is the concurrency the
single-daemon design exists to provide.

## Which binary actually runs

`argv[0]` is resolved against a fixed list -- `$OMARCHY_PATH/bin`,
`/usr/local/bin`, `/usr/bin` -- rather than against the `PATH` this process
inherited from the session.

**This is robustness, not a security control,** and it is deliberately not in
`SECURITY.md`. No MCP client can influence this daemon's environment, so the
attack it would defend against does not exist here. What it defends against is
an ordinary desktop: on the machine this was written on, the daemon's inherited
`PATH` was `/usr/share/omarchy/bin`, then fifty-five toolchain-manager shims,
and only then `/usr/bin`. Which `tesseract` an OCR call used was therefore a
property of what the user had most recently installed, and would change without
anything in this project changing.

Two details are load-bearing:

- **`Popen(argv, executable=...)`, not a rewritten `argv[0]`.** The process runs
  the file that was chosen while the command reported back to the agent stays
  the copy-pasteable `omarchy theme set` that the README and `TOOLS.md` show.
  Which file ran goes in the log line, where it answers *which one* without
  costing a line of the agent's context on every call.
- **The environment is passed through untouched, `PATH` included.** What a
  command looks up for *itself* is its own business: `omarchy launch editor` is
  supposed to find the editor this user installed, wherever that is. Omarchy's
  own dispatcher resolves its `omarchy-*` helpers relative to its own location
  rather than through `PATH`, so choosing the right `omarchy` already settles
  every subcommand.

The other half is the error. `execute.run` was the one spawn site that let
`FileNotFoundError` escape, and the SDK strips the cause, so a renamed `omarchy`
reached the agent as *"Error executing tool omarchy_run"* and nothing more. It
now raises `NotInstalled`, naming the binary and the directories searched --
facts, rather than a guess at which package ships it.

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

That switch is live. `tools/catalogue.py` separates declaring a tool from
offering one: `register()` records the function and its arguments, and `apply()`
adds and removes tools on the running server to match a config. It is the same
call at startup and at reload, so there is no second path for the live case to
drift from, and a disabled tool stays *absent* rather than present-and-refusing
— which is the point of the switch, since a tool the model cannot see is one
prompt injection cannot talk it into trying.

`TOOLS.md` is generated from the server's own schemas and `make check` fails if
it is stale. Never edit it by hand.

## Reloading the config without dropping a session

The configuration used to be read once. A tool switched off while a client was
attached stayed offered until a restart, and `reloadConfig` was a restart —
which drops every attached MCP session, so the fix cost more than the problem.

Three pieces:

- **`settings.py`** holds which `Config` is in force. It stops at the edge: a
  tool body takes one snapshot per call and passes it down, so `policy.py`,
  `gate.py`, `consent.py` and `execute.py` still take an immutable `Config` and
  one call is always decided by one config. A reload landing between the gate
  and the executor cannot authorize under one set of rules and run under another.
- **`reload.py`** polls the file every two seconds and applies it. `SIGHUP`
  short-circuits the wait, which is what `reloadConfig` and the panel's button
  now send. A poll rather than a file watch because the process that reacts owns
  the trigger: it works while the shell is restarting, and when the daemon is run
  by hand.
- **`clients.py`** tells attached clients the tool list moved — over the
  `SubscriptionBus` for 2026-07-28 clients, and per connection for everyone
  else, from a register a server middleware fills in.

What is live: `tools.disabled`, everything under `[policy]`, `server.timeout_ms`
and `server.max_output_b`. What is not: `server.port`, because the socket is
bound, and the activity log's settings, because the sink is open.

**A file that does not parse changes nothing.** `config.load` answers a broken
file with defaults, which is right at startup — a daemon that refuses to start
over a typo looks like one that was never installed — and wrong on a reload,
where it would switch every disabled tool back on for a stray keystroke. So the
running config stands, a notification says so, and the bar panel says so until
it parses again. A file that has merely gone missing waits one poll first:
editors write a temporary file and rename it over the target, and absent-once is
that gap rather than a deletion.

`permissions.json` is watched by the same poll and reloads on the same two
seconds, but its failure rule is not the same. At startup a defective document
stops the daemon: there is no known-good one to keep, and "no rules" is not the
safe floor, because a hand-written `deny` demotes routes the derivation calls
safe. At reload there *is* a known-good one — the document the user last
successfully wrote — so it stands, and the daemon never exits over an editor's
mid-keystroke autosave, which no debounce could tell from a finished wrong file.
The bar carries `permissionsOk` beside `configOk`: two files that fail
independently and are fixed in different places want two lamps, not one.

`tools/list_changed` is announced only when the tool set actually moves. A
policy edit changes what a route is allowed to do, not what the tool list says,
and a client that re-listed would learn nothing.

## Resources, and who they are for

Eight, and they are not a second tool surface. Tools are how an agent acts;
resources are how a person reads. In Claude Code they appear as `@` mentions the
user types, so they are chosen for what someone building an Omarchy plugin would
want to pull into a conversation — above all `omarchy://shell/targets`, the
shell's IPC surface with full signatures, which is documented nowhere upstream.

Five are concrete and appear in the `@` menu. Three are URI templates covering
every command and every target without putting several hundred entries in a
listing. Claude Code never enumerates templates (finding F8), so the concrete
four are the discoverable set; templates still resolve when read by URI, and
other clients may list them.

## What the tests pin

`policy.py`, `permissions.py`, `auth.py`, `execute.py`, `gate.py` and `prompt.py`
are the security boundary, and the existing tests are its specification — every
sudo command classifies `blocked` and no rule can promote it, `deny` beats `ask`
beats `allow` with specificity never reordering it, a route whose argument is a
command line is never granted, any defect at all refuses the permissions
document, `argv` never reaches a shell, a foreign `Origin` gets 403 and a
foreign `Host` gets 421, nothing that must not be asked about is asked about,
and a consent file that merely exists is not a click. Changes there need tests
in the same commit.

`tests/test_activity.py` pins the log's own promises, which are properties
rather than examples: OCR text and clipboard reads never reach the file while a
clipboard *write* does, one line is written per call however the call ended, a
refusal is recorded as a refusal rather than a success, the file is `0600` in a
`0700` directory and stays bounded, and a broken disk neither raises into a tool
call nor toasts more than once.

The suite reads committed snapshots rather than the installed system, and
autouse fixtures enforce it. That is what lets the tests run in CI at all, and
it stops them quietly changing meaning the next time `omarchy update` renames a
route (finding F21).

| Snapshot | Of | Pinned for |
|----------|----|-----------|
| `commands.json` | `omarchy commands --all --json` | `registry.all_commands` |
| `themes.txt` | `omarchy theme list` | `resolve._themes` |
| `monitors.json` | `hyprctl -j monitors`, trimmed | `resolve._monitors` |
| `ipc-show.txt` | `qs ipc show` | the shell parser tests |

Refresh a snapshot deliberately, in its own commit. `monitors.json` is the one
that is edited rather than captured: it carries two monitors so that a wrong
name can be shown being refused rather than taken as the only candidate, and
the serial number is dropped. A `needs_omarchy` test checks that `themes.txt`
still shares a theme with the machine it runs on, so a snapshot that has rotted
away from any real Omarchy says so.

### Nothing in the suite reaches the machine it runs on

Two more autouse fixtures, and they exist because the suite once could. Three
tests used `omarchy system reboot` as their example of a guarded route, on the
reasonable assumption that a guarded route is refused and nothing happens. When
the guarded default became `ask`, that refusal became a real critical
notification on a real desktop; it was clicked, in good faith, and the machine
rebooted mid-run (finding F29).

- **`_no_real_omarchy`** fails any attempt to spawn `omarchy`, `omarchy-shell`,
  `hyprctl`, `qs` or `wl-copy` against the live system, naming the argv. A test
  that points `execute.SEARCH` at a fixture directory is building its own fake
  binary and is left alone; so is one marked `needs_omarchy`, which is the
  existing opt-in for reading the installed system.
- **`_no_desktop_prompts`** stops `prompt.send` and `prompt.dismiss` reaching
  the desktop at all. A notification the suite raised is a machine only
  pretending to ask, and answering it is how a person ends up inside a test run.

`tests/test_conftest_guards.py` tests both, because a guard nobody exercises is
one that stops working on the next refactor and says nothing. The lesson it
encodes is not "pick a gentler example route" — it is that a suite must not be
one behaviour change away from executing whatever it names.
