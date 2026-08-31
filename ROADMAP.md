# Roadmap

What is decided, what is built, what was rejected. Check here before proposing
a feature — several things below were considered and deliberately dropped.

## Decisions

Settled during design. Each row names the thing that forced it, because the
reasons matter more than the choices when something needs revisiting.

| # | Decision | Forced by |
|---|----------|-----------|
| 1 | **HTTP transport on loopback**, not stdio | Clients are local agents and other plugins, which speak standard MCP HTTP. A stdio server could not be a daemon, and the daemon is the point |
| 2 | **18 tools**: 4 generic + 14 curated | 356 commands as 356 tools is ~30k tokens of client context before the agent does anything. Discovery + dispatch scales; per-command tools do not |
| 3 | **Bearer token + Origin validation**, loopback bind | `omarchy_run` is arbitrary command execution. Loopback alone does not stop browser DNS rebinding — a hostile page's `fetch` originates from your own machine |
| 4 | **Three policy tiers derived from registry metadata** | `omarchy commands --json` already carries `requires_sudo` and `group`. Derived policy does not rot on Omarchy upgrades; a hand-written allowlist does |
| 5 | **Timeout + detach, defaulted per route** | Many commands block on the user by design (`theme switcher`, `menu select`, `capture region`). A blocked HTTP request means a client timeout and a leaked child |
| 6 | **Python + venv + official `mcp` SDK** | Spec compliance tracked upstream. `/usr/bin/python3` is guaranteed on Omarchy; node and luajit are not |
| 7 | **Self-healing bash wrapper** builds the venv, then `exec`s | `omarchy plugin add` runs no build and no install hook, by design. Bootstrap has to be lazy, and QML is the wrong place for it |
| 8 | **`kinds: ["service", "bar-widget"]`** | A daemon whose only client is an agent fails silently and invisibly. The bar icon is the cheapest compliance with "never fail silently" |
| 9 | **7 IPC functions**, health probed not assumed | `qs ipc show` is the discovery mechanism, so the names are documentation. A wedged HTTP loop still shows a live pid, so liveness needs a real probe |
| 10 | **4 concrete resources + 3 URI templates** | Covers all 356 commands and ~20 IPC targets. 356 concrete resources would bloat `resources/list` and blow up in any client that injects the list into context |
| 11 | **Optional TOML config, every key commented out** | Live keys freeze v1 defaults forever. Commented keys let upstream defaults flow through |
| 12 | **Daemon health notifies; request failures do not** | The agent already receives tool errors in the response. Toasting them would fire constantly on a wrong `omarchy_run` |
| 13 | **Never install our own package into the venv** | An editable install writes build artifacts into the plugin directory, which Omarchy watches — every bootstrap would reload the shell |
| 14 | **uv only, no pip fallback** | Two bootstrap paths means the rare one is the least tested and, without `uv.lock`, the least safe |
| 15 | **Visibility before curated tools** | Building 14 tools on a daemon you can only observe through `journalctl` means debugging blind |
| 16 | **Keep 4 concrete resources + 3 templates** even though Claude Code never enumerates templates (F8) | Resources are for a human typing `@`; agents discover through tools. 61 concrete group resources would bury `omarchy://shell/targets`, the entry actually wanted |
| 17 | **Dev virtualenv lives outside the repository** | `omarchy plugin validate` rejects symlinks anywhere in a plugin folder, and a virtualenv is largely symlinks. The `Makefile` enforces it |

## Phases

- [x] **0 — Spike.** Done. Findings below. Minimal SDK server, one tool, bearer auth. Answers: does
      `claude mcp add --transport http` connect; do resource *templates* appear
      in `@` autocomplete or only concrete resources; does the SDK's streamable
      HTTP require a session id the client must round-trip.
- [x] **1 — Walking skeleton.** Done. Manifest, `Service.qml`, `bin/omarchy-mcpd`,
      uv bootstrap, config seeding, `registry.py`, `policy.py`, `execute.py`,
      the 4 generic tools, auth. Installs end to end. **An agent can reach all
      356 commands and every IPC target at the end of this phase** — everything
      after is ergonomics.
- [ ] **2 — Visibility.** Bar widget, health probe, state file, 7 IPC
      functions, throttled notifications.
