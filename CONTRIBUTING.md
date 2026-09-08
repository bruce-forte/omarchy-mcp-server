# Working on this repo

An Omarchy plugin: a Python daemon serving MCP over loopback HTTP, plus two QML
surfaces hosted by `omarchy-shell`.

Read these first, in this order:

| File | For |
|------|-----|
| [`ARCHITECTURE.md`](ARCHITECTURE.md) | How it works and why it is shaped this way. **Start here before touching the source** |
| [`SECURITY.md`](SECURITY.md) | The threat model. Read before changing `policy.py`, `permissions.py`, `auth.py`, or `execute.py` |
| [`ROADMAP.md`](ROADMAP.md) | Decisions with their reasons, phases, and what was rejected — check before proposing a feature |
| `README.md` | The user-facing side |

Each of those opens with a **Start here** section that says what it covers and
routes to the rest of it. Keep them accurate: a claim in a document nobody has
re-read is worse than no document, and the counts in them (nineteen tools, eight
resources, thirteen IPC verbs) are the ones that rot first.

This file is the working agreement. It is `CONTRIBUTING.md` rather than
`CLAUDE.md` on purpose — see **No agent-control file ships** below.

## Conventions for this repo

- **Commit straight to `master`.** No branches.
- **No agent-control file ships.** No `CLAUDE.md`, `AGENTS.md`, `GEMINI.md`,
  `SKILL.md`, `.claude/`, `.codex/`, `.cursorrules` — at any depth.
  `omarchy plugin add` clones this repository into
  `~/.config/omarchy/plugins/<id>/`, and a coding agent working in or above that
  directory reads such a file and obeys it, without anybody choosing to. That is
  an instruction channel into somebody else's agent that no reviewer of this
  daemon ever saw, and it is worst in a plugin that hands an agent command
  execution. `make agents` checks `git ls-files` against the usual names and
  fails on a match; `.gitignore` lists them, so your own tooling can keep
  writing them locally. Contributor documentation goes here, under a name no
  agent auto-loads. ROADMAP N18 has the history.
- **No AI attribution in commit messages.** No `Co-Authored-By`, no
  `Generated with`, no session trailer.
- Run `make check` before committing. It runs the tests, `ruff`, `pyright`,
  `qmllint`, `shellcheck`, `omarchy plugin validate`, `make agents`, and three
  staleness gates:
  that `TOOLS.md` matches the server's current schemas, that
  `permissions.schema.json` matches the pydantic models that enforce it, and
  that `config.example.toml` still pins no defaults. CI runs the same things.
- **Use the `Makefile`, not bare `uv`.** It sets `UV_PROJECT_ENVIRONMENT` so the
  dev virtualenv lands outside the repository. A `.venv` here makes
  `omarchy plugin validate` fail, because it rejects symlinks inside a plugin
  folder.
- Regenerate `TOOLS.md` with `make tools` whenever a tool's name, description,
  schema, or annotations change, and `permissions.schema.json` with
  `make schema` whenever `permissions.py`'s models change. Both are generated;
  never edit either by hand, and `make check` fails if either is stale.
- **Run `make lsp` once if your editor reports unresolved imports.** `make lint`
  finds the virtualenv because it goes through `uv run`; nvim, VS Code and Zed
  launch `pyright` directly, look for a `.venv` beside `pyproject.toml`, and find
  none — it is outside this directory on purpose. `make lsp` writes a
  git-ignored `pyrightconfig.json` naming the real one. It is a full copy of
  `[tool.pyright]` plus two keys, because pyright replaces that table rather
  than merging with it, so re-run it after changing those settings; `make check`
  fails if the file exists and has gone stale.
- **Every parameter in `src/` and `examples/` carries a type.** `pyright` is
  configured to fail on one that does not. `tests/` is deliberately exempt: an
  unannotated `tmp_path` or `monkeypatch` is a pytest fixture whose name is its
  type, and spelling those out would be a thousand annotations that say nothing.
  Prefer the real type. `Any` is a legitimate answer where the code genuinely
  accepts anything — `gate.py`'s `ctx`, read only through `getattr` — but say so
  in a comment where you use it.
