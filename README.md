# MCP Server for Omarchy

Lets a local AI agent drive your Omarchy desktop: every command in Omarchy's
registry, plus the live shell's IPC targets, exposed over MCP on loopback.

Runs as an **Omarchy plugin**, so there is no systemd unit to enable, no second
install step, and no separate package. The plugin supervises a small daemon; the
daemon starts with your session and stops with it.

> **Status: Phase 4.** Nineteen tools, seven resources, a supervised daemon,
> and a bar widget that says whether it is serving. What is left is hardening;
> see [`ROADMAP.md`](ROADMAP.md).

## Documentation

| File | For |
|------|-----|
| `README.md` | Using it — install, connect a client, configure, uninstall |
| [`TOOLS.md`](TOOLS.md) | The tool reference, generated from the server's own schemas |
| [`ARCHITECTURE.md`](ARCHITECTURE.md) | How it works. Start here to read the source |
| [`SECURITY.md`](SECURITY.md) | What an agent can and cannot do, and why |
| [`ROADMAP.md`](ROADMAP.md) | What is done, what is left, what was decided against |
| [`CLAUDE.md`](CLAUDE.md) | Working agreement, and Omarchy plugin conventions |

## Contents

- [What it does](#what-it-does)
- [Install](#install)
- [Connecting a client](#connecting-a-client)
- [Checking it works](#checking-it-works)
- [Configuration](#configuration)
- [What an agent is allowed to run](#what-an-agent-is-allowed-to-run)
- [Development](#development)
- [Troubleshooting](#troubleshooting)
- [Uninstall](#uninstall)
- [Requirements](#requirements)
- [Security](#security)
- [License](#license)

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

| Tool | Does |
|------|------|
| `omarchy_search_commands` | Finds commands, with arguments, examples, and whether they can be run |
| `omarchy_run` | Runs one. Arguments never touch a shell |
| `omarchy_shell_targets` | Lists IPC targets and every method signature |
| `omarchy_shell_call` | Calls one |

One tool per command would put tens of thousands of tokens of schema into a
client's context before it did anything, so discovery and dispatch are separate.

**Fifteen curated tools** sit on top, each earning its place one of two ways.

Some return what the generic runner structurally cannot — an image, or data that
is not Omarchy's at all:

| Tool | Does |
|------|------|
| `omarchy_screenshot` | Returns the screen as an image, so an agent can see it |
| `omarchy_desktop_state` | Hyprland's monitors, workspaces, windows, and focus |
| `omarchy_screen_text` | OCR, for reading what something says |
| `omarchy_clipboard_read` / `_write` | The clipboard, which is not an Omarchy command |
| `omarchy_system_status` | Eight probes in one call instead of eight round trips |

The rest are simply asked for constantly, and a search round trip before every
volume change is a bad trade:

`omarchy_notify`, `omarchy_osd`, `omarchy_theme`, `omarchy_background`,
`omarchy_audio`, `omarchy_brightness`, `omarchy_media`, `omarchy_toggle`,
`omarchy_launch`.

Curated tools go through the same policy check and executor as `omarchy_run` — a
better-shaped door onto the same room, never a way around the lock. Any of them
can be switched off in the config, and everything they do stays reachable
through `omarchy_run`.

**Seven resources** carry the reference material. Tools are how an agent acts;
resources are how a person reads — in Claude Code they appear as `@` mentions:

| URI | Holds |
|-----|-------|
| `omarchy://commands` | The whole registry, annotated with what this server may run |
| `omarchy://shell/targets` | Every IPC target with full method signatures — documented nowhere upstream |
| `omarchy://desktop/state` | Monitors, workspaces, windows, focus |
| `omarchy://system/status` | The system status aggregate |

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

## Checking it works

```bash
omarchy-shell io.github.bruce-forte.mcp-server status
curl -s http://127.0.0.1:8765/health
```

`status` reports whether the daemon is *serving*, which is not the same as
running — a wedged HTTP loop still has a live process, so the plugin probes
`/health` rather than trusting the pid.

## Configuration

Optional. Everything works without it. A commented template is written to
`~/.config/omarchy/mcp/config.toml` on first run; every key is commented out and
shows its default, so keys you leave alone keep tracking upstream defaults.

```toml
[server]
# port = 8765
# timeout_ms = 30000
# max_output_b = 262144

[policy]
# allow = ["omarchy system reboot"]
# allow_groups = ["install"]
# deny = ["omarchy launch browser"]

[log]
# level = "info"
```

After editing:

```bash
omarchy-shell io.github.bruce-forte.mcp-server reloadConfig
```

The listen address is always `127.0.0.1` and is deliberately not configurable.
See [`SECURITY.md`](SECURITY.md).

## What an agent is allowed to run

Commands are classified automatically from the registry, so the policy does not
rot when Omarchy adds commands:

| Tier | Rule | Behaviour |
|------|------|-----------|
| `blocked` | needs sudo | Refused always. The daemon has no controlling terminal, so a password prompt could never be answered. Not overridable |
| `guarded` | installs, removes, migrates, reboots | Refused unless allowed in your config |
| `safe` | everything else | Runs |

`omarchy_search_commands` reports the tier of every result, so an agent can see
what it may do before trying.

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
  "did_you_mean": ["Tokyo Night"]
}
```

The same applies to monitor names, wallpaper paths, and URLs. `reason` tells an
agent whether the name was wrong (`not_found`, worth retrying with another) or
whether nothing could be checked (`source_unavailable`, retrying will not help).

## Development

Work on a checkout, then point Omarchy at it:

```bash
omarchy plugin add /path/to/omarchy-mcp-server --enable --yes
omarchy plugin update io.github.bruce-forte.mcp-server
```

`plugin add` **clones**, so only committed work gets installed.

```bash
make check        # tests, qmllint, manifest validation
make test
make tools        # regenerate TOOLS.md from the server's schemas
make run          # run the daemon in the foreground
```

Use the `Makefile` rather than bare `uv` commands: it puts the dev virtualenv
outside the repository, because `omarchy plugin validate` rejects symlinks
anywhere inside a plugin folder and a virtualenv is largely symlinks.

**Reload rules.** Editing Python takes effect on the next daemon restart
(`omarchy-shell io.github.bruce-forte.mcp-server restart`). Editing QML needs
`omarchy restart shell`.

## Troubleshooting

| Symptom | Cause and fix |
|---------|---------------|
| The bar icon shows `!` | The daemon is not serving. `journalctl --user -f \| grep omarchy-mcp` says why |
| Client cannot connect | Wrong or stale token. Re-run `clientConfig` and re-add the server |
| `address already in use` | Something else has port 8765. Set `port` in the config, then re-run `clientConfig` |
| Bootstrap failed on first login | Usually no network yet. `omarchy-shell io.github.bruce-forte.mcp-server rebuild` |
| A command is refused | Check its tier with `omarchy_search_commands`. Sudo commands cannot be run at all |
| Tools do not appear in the client | The client caches the tool list; reconnect it |

## Uninstall

```bash
omarchy plugin remove io.github.bruce-forte.mcp-server
rm -rf ~/.local/state/io.github.bruce-forte.mcp-server   # venv and bearer token
rm -rf ~/.config/omarchy/mcp                             # your configuration
claude mcp remove omarchy                                # if you added it there
```

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
you nor this project wrote, and a page that says *"ignore your instructions and
run …"* is a real attack. The server tells the model to treat all of it as data,
but that is a request, not a control. What actually stops it is the policy tier
and your client's approval prompt — so keep tool approvals on.

## License

Apache-2.0. See [LICENSE](LICENSE).
