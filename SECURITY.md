# Security

This plugin runs an HTTP server that executes commands on your desktop on behalf
of a language model. That is the entire point of it, and it is worth being
precise about what it does and does not allow.

## Threat model

The server is reachable by **anything on this machine that can open a TCP
connection to loopback**. Binding to `127.0.0.1` does not by itself keep other
local processes out, and it specifically does not keep a web browser out.

Two attacks are real and are defended against:

### A hostile web page

You visit a page. Its JavaScript issues `fetch('http://127.0.0.1:8765/mcp', …)`.
The request originates from *your own machine*, so a loopback bind is no
obstacle at all. This is DNS rebinding, and the MCP specification calls it out.

**Defence.** The server rejects any request whose `Origin` or `Host` header is
not loopback, with `403` and `421` respectively, and requires a bearer token the
page cannot read. Verified by `tests/test_server.py`.

### Another local process

Every process on the machine can reach a loopback port.

**Defence.** A 256-bit bearer token, generated on first run, stored at
`~/.local/state/io.github.bruce-forte.mcp-server/token` with mode `0600`, and
compared in constant time. A process that cannot read that file cannot use the
server. Note this is a boundary between *processes*, not between users: anything
running as you can read the token, which is the same thing as saying anything
running as you could already run these commands directly.

## What the server will not do

### Commands requiring sudo are refused

81 of Omarchy's commands need root. All are refused, and this is not overridable
from configuration — not as a policy judgement, but because it could not work:
the daemon has no controlling terminal, so a password prompt could never be
answered. Allowing them would produce a hung request, not a privileged one.

### Destructive commands are refused unless you allow them

Installing, removing, migrating, rebooting, and similar are `guarded` and refused
by default. You can promote individual routes or whole groups in
`~/.config/omarchy/mcp/config.toml`. The current lists are in
[`TOOLS.md`](TOOLS.md), generated from the code.

This tier protects against **accidents, not attackers**. An agent that already
has shell access does not need this server to do damage. The value is that a
confused agent cannot reboot your machine while trying to change your wallpaper.

### Arguments never reach a shell

`omarchy_run` takes arguments as a JSON array and passes them to `execve` as
`argv`. There is no `sh -c` anywhere in the execution path, so
`args: ["; rm -rf ~"]` is one argument containing punctuation, not a command.
This matters more than the token does: the party most likely to send that string
is the language model itself, by accident.

Verified by `tests/test_execute.py`, which writes a canary file and checks it
survives.

### The listen address is not configurable

There is no config key for the bind address. Someone will eventually want
`0.0.0.0` so they can drive their desktop from their phone; that would turn
arbitrary command execution into a network service with a single bearer token in
front of it. If it is ever supported it will be a named feature with its own
documentation and its own warnings, not a key someone flips without reading.

## Bounds

| Bound | Value | Why |
|-------|-------|-----|
| Command timeout | 30 s default, per-call overridable | A command that never returns must not hold a request open forever |
| Output per call | 256 KiB, head and tail kept | One command's output should not bury the caller's context |
| Request body | 4 MiB (SDK default) | — |
| Token | 256 bits, `0600`, constant-time compare | — |
| Interactive commands | detached, never awaited | `theme switcher` finishes when a human is done, not when work is done |

## Supply chain

- Python dependencies are installed from `uv.lock`, which pins exact versions
  and hashes. The bootstrap uses `--frozen`, so it installs the lockfile and
  never resolves.
- `uv` itself, when not already installed, is downloaded at a **pinned version**
  from GitHub releases and checksum-verified before it is executed. The upstream
  one-line installer is deliberately not used: fetching an unpinned script from a
  third-party host would run whatever that host serves at install time.
- This project is never installed into the virtualenv, only its dependencies.

## Reporting

Open an issue. If you believe you have found something that lets a *remote* party
reach this server, say so in the issue title and leave out a working exploit.