- **A pydantic model's docstring is published.** It becomes the object's
  `description` in `permissions.schema.json`, so adding one to a `BaseModel` in
  `permissions.py` makes the schema stale on a purely editorial change. Document
  those classes with a comment *above* the class instead. Field descriptions are
  the opposite case: they are written for the schema, and belong in `Field(...)`.
- Tests read committed snapshots in `tests/fixtures/`, never the installed
  Omarchy: `commands.json` (`omarchy commands --all --json`), `themes.txt`
  (`omarchy theme list`), `monitors.json` (`hyprctl -j monitors`, trimmed by
  hand), `ipc-show.txt` (`qs ipc show`). Autouse fixtures pin all of them, pin
  every path the daemon writes to, and stop any test spawning a real `omarchy`
  or raising a real notification. That is what lets the suite run in CI, it stops
  tests changing meaning the next time `omarchy update` renames a route, and it
  stops the suite driving — or writing to — the machine it runs on. Refresh a
  snapshot deliberately, in its own commit.
- A tool argument that names something — a theme, a monitor, a path, a URL —
  gets a resolver in `resolve.py` and a route in its table, so the refusal
  happens before anything is spawned and the call carries a human label for the
  approval prompt N4 puts on screen. No resolver without a source of truth:
  package names have none, and refusing one that is not installed yet would
  refuse every install.

## The security boundary

`policy.py`, `permissions.py`, `auth.py`, `execute.py`, `gate.py` and
`prompt.py` are the boundary. Changes to them need tests in the same commit, and
the existing tests are the specification:

- Every sudo command classifies `blocked`, and **no rule** can promote it.
  Naming one in `allow` or `ask` stops the daemon rather than being void.
- **`safe` is an allowlist.** A command group in neither `SAFE_GROUPS` nor
  `GUARDED_GROUPS` classifies `guarded`, not `safe`, so a group Omarchy adds
  after the last release asks rather than running unattended (N17). Refreshing
  `tests/fixtures/commands.json` fails
  `test_every_group_omarchy_ships_is_classified` until every new group is put in
  one list or the other — that is the decision the design exists to force, and
  it is made at a keyboard.
- `deny` → `ask` → `allow`, first match wins, and specificity never reorders it.
- A route whose own argument is a command line (`NEVER_STORE`) may be asked
  about and never granted.
- **Any** defect in the permissions document refuses it. At startup that means
  the daemon does not start, exit `78`; at reload the last good document stands.
- **Restrictions extend forward, grants do not.** A route that appeared under an
  existing `allow` is held at `ask` until the review is acknowledged; `deny` and
  `ask` cover a new route the moment it arrives.
- The registry snapshot advances **only** on acknowledgement — never on startup,
  except the first run, which has nothing to compare against.
- `argv` never passes through a shell. `tests/test_execute.py` writes a canary
  file and asserts it survives an injection attempt.
- Missing, wrong, and truncated tokens are all rejected; `/health` is the only
  route without one.
- A foreign `Origin` gets 403, a foreign `Host` gets 421.
- Nothing is asked about that must not be: `blocked` and a `deny` rule are
  refused before a question exists. Only an accept runs anything.
- A question put enough times is not put again. `cooldown.py` refuses before a
  prompt is assembled, so a suppressed call costs no notification and no
  resolver. It is in memory on purpose: a nag-guard is not a permission.
- A consent token is both the filename and the contents, and is never given to
  the model. A file that merely exists is not a click.
- An agent cannot switch off its own supervision. `policy.self_refusal` reads a
  call's *arguments*, so it sits ahead of the tier rather than in it, and covers
  all three doors: `omarchy_shell_call`, the `omarchy shell` route reached
  through `omarchy_run`, and `omarchy plugin disable|remove|…` naming this
  plugin.

Do not add a config key for the listen address. See `SECURITY.md`.

**Nothing in the test suite may reach the machine it runs on.** Autouse
fixtures in `conftest.py` enforce it, and `tests/test_conftest_guards.py` tests
them. They exist because the suite once rebooted the developer's machine — see
`ROADMAP.md` F29 — and the rule that came out of it is that a suite must not be
one behaviour change away from executing whatever it names. Do not weaken them
to make a test pass; the opt-ins are to redirect `execute.SEARCH` at a fixture
directory, or to mark the test `needs_omarchy`.

