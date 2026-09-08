# MCP Server for Omarchy

Lets a local AI agent drive your Omarchy desktop: every command in Omarchy's
registry, plus the live shell's IPC targets, exposed over MCP on loopback.

Runs as an **Omarchy plugin**, so there is no systemd unit to enable, no second
install step, and no separate package. The plugin supervises a small daemon; the
daemon starts with your session and stops with it.

## Getting started

### 1. Install the plugin

```bash
omarchy plugin add https://github.com/bruce-forte/omarchy-mcp-server.git --enable
```

The first run builds a Python environment under
`~/.local/state/io.github.bruce-forte.mcp-server/` — a second or two, and it
needs the network once. Nothing else is installed and no service is enabled;
`omarchy-shell` supervises the daemon from now on.

A **plug icon** appears in your bar. That icon is the whole UI: it says whether
the server is serving, and clicking it opens the panel where everything else
happens. → [Install](#install)

### 2. Give yourself a shortcut to the CLI

The daemon's own command lives inside the plugin directory and is **not** on
your `PATH`. Everything below, and the panel itself, refers to it as
`omarchy-mcpd`, so make that true:

```bash
echo "alias omarchy-mcpd='~/.config/omarchy/plugins/io.github.bruce-forte.mcp-server/bin/omarchy-mcpd'" \
  >> ~/.bashrc && source ~/.bashrc
```

Skip this if you would rather type the full path each time. It is only ever
needed from a terminal — the bar panel needs none of it.

### 3. Connect your client

The server requires a bearer token, generated on first run. Print the exact line
to run:

```bash
omarchy-shell io.github.bruce-forte.mcp-server clientConfig
```

which gives you something like:

```bash
claude mcp add --transport http omarchy http://127.0.0.1:8765/mcp \
  --header "Authorization: Bearer <your token>"
```

Run it, and the agent has the desktop. More than one client can attach at once.
→ [Connecting a client](#connecting-a-client)

### 4. Check it is actually serving

```bash
omarchy-shell io.github.bruce-forte.mcp-server status
```

Or just look at the bar: the plug icon shows `!` when the daemon is not serving,
and blinks when an agent makes a call. → [Checking it works](#checking-it-works)

### 5. Ask the agent for something

Try _"what theme am I using, and what else is installed?"_, then _"switch to
Tokyo Night"_. Both just happen — **no prompt**, and that is correct. Switching
a theme is reversible in one more sentence, so it is `safe` and it runs.

Now ask for something that is not: _"install cowsay"_. A **critical
notification** appears on your desktop naming the command and what its arguments
resolved to. Clicking it opens the panel, where **Allow once**, **Always** and
**Deny** are. Press **Deny** — nothing runs, and you have seen the whole
mechanism.

That is the shape of it, and the line is **not** "reads run, writes ask". Most
things an agent does to your desktop — themes, wallpapers, volume, brightness,
launching apps, opening windows — are `safe` and run without asking, because
undoing them is another sentence to the agent. What asks is the tier called
`guarded`: installs, removals, migrations, reboots, shell plugins. What is never
allowed at all is anything needing sudo.

If that split is not the one you want, it is yours to change — a `deny` rule
refuses a route outright, and an `ask` rule puts a route you consider risky
behind a prompt even though it derives as safe.
→ [What an agent is allowed to run](#what-an-agent-is-allowed-to-run)

### What to do next

| If you want to…                                               | Go to                                                                                                                           |
| ------------------------------------------------------------- | ------------------------------------------------------------------------------------------------------------------------------- |
| Stop being asked about a command you always approve           | Press **Always** on the prompt, or write an `allow` rule — [Being asked, and writing it down](#being-asked-and-writing-it-down) |
| See what an agent has actually done to your desktop           | [Seeing what it did](#seeing-what-it-did)                                                                                       |
| Understand which commands ask and which do not                | [What an agent is allowed to run](#what-an-agent-is-allowed-to-run)                                                             |
| Turn a tool off, or change the port                           | [Configuration](#configuration)                                                                                                 |
| Know exactly what a hostile web page can and cannot do to you | [`SECURITY.md`](SECURITY.md)                                                                                                    |
| Read the tool reference                                       | [`TOOLS.md`](TOOLS.md)                                                                                                          |

## How this works

Four pieces, one process each doing one job:

```
  omarchy-shell (Quickshell)
    │
    ├── Service.qml ──spawns──> bin/omarchy-mcpd ──> python -m omarchy_mcp
    │        │                    (bootstrap)            (the daemon)
    │        │                                             │
    │        │<─── state + one line per tool call ─────────┤
    │        ├──── polls GET /health every 10s ────────────┘
    │        │
    │        └── IpcHandler: status, recent, review, permissions, start, stop, …
    │
    └── BarWidget.qml ── the plug icon, and the panel behind it
```

- **The daemon** is a small HTTP server bound to `127.0.0.1`, speaking MCP behind
  a bearer token. It holds the policy, the permissions, and the activity log.
- **`Service.qml`** supervises it: starts it with your session, restarts it if it
  dies, and probes `/health` rather than trusting that the process exists — a
  wedged HTTP loop still has a live pid.
- **`BarWidget.qml`** is the plug icon and its panel. It is the surface you use
  to answer a question, read what happened, and see your rules.

**Nothing here is a hand-written catalogue.** `omarchy commands --json`
describes every command Omarchy ships; the daemon reads that listing live and
classifies each route by rule rather than by a list, so an `omarchy update` that
adds or renames commands needs no change here.

When an agent calls a tool, the daemon does four things in order: works out what
kind of command it is, applies your rules, resolves what the arguments actually
name on _your_ machine, and asks you if that is what your rules call for. Only
then does it spawn anything — as an argument list, never through a shell.

For the reasoning behind each of those, read [`ARCHITECTURE.md`](ARCHITECTURE.md).

## Documentation

| File                                                   | For                                                         |
| ------------------------------------------------------ | ----------------------------------------------------------- |
| `README.md`                                            | Using it — install, connect a client, configure, uninstall  |
| [`TOOLS.md`](TOOLS.md)                                 | The tool reference, generated from the server's own schemas |
| [`ARCHITECTURE.md`](ARCHITECTURE.md)                   | How it works. Start here to read the source                 |
| [`SECURITY.md`](SECURITY.md)                           | What an agent can and cannot do, and why                    |
| [`ROADMAP.md`](ROADMAP.md)                             | What is done, what is left, what was decided against        |
| [`CLAUDE.md`](CLAUDE.md)                               | Working agreement, and Omarchy plugin conventions           |
| [`permissions.example.json`](permissions.example.json) | A starting point for your own rules                         |
| [`permissions.schema.json`](permissions.schema.json)   | The schema your editor validates them against               |

## Contents

- [Getting started](#getting-started)
- [How this works](#how-this-works)
- [What this is](#what-this-is)
- [What it does](#what-it-does)
- [Install](#install)
- [Connecting a client](#connecting-a-client)
- [Checking it works](#checking-it-works)
- [Configuration](#configuration)
- [What an agent is allowed to run](#what-an-agent-is-allowed-to-run)
- [Seeing what it did](#seeing-what-it-did)
- [The command line](#the-command-line)
- [Development](#development)
- [Troubleshooting](#troubleshooting)
- [Uninstall](#uninstall)
- [Requirements](#requirements)
- [Security](#security)
- [License](#license)

## What this is

One process that exposes the whole of Omarchy to whatever agent you point at it,
rather than a chosen subset of it wrapped in hand-written tools.

**Complete coverage that maintains itself.** Every command in Omarchy's registry
is reachable, including ones added by a release published after this one. The
registry is read live and classified by rules rather than by a list, so there is
no catalogue here to keep in step and nothing to update when `omarchy update`
renames a route.

**The shell's IPC surface at all.** The bar, the OSD, media, notifications, and
every loaded plugin are reachable only through Quickshell IPC, which no command
covers. `qs ipc show` is the only documentation these interfaces have, and this
server republishes it with full method signatures.

**Resources, so a person can read what an agent can do.** Tools are for acting;
resources are for reading. In Claude Code they are `@` mentions — the whole
annotated registry, every IPC target, the desktop's current state — readable
without running anything, by you as much as by the agent.

**One daemon, many clients.** Claude and Codex attached at the same time share
one policy, one configuration, and one place where approvals are decided,
because there is one process holding all of it rather than one per client.

**It asks you, where you actually are.** A guarded command raises a notification
on your desktop naming what it would do and what it resolved to — the theme, the
monitor, the file. Clicking it opens a panel with **Allow once**, **Always** and
**Deny**; ignoring it refuses. The question reaches you at the desktop rather
than in whichever terminal the agent happens to be running in, because that is
where you are. _Always_ writes the decision down, so you are asked once rather
than every time.

## What it does

Omarchy has two control surfaces, and this exposes both:

- **The command registry.** `omarchy commands --json` describes several hundred
  commands — route, arguments, summary, examples, and whether each needs sudo.
  That listing is read live, so this server never goes stale when Omarchy is
  upgraded and there is no command catalogue to maintain.
- **The shell's IPC targets.** Everything `omarchy-shell` draws — the bar, the
  OSD, notifications, media, and every loaded plugin — is reachable only through
  Quickshell IPC. `qs ipc show` lists them with full method signatures, and that
  listing is the only documentation these interfaces have.

**Four generic tools** cover both surfaces completely:

| Tool                      | Does                                                                  |
| ------------------------- | --------------------------------------------------------------------- |
| `omarchy_search_commands` | Finds commands, with arguments, examples, and whether they can be run |
| `omarchy_run`             | Runs one. Arguments never touch a shell                               |
| `omarchy_shell_targets`   | Lists IPC targets and every method signature                          |
| `omarchy_shell_call`      | Calls one                                                             |

One tool per command would put tens of thousands of tokens of schema into a
client's context before it did anything, so discovery and dispatch are separate.

**Fifteen curated tools** sit on top, each earning its place one of two ways.

Some return what the generic runner structurally cannot — an image, or data that
is not Omarchy's at all:

| Tool                                | Does                                                   |
| ----------------------------------- | ------------------------------------------------------ |
| `omarchy_screenshot`                | Returns the screen as an image, so an agent can see it |
| `omarchy_desktop_state`             | Hyprland's monitors, workspaces, windows, and focus    |
| `omarchy_screen_text`               | OCR, for reading what something says                   |
| `omarchy_clipboard_read` / `_write` | The clipboard, which is not an Omarchy command         |
| `omarchy_system_status`             | Eight probes in one call instead of eight round trips  |

The rest are simply asked for constantly, and a search round trip before every
volume change is a bad trade:

`omarchy_notify`, `omarchy_osd`, `omarchy_theme`, `omarchy_background`,
`omarchy_audio`, `omarchy_brightness`, `omarchy_media`, `omarchy_toggle`,
`omarchy_launch`.

Curated tools go through the same policy check and executor as `omarchy_run` — a
better-shaped door onto the same room, never a way around the lock. Any of them
can be switched off in the config, and everything they do stays reachable
through `omarchy_run`.

**Eight resources** carry the reference material. Tools are how an agent acts;
resources are how a person reads — in Claude Code they appear as `@` mentions:

| URI                       | Holds                                                                      |
| ------------------------- | -------------------------------------------------------------------------- |
| `omarchy://commands`      | The whole registry, annotated with what this server may run                |
| `omarchy://permissions`   | The rules in force, what each covers here, and every route they decide     |
| `omarchy://shell/targets` | Every IPC target with full method signatures — documented nowhere upstream |
| `omarchy://desktop/state` | Monitors, workspaces, windows, focus                                       |
| `omarchy://system/status` | The system status aggregate                                                |

Plus three URI templates — `omarchy://command/{route}`,
`omarchy://commands/{group}`, `omarchy://shell/target/{name}` — which between
them cover every command and target without a listing of several hundred
entries.

Full reference, generated from the server's own schemas: [`TOOLS.md`](TOOLS.md).

## Install

```bash
omarchy plugin add https://github.com/bruce-forte/omarchy-mcp-server.git --enable
```

That clones the repository into
`~/.config/omarchy/plugins/io.github.bruce-forte.mcp-server/`, validates the
manifest, and enables it. Confirm it landed:

```bash
omarchy plugin list | grep mcp-server
```

On first run the plugin builds a Python environment in
`~/.local/state/io.github.bruce-forte.mcp-server/`. This takes a second or two
and needs a network connection once. `omarchy plugin add` deliberately runs no
build and no install hook, so this happens lazily rather than at install time.

## Connecting a client

The server requires a bearer token, generated on first run. Print the exact
command to run:

```bash
omarchy-shell io.github.bruce-forte.mcp-server clientConfig
```

or directly:

```bash
~/.config/omarchy/plugins/io.github.bruce-forte.mcp-server/bin/omarchy-mcpd --print-client-config
```

which prints something like:

```bash
claude mcp add --transport http omarchy http://127.0.0.1:8765/mcp \
  --header "Authorization: Bearer <your token>"
```

For clients configured by file rather than by command, add `--json`:

```json
{
    "mcpServers": {
        "omarchy": {
            "type": "http",
            "url": "http://127.0.0.1:8765/mcp",
            "headers": { "Authorization": "Bearer <your token>" }
        }
    }
}
```

The panel's **Copy client config** button puts the same line on your clipboard
without showing the token on screen, which is the one to use during a screen
share.

## Checking it works

```bash
omarchy-shell io.github.bruce-forte.mcp-server status
curl -s http://127.0.0.1:8765/health
```

`status` reports whether the daemon is _serving_, which is not the same as
running — a wedged HTTP loop still has a live process, so the plugin probes
`/health` rather than trusting the pid.

### The bar widget

The plug icon in the bar says whether the server is serving, and blinks when an
agent makes a call — the only thing on the desktop that marks the moment
something acted on it. Click it for a panel with three tabs:

| Tab         | Shows                                                                                                                                                                                                              |
| ----------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------ |
| **Summary** | whether it is serving and on which port, how many tools are offered, whether either config file failed to load, anything the server has stopped asking about, and a button that opens `config.toml` in your editor |
| **Log**     | the last 30 records, newest first, each with a severity icon and how long ago it happened                                                                                                                          |
| **Rules**   | every rule in force and what it covers, flagged when it is not doing what it looks like it does — with **Remove** on the grants this daemon wrote and **Edit** on each file                                        |

Two things are not in a tab, because they must not be behind one. A question
waiting for an answer sits **above** the tabs, and these buttons sit **below**
them, on every tab:

| Button                 | Does                                                                                                                                |
| ---------------------- | ----------------------------------------------------------------------------------------------------------------------------------- |
| **Stop** / **Start**   | Switches the daemon off, or back on. A Stop lasts across a shell restart and a logout, until you start it again                     |
| **Restart**            | Bounces the daemon. Needed after changing `server.port` or the `[log]` settings; every other key re-reads itself within two seconds |
| **Check permissions**  | Says whether `permissions.json` would let the daemon start — the one control that is useful precisely when it will not              |
| **Reload config**      | Re-reads `config.toml` now rather than within two seconds                                                                           |
| **Copy client config** | Puts the `claude mcp add …` line on your clipboard. It carries the bearer token, so it is never shown on screen                     |

**It is keyboard-driven.** `[` and `]` move between tabs; `j`/`k` or the arrows
walk the controls and `h`/`l` move within a row; `Enter` presses what is lit and
`Esc` closes. On the Log, where there is nothing to press, `j`/`k` scroll
instead. A dim line under the buttons lists whatever applies where you are.
`Tab` is untouched and still moves to the next panel on the bar, as it does
everywhere else in Omarchy.

The panel reads the activity log and the rules from the files, so it still
answers when the daemon is stopped or is refusing to start — which is when the
**Edit** and **Check permissions** buttons matter most. Arguments are not shown
there — see [Seeing what it did](#seeing-what-it-did).

It opens from a keybind or a terminal too:

```bash
omarchy-shell shell toggle io.github.bruce-forte.mcp-server
```

## Configuration

Optional. Everything works without it. A commented template is written to
`~/.config/omarchy/mcp/config.toml` on first run; every key is commented out and
shows its default, so keys you leave alone keep tracking upstream defaults.

```toml
[server]
# port = 8765
# timeout_ms = 30000
# max_output_b = 262144

[tools]
# Curated tools to switch off. Everything they do stays reachable through
# omarchy_run; a disabled tool is absent from the client's list, not refused.
# disabled = ["omarchy_screenshot", "omarchy_clipboard_write"]

[log]
# level = "info"
# activity = true
# activity_max_bytes = 1048576
# activity_file = "activity.jsonl"
```

The full template, with every key explained, is
[`config.example.toml`](config.example.toml).

What an agent may run is **not** in this file. It lives beside it in
`permissions.json` — see [Being asked, and writing it down](#being-asked-and-writing-it-down).

Saving either file is enough. The daemon re-reads both within about two seconds:
tools switch on and off on any client that is already attached, and rule changes
apply to the next call. To not wait:

```bash
omarchy-shell io.github.bruce-forte.mcp-server reloadConfig
```

or press **Reload config** in the bar panel, which does the same thing.

Two exceptions, both needing a restart, because the daemon is already using
them: `server.port` (the socket is bound) and the `[log]` activity settings (the
log file is open).

If the file stops parsing, the daemon keeps running the configuration it already
had rather than falling back to defaults — a stray keystroke must not empty your
`deny` list or switch disabled tools back on. It says so with a notification,
and the bar panel says so until the file parses again.

The listen address is always `127.0.0.1` and is deliberately not configurable.
See [`SECURITY.md`](SECURITY.md).

## What an agent is allowed to run

Commands are classified automatically from the registry, so the policy does not
rot when Omarchy adds commands:

| Tier      | Rule                                                | Behaviour                                                                                                             |
| --------- | --------------------------------------------------- | --------------------------------------------------------------------------------------------------------------------- |
| `blocked` | needs sudo                                          | Refused always. The daemon has no controlling terminal, so a password prompt could never be answered. Not overridable |
| `guarded` | installs, removes, migrates, reboots, shell plugins | Refused unless allowed in your config, or approved by you at the time                                                 |
| `safe`    | everything else                                     | Runs                                                                                                                  |

**The line is what is hard to undo, not what writes.** This surprises people, so
it is worth being explicit: switching your theme, setting a wallpaper, changing
the volume or brightness, launching an app, moving a window and sending a
notification are all `safe`, and an agent does them **without asking you**.
Undoing any of them is one more sentence to the agent. What is `guarded` is the
`install`, `remove`, `migrate`, `update`, `dev`, `plugin` and `snapshot` groups,
plus a handful of individually destructive routes — `omarchy system reboot`,
`omarchy theme remove` and `omarchy restart shell` among them.

If you want the line drawn somewhere else, draw it: an `ask` rule puts a route
behind a prompt even though it derives as safe, and a `deny` rule refuses it
outright. To see exactly where it falls on _your_ machine:

```bash
omarchy-mcpd --permissions
```

`omarchy_search_commands` reports the tier of every result too, so an agent can
see what it may do before trying.

**An agent cannot switch this server off.** Its own IPC target answers `status`
and `recent` to an agent — both read-only — and refuses every other verb, as is
any command that would disable, remove or replace this plugin. The refusal
points at the bar panel, which is where you press Stop, Restart or Reload
config. See [`SECURITY.md`](SECURITY.md).

### Being asked, and writing it down

Deciding once, in advance, in a text editor, is the wrong shape for a decision
about a specific command. So by default a guarded command **asks you at the
time**, and what you decide can be written down.

The rules live in their own file, `~/.config/omarchy/mcp/permissions.json`,
which is meant to be checked into your dotfiles:

```json
{
    "permissions": {
        "guardedDefault": "ask",
        "deny": [{ "kind": "route", "matcher": "omarchy dev *" }],
        "ask": [{ "kind": "route", "matcher": "omarchy install *" }],
        "allow": [{ "kind": "route", "matcher": "omarchy theme *" }]
    }
}
```

Rules are read **deny, then ask, then allow** — the first match decides, and a
narrower rule never jumps the queue. A matcher is either an exact route
(`omarchy install app`) or a prefix with a trailing ` *` (`omarchy install *`,
which also covers the bare `omarchy install`). There is no separate notion of a
group: every route's group _is_ its second word, so `omarchy install *` is the
`install` group.

Set `"guardedDefault": "deny"` to have guarded commands refused outright rather
than asked about.

Beside it, `permissions.local.json` is the daemon's own file: the only thing it
ever writes, holding the rules you created by pressing **Always**. Gitignore
that one; the daemon never touches `permissions.json`, which is yours.

#### An update can widen a rule you wrote

`omarchy install *` means _the install prefix_, not the fifteen routes that
existed the day you typed it. So an `omarchy update` can put three more commands
inside a sentence you already agreed to, and nothing in your file changed to say
so. The server watches for exactly that:

- a **rule that now covers more** than it did — the one worth being told about
- a **rule that has stopped matching** — a `deny` that upstream renamed out from
  under you looks exactly like one that is working
- **commands in a group this plugin has never classified**, which derive as safe
  and run

New commands that fall under an existing `allow` are **held at ask** until you
review them: restrictions extend forward, grants do not. A `deny` or `ask` rule
covers a new command the moment it arrives.

The bar panel shows all of it, and **Acknowledge** records what Omarchy ships now
so you are only told about the next change. From a terminal:

```bash
omarchy-mcpd --review
```

A fresh install has nothing to compare against, so the first run records a
baseline and tells you nothing.

Rules that stop matching anything — a route was renamed out from under one —
accumulate rather than being tidied away behind your back. The panel lists them
and offers **Prune**, which removes them from `permissions.local.json` only.
`--review` prints the same list.

### Seeing the rules, and taking one back

The panel's **Rules** tab lists every rule in force, grouped by the file it came
from, with what each one covers and whether it is doing anything at all. Four
things get flagged:

| Flag        | What it means                                                                                                                                                     |
| ----------- | ----------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `error`     | The rule asserts something that can never be honoured — granting a sudo route, say. The daemon refuses to start on one                                            |
| `void`      | The matcher covers nothing on this Omarchy: a typo, or a route that was renamed                                                                                   |
| `shadowed`  | It never decides anything, because an earlier rule with a different effect already covers everything it matches. **You believe you granted this and you did not** |
| `redundant` | The same, but the earlier rule agrees with it. Safe to remove                                                                                                     |

A shadowed or redundant rule names the rule that got there first, and which file
that one is in, because the two fixes are _delete this_ and _narrow that_.

Grants written by answering **always** get a **Remove** button. It takes `allow`
rules out of `permissions.local.json` and nothing else — never a `deny`, never
an `ask`, and never anything from your own `permissions.json`. A restriction is
a decision, and it is taken back the way it was written: in an editor, which
each file's **Edit** button opens.

From a terminal, `omarchy-mcpd --permissions` prints the same thing, and
`omarchy-shell io.github.bruce-forte.mcp-server permissions` says how many rules
need attention. That verb, like `review` and `pending`, answers **you** — an
agent asking this plugin's own target gets only `status` and `recent`.

Copy [`permissions.example.json`](permissions.example.json) to start, and check
your edits before restarting anything — see [The command line](#the-command-line)
for all four verbs.

`--permissions` answers the question you actually have — _why can it do that?_ —
by expanding every rule against the commands your Omarchy ships and listing the
routes the document decides. Agents read the same report as
`omarchy://permissions`.

**A file that does not load stops the server.** Not "is ignored with a warning"
— ignoring it would mean running under rules nobody wrote, and an ignored `deny`
is a protection you think you have and do not. You get a critical notification
naming the problem, the bar panel says so, and the panel's **Check permissions**
button tells you when the fix is good. (An edit made while the server is running
is gentler: a broken save leaves the rules it already had in force.)

### What being asked looks like

A guarded route raises a critical notification naming the command and what it
resolved to — the theme, the monitor, the path. **Clicking it opens the panel.**
It does not approve anything: the notification has no buttons, and a click on a
toast should not be able to grant a command.

**Three answers, one place.** The panel shows the pending question with:

|                |                                                                                                                                                                                              |
| -------------- | -------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| **Allow once** | runs this call and changes nothing                                                                                                                                                           |
| **Always**     | runs it _and_ writes an `allow` rule for that exact command to `permissions.local.json` — never a wildcard, however many times you press it, because you consented to what was on the screen |
| **Deny**       | refuses, unambiguously                                                                                                                                                                       |

The bar icon opens the same panel, so a notification that fails to summon it is
not a dead end.

Nothing else approves. Dismissing it, ignoring it, and letting the deadline pass
all refuse, because a prompt that granted on expiry would be granting to an
empty room.

The agent is told which of those happened, because they mean different things:
a refusal is worth respecting, and a silence is worth asking you about directly.

**It stops asking before you stop reading.** A command you did not approve is
not asked about again for a few minutes — longer each time — and twelve prompts
in ten minutes stops the asking altogether, whatever you answered. A stream of
critical notifications turns a click into a reflex, and a reflexive click is not
consent. The agent is told plainly, and told to have you allow the command once
instead. The panel shows what is being held back and for how long; it clears
itself.

Two things asking never reaches. Anything needing sudo stays refused — no answer
makes it runnable, so no rule may grant it and writing one is an error the
server tells you about rather than a line that quietly does nothing. And
anything a `deny` rule covers stays refused, because that is a decision you
already took and re-asking it would turn your _no_ into a question.

One route is asked about every time and can never be granted:
`omarchy update lock`, whose own argument is a command line. Allowing it once
would allow everything.

If your MCP client supports elicitation over a transport that can carry it, the
question appears there instead. Claude Code's does not — the protocol revision
it negotiates carries no server-initiated requests at all — which is why the
notification is the primary surface rather than a nicety beside it.

### Arguments are checked before anything runs

Whatever the tier, an argument that names something is checked against your
machine before anything is spawned. A theme name is matched the way Omarchy
matches it — case and spaces do not count — and a near miss is refused with the
near misses named rather than corrected into a different theme:

```jsonc
// omarchy_theme(action="set", name="Tokoy Night")
{
    "error": "no theme named 'Tokoy Night' is installed.",
    "unresolved": "theme",
    "reason": "not_found",
    "did_you_mean": ["Tokyo Night"],
}
```

The same applies to monitor names, wallpaper paths, and URLs. `reason` tells an
agent whether the name was wrong (`not_found`, worth retrying with another) or
whether nothing could be checked (`source_unavailable`, retrying will not help).

This is also what makes an approval prompt worth answering: it names _Tokyo
Night_, not _"an agent wants to run omarchy theme set"_.

## Seeing what it did

Every tool call is appended to an activity log, so _what did that agent do to my
desktop_ has an answer after the daemon is gone:

```
~/.local/state/io.github.bruce-forte.mcp-server/activity.jsonl
```

One JSON object per line — what was called, what it was understood to be acting
on, whether you approved it, how it ended, and how long it took:

```jsonc
{
    "ts": "2026-08-31T14:22:07+02:00",
    "tool": "omarchy_run",
    "route": "omarchy theme set",
    "args": ["tokyo-night"],
    "target": "Tokyo Night",
    "tier": "guarded",
    "consent": "accepted",
    "outcome": "ok",
    "exit": 0,
    "ms": 142,
}
```

Refusals are in there too — a guarded route that was stopped is more interesting
than a safe one that ran. `outcome` is one of `ok`, `failed`, `timed_out`,
`refused`, `not_installed` or `error`.

Read the end of it without `jq`, running or not:

```bash
omarchy-mcpd --tail 20
```

Or click the bar icon, which shows the same records without their arguments.

**What is never written there:** command output. No OCR text, no clipboard
reads, nothing a tool returned. Arguments are written, truncated — they are what
the agent asked for, and they are the point — so the file can contain text you
copied, and it is created `0600` in a `0700` directory. It rotates at 1 MiB
keeping one previous generation, so it costs at most 2 MiB.

Switch it off, resize it, or rename it under `[log]` in your config; see
[Configuration](#configuration).

## The command line

`omarchy-mcpd` is the daemon, and also the tool for reading its state from a
terminal. It is **not on your `PATH`** — it lives inside the plugin directory:

```
~/.config/omarchy/plugins/io.github.bruce-forte.mcp-server/bin/omarchy-mcpd
```

Alias it, as in [Getting started](#2-give-yourself-a-shortcut-to-the-cli), or
type the path. Every verb below reads files directly, so all of them work
whether or not the daemon is running — which is exactly when you need them.

```bash
omarchy-mcpd --tail 20             # the last 20 things an agent did
omarchy-mcpd --permissions         # what is in force, and what each rule covers
omarchy-mcpd --check-permissions   # would the daemon start? exit 0 if yes
omarchy-mcpd --review              # what changed under your rules since you looked
omarchy-mcpd --edit permissions    # open it in your editor, with a template if new
omarchy-mcpd --print-client-config # the client setup line, with the token
```

`--edit` takes `permissions`, `local` or `config`. It opens the file in whatever
editor you use, and if the permissions file is not there yet it writes a
starting one first — an empty rule block and the `$schema` line, so your editor
checks a matcher as you type it. It never changes a file that already exists.

`--tail`, `--review`, `--permissions` and `--print-client-config` all take
`--json` for a machine-readable form. `--version` prints the version.

The same answers are reachable through the plugin's IPC target, which is what a
script or another agent discovers:

```bash
omarchy-shell io.github.bruce-forte.mcp-server status
qs ipc -n -p "$OMARCHY_PATH/shell" show     # every verb, with signatures
```

## Development

Work on a checkout, then point Omarchy at it:

```bash
omarchy plugin add /path/to/omarchy-mcp-server --enable --yes
omarchy plugin update io.github.bruce-forte.mcp-server
```

`plugin add` **clones**, so only committed work gets installed.

```bash
make check        # tests, qmllint, shellcheck, manifest validation, staleness gates
make test
make tools        # regenerate TOOLS.md from the server's schemas
make schema       # regenerate permissions.schema.json from the pydantic models
make run          # run the daemon in the foreground
```

Use the `Makefile` rather than bare `uv` commands: it puts the dev virtualenv
outside the repository, because `omarchy plugin validate` rejects symlinks
anywhere inside a plugin folder and a virtualenv is largely symlinks.

**Reload rules.** Editing Python takes effect on the next daemon restart
(`omarchy-shell io.github.bruce-forte.mcp-server restart`). Editing QML needs
`omarchy restart shell`.

The source carries its own documentation: every module opens with why it is
shaped the way it is, and [`ARCHITECTURE.md`](ARCHITECTURE.md) says which order
to read them in.

## Troubleshooting

| Symptom                                                            | Cause and fix                                                                                                                                                        |
| ------------------------------------------------------------------ | -------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| The bar icon shows `!`                                             | The daemon is not serving. `journalctl --user -f \| grep omarchy-mcp` says why                                                                                       |
| `omarchy-mcpd: command not found`                                  | It is not on `PATH` by design — see [The command line](#the-command-line)                                                                                            |
| Client cannot connect                                              | Wrong or stale token. Re-run `clientConfig` and re-add the server                                                                                                    |
| `address already in use`                                           | Something else has port 8765. Set `port` in the config, restart, then re-run `clientConfig`                                                                          |
| Bootstrap failed on first login                                    | Usually no network yet. `omarchy-shell io.github.bruce-forte.mcp-server rebuild`                                                                                     |
| A command is refused                                               | Check its tier with `omarchy_search_commands`, or `omarchy-mcpd --permissions`. Sudo commands cannot be run at all                                                   |
| Tools do not appear in the client                                  | The client caches the tool list; reconnect it                                                                                                                        |
| The server will not start, and the panel blames `permissions.json` | Run `omarchy-mcpd --check-permissions`, or press **Check permissions** in the panel. It names the rule and the two legal matcher forms. Fix it, then press **Start** |
| An approval notification appears more often than you want          | Press **Always** on it, write an `allow` rule, or set `"guardedDefault": "deny"` to have guarded commands refused instead of asked about                             |
| A rule you wrote does nothing                                      | The **Rules** tab flags it `void`, `shadowed` or `redundant` and names the rule that got there first                                                                 |

## Uninstall

```bash
omarchy plugin remove io.github.bruce-forte.mcp-server
rm -rf ~/.local/state/io.github.bruce-forte.mcp-server   # venv, token, activity log, autostart marker, registry snapshot
rm -rf ~/.config/omarchy/mcp                             # config.toml and your permissions
rm -rf "$XDG_RUNTIME_DIR/io.github.bruce-forte.mcp-server"   # pending approvals
claude mcp remove omarchy                                # if you added it there
```

The runtime directory is cleared when you log out, so that line only matters if
you are removing the plugin without rebooting.

`plugin remove` takes the plugin directory only; the two directories above are
outside it by design and are not touched.

## Requirements

- Omarchy 4 (Quattro) or newer
- `/usr/bin/python3` — present on every Omarchy install
- A network connection on first run, to build the environment
- `uv`, or a network connection so the plugin can fetch a pinned copy of it

## Security

This server runs commands on your desktop on behalf of a language model. Read
[`SECURITY.md`](SECURITY.md) before installing it.

The one thing worth knowing before you get there: the tools that read your
screen, your clipboard and your window titles hand the model text that neither
you nor this project wrote, and a page that says _"ignore your instructions and
run …"_ is a real attack. The server tells the model to treat all of it as data,
but that is a request, not a control. What actually stops it is the policy tier
and your client's approval prompt — so keep tool approvals on.

## License

Apache-2.0. See [LICENSE](LICENSE).