- [ ] **3 — Curated tools.** Tier 1 (5) first: `screenshot` and `desktop_state`
      unlock what `run` structurally cannot do. Then Tier 2 (9).
- [ ] **4 — Resources.** The 7 from decision 10, shaped by what Phase 0 found.
- [ ] **5 — Hardening.** Generated `TOOLS.md`, CI, `SECURITY.md`.

Tests are not a phase. `policy.py` and the auth checks are tested in the phase
that creates them — they are the security boundary, and tests retrofitted to a
security boundary only assert whatever the code already does.

## Rejected

| Thing | Why not |
|-------|---------|
| One tool per omarchy command | Decision 2 |
| Hand-curated allowlist of commands | Defeats the self-maintaining registry; rots every Omarchy release |
| `ask_user` via `omarchy menu select` | Blocks the HTTP request until the user answers. MCP elicitation is the right mechanism if this is ever wanted |
| `power` tool (lock/logout/reboot/shutdown) | Irreversible from an agent's hands. Still reachable through `omarchy_run` if deliberately allowed in config |
| `plugin_manage` tool | An agent editing the shell it runs inside |
| `reminder`, `screenrecord` tools | `reminder` is fine through `omarchy_run`. `screenrecord` is long-running and stateful for rare use |
| Configurable listen `host` | Turns arbitrary command execution into a LAN service behind one bearer token. If ever wanted, it is a named feature with its own documentation, not a config key |
| Promoting sudo commands to runnable | No controlling tty: `sudo` hangs on a password prompt nobody can see |
| Async job model for long runners | Decision 5 covers it with less surface. Revisit if progress streaming is actually wanted |
| MCP prompts | Anything worth shipping is better as a Claude Code skill, iterable without a daemon restart |
| stdio transport / shim | Decision 1. Revisit only if a target client cannot do HTTP |

## Phase 0 findings

Verified against `mcp` 2.1.1, Claude Code, and a live Omarchy 4.0.1 desktop.

| # | Finding | Consequence |
|---|---------|-------------|
| F1 | The SDK is on **2.x**, where `FastMCP` was renamed `MCPServer` and the module moved to `mcp.server.mcpserver`. Nearly every example in the wild is 1.x | Pin `mcp>=2,<3` in `pyproject.toml`. Treat 1.x docs as wrong |
| F2 | `claude mcp add --transport http` connects and lists tools. A full session calls the tool and returns its result | Decision 1 holds. The whole transport choice is validated |
| F3 | Claude Code's handshake is `server/discover` + `subscriptions/listen` + `tools/list` — **not** a classic `initialize`. The SDK answers it transparently | Nothing to do, but do not hand-roll the protocol on this evidence |
| F4 | The server returns `mcp-session-id` and the client round-trips it. Stateful mode works | No need for `stateless_http` |
| F5 | `TransportSecuritySettings` has `enable_dns_rebinding_protection = True` by default and returns **403** for a hostile `Origin` | Decision 3's Origin requirement is **native to the SDK**, not custom code. Still set `allowed_hosts`/`allowed_origins` explicitly |
| F6 | Bearer auth as `BaseHTTPMiddleware` reading the body **breaks the transport**: the SDK's `watch_disconnect` calls `receive()` expecting `http.disconnect`, gets `http.request`, and every `tools/call` 500s | **Auth must be pure ASGI middleware touching headers only.** Verified both ways: the failure and the fix |
| F7 | `ToolAnnotations(readOnlyHint=..., destructiveHint=..., openWorldHint=...)` is supported and surfaces in `tools/list` | Decision 4's annotation layer works as designed |
| F8 | **Claude Code never calls `resources/templates/list`.** Confirmed twice, at RPC and handler level, in a session that *did* call `resources/list` unprompted | Templates are invisible in Claude Code's `@` menu. They still resolve when read by explicit URI, and other clients may enumerate them. See decision 10 |
| F9 | `uv venv --python /usr/bin/python3` builds against the system interpreter with no managed CPython download | Decision 14's bootstrap is sound |
| F10 | `MCPServer` also ships `custom_route` (used for `/health`), `token_verifier`, and `resource_security` with path-traversal rejection on by default | `/health` for decision 9's liveness probe is a one-liner |