**Nor may it write there.** `conftest.WRITTEN_PATHS` pins every module-level
*binding* of every path the daemon writes — bindings rather than one constant,
because `from .paths import X` copies the value at import and patching `paths`
would reach nobody. A module that starts writing somewhere new goes in that
list; `test_every_written_path_is_pinned` fails until it does. This has gone
wrong twice, with the activity log and with `registry-seen.json`, and the guard
then found that the suite could rotate the user's bearer token.

## How the source is documented

The audience is somebody who has not written much Python, and the two halves are
kept apart on purpose:

- **A module docstring says why the module exists**, what it may not do, and
  which finding or decision forced its shape. That is the half that cannot be
  recovered by reading the code.
- **An inline comment explains the Python** wherever the language is the hard
  part rather than the domain: what a context manager guarantees, why `argv` is
  a list, what a decorator is deferring, why a comparison uses `is`.

`src/omarchy_mcp/__init__.py` carries the package's own primer — a reading order
and the idioms that recur everywhere — so an individual module never has to
re-explain `from __future__ import annotations`.

Match that when you add code. A `#:` comment documents the constant on the next
line, and a function whose name does not already say what it returns gets a
docstring saying it.

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
  of what an agent actually did to the desktop, alongside `activity.jsonl`,
  which keeps it after the daemon is gone.
- **The activity log never carries command output.** OCR text, clipboard reads
  and stdout are the user's screen, not the agent's action. Arguments are
  written, truncated. Every tool records through `stats.call(...)` — one record
  per call, on every exit path — never by writing to `activity.py` directly.

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

Note `shell call <id> <method> <arg>` is a different, lower-level route. It
reaches loaded **panel, overlay and menu** plugins only — never a service, never
a bar widget — because it looks the plugin up in the loader map that only those
three kinds are in. `IpcHandler` is the one that shows up in the listing.

### A bar widget exists once per monitor

The bar is a surface per screen, so a `bar-widget` entry point is instantiated
once per screen. Nothing about the QML says so, and on a single-monitor machine
nothing will ever reveal it.

Three consequences worth designing around:

- **An IPC target routes to exactly one instance.** Whichever registered it. The
  base class carries `broadcast(method)` for this — it fans a call out to every
  live instance through `bar.moduleWidgets(moduleName)` — and the shell routes
  `summon`/`hide`/`toggle` to the live widget rather than through a target for
  the same reason.
- **Anything a widget reads, it reads N times.** A file poll, a `Process`, an
  HTTP probe: three monitors mean three of them. Put that work in the plugin's
  *service*, which is instantiated once, and let the widgets read the result off
  the service object.
- **Per-widget state is per screen.** A panel opened on one monitor is closed on
  the others unless something coordinates them.

### A bar widget is allowed to be a panel

`entryPoints.barWidget` does not have to be a plain bar item. Pointing it at a
component rooted in `qs.Ui`'s `Panel` gives one click-to-open popup that is both
the bar button and its panel — `omarchy.power`, `.network`, `.audio` and
`.agents` are all built this way, and the `Ui` kit has the pieces to fill it
(`KeyboardPanel`, `PanelKeyCatcher`, `PanelSectionHeader`, `PanelSeparator`,
`PanelActionButton`, `Button`, `PopupCard`, `ConfirmDialog`).

The six valid `kinds`, and the `entryPoints` key each one wants:

| kind | entry point key |
|------|-----------------|
| `bar` | `bar` |
| `bar-widget` | `barWidget` |
| `menu` | `menu` |
| `overlay` | `overlay` |
| `panel` | `panel` |
| `service` | `service` |

Two things the shell does not say out loud:

- **The bar host recognises a panel by duck-typing**, not by kind: the root item
  needs `open()`, `close()` and `opened`, all three, or `shell summon/hide/toggle`
  will not reach it. `Panel` provides them.
- **Declaring `panel`, `overlay` or `menu` alongside `bar-widget` changes the
  routing.** The panel loader takes ownership and summons stop going to the live
  bar widget. If the popup belongs to the bar icon, `bar-widget` alone is the
  kind you want.

`Panel` also registers a free `IpcHandler` from its `ipcTarget`. Leave that empty
if the plugin's service already owns the plugin-id target — a target only ever
routes to one handler.

**A control that acts on this plugin belongs in the panel**, next to Stop,
Restart and Copy client config. It is the surface a person can find without
knowing the plugin has an IPC target at all, and it can ask a question a
notification cannot: a notification carries one action (F25), a panel carries
as many buttons as the decision needs.

The panel is tabbed — Summary, Log, Rules — with a parked question above the
strip and the daemon's buttons below it on every tab. So a new control goes in
the tab it belongs to, and only something that must never be missed earns a
place in one of the two permanent bands.

Add the IPC verb too, and name it for the caller — the `qs ipc show` listing is
how a script or an agent discovers the same capability. But the panel is the
surface a person gets.

### What FileView will and will not do

Two limits worth knowing before designing around it:

- **It has no seek.** `text()` is the whole file. Watching a log that grows to a
  megabyte means pulling a megabyte into the shell process on every write.
- **It can write a file and cannot remove one.** A marker file that has to mean
  two things should say which in its *contents*, rather than existing or not.

### A widget cannot be *called*, but it can *reach* a service

Two different things, and this plugin got the rule wrong for a phase.

`shell call <id> <method>` routes through the loader map, which only
panel, overlay and menu plugins are in. It reaches neither services nor bar
widgets, and no amount of `IpcHandler` changes that.

A bar widget can nonetheless reach a service object directly:

```qml
readonly property var service: bar && bar.shell && bar.shell.serviceFor
  ? bar.shell.serviceFor(moduleName) : null
```

`shell.serviceFor(pluginId)` has no first-party restriction, and the shell loads
both kinds into one QML engine. `omarchy.media` uses it between its own service
and its own widget.

Two things to know before relying on it:

- **The widget is constructed before the service exists**, so the first
  evaluation is always null. It resolves only because a *binding* re-runs when
  the shell's service map changes; the `Component.onCompleted` version of that
  line latches null forever. Keep a file fallback for the window in between.
- **Do not reach for a plugin-local `pragma Singleton`** to share state instead.
  Relative-path singleton imports give each importer its own copy; the shell
  says so twice in its own source, and injection is what it does instead.

**Use this surface for anything new.** Whatever the user should be able to see
or change about this plugin goes in the panel, and reaches the daemon through
the service object. Not a new file under `$XDG_RUNTIME_DIR` for the widget to
poll, and not a new `IpcHandler` verb whose only caller is QML.

The state file that exists is not a pattern to copy. It is a fallback for the
one window where `serviceFor` has not resolved yet, and it stays that size:
whether the daemon is up, on what port, and what it last did. New state belongs
on the service object, where a binding already updates the widget.

N10 did exactly this: the pending question and the delta review both live on the
service object, and the panel reads them from there. **Read-only IPC verbs
only** — an agent can reach this plugin's own target, so a verb that granted a
permission or acknowledged a review would let it permit itself. `status`,
`recent`, `pending`, `review` and `permissions` report; answering, acknowledging,
pruning and removing a grant go through `bin/omarchy-mcp-consent`, each with a
token the daemon publishes only on the frame the shell reads. Keep new verbs on
that side of the line.

Note only `status` and `recent` are answered to an *agent* — `policy.py`'s
`SELF_READ_VERBS` — so a new read verb is for a person at a terminal, and the
list above is what `qs ipc show` offers, not what an agent may call.

### Reload rules

- Editing Python: `omarchy-shell io.github.bruce-forte.mcp-server restart`
- Editing QML: `omarchy restart shell` — the shell holds the object it already
  instantiated, so neither saving the file nor `rescanPlugins` swaps it in
- Editing `~/.config/omarchy/mcp/config.toml` or `permissions.json`: nothing.
  The daemon re-reads both within two seconds and applies them in place — tools
  appear and disappear on attached clients, and a rule change takes effect on
  the next call. `omarchy-shell io.github.bruce-forte.mcp-server reloadConfig`
  sends `SIGHUP` and skips the wait; it is no longer a restart. `server.port`
  and the `[log]` activity settings still need one, because the socket is bound
  and the log is open.
- A **broken** `permissions.json` behaves differently depending on when it is
  read. At reload the last good document stands and the bar says so. At startup
  the daemon does not start at all, and exits `78` so `Service.qml` stops
  respawning rather than blaming the venv. `omarchy-mcpd --check-permissions`,
  or the panel's **Check permissions** button, validates a fix without
  restarting anything.
