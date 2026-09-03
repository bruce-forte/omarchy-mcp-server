# Roadmap

What is decided, what is built, what was rejected. Check here before proposing
a feature — several things below were considered and deliberately dropped.

## Decisions

Settled during design. Each row names the thing that forced it, because the
reasons matter more than the choices when something needs revisiting.

| # | Decision | Forced by |
|---|----------|-----------|
| 1 | **HTTP transport on loopback**, not stdio | Clients are local agents and other plugins, which speak standard MCP HTTP. A stdio server could not be a daemon, and the daemon is the point |
| 2 | **19 tools**: 4 generic + 15 curated | 356 commands as 356 tools is ~30k tokens of client context before the agent does anything. Discovery + dispatch scales; per-command tools do not |
| 3 | **Bearer token + Origin validation**, loopback bind | `omarchy_run` is arbitrary command execution. Loopback alone does not stop browser DNS rebinding — a hostile page's `fetch` originates from your own machine |
| 4 | **Three policy tiers derived from registry metadata** | `omarchy commands --json` already carries `requires_sudo` and `group`. Derived policy does not rot on Omarchy upgrades; a hand-written allowlist does |
| 5 | **Timeout + detach, defaulted per route** | Many commands block on the user by design (`theme switcher`, `menu select`, `capture region`). A blocked HTTP request means a client timeout and a leaked child |
| 6 | **Python + venv + official `mcp` SDK** | Spec compliance tracked upstream. `/usr/bin/python3` is guaranteed on Omarchy; node and luajit are not |
| 7 | **Self-healing bash wrapper** builds the venv, then `exec`s | `omarchy plugin add` runs no build and no install hook, by design. Bootstrap has to be lazy, and QML is the wrong place for it |
| 8 | **`kinds: ["service", "bar-widget"]`** | A daemon whose only client is an agent fails silently and invisibly. The bar icon is the cheapest compliance with "never fail silently" |
| 9 | **7 IPC functions**, health probed not assumed | `qs ipc show` is the discovery mechanism, so the names are documentation. A wedged HTTP loop still shows a live pid, so liveness needs a real probe |
| 10 | **Concrete resources + URI templates** (4 + 3 at the time; `omarchy://permissions` made it 5 + 3 in N10 c) | Covers all 356 commands and ~20 IPC targets. 356 concrete resources would bloat `resources/list` and blow up in any client that injects the list into context |
| 11 | **Optional TOML config, every key commented out** | Live keys freeze v1 defaults forever. Commented keys let upstream defaults flow through |
| 12 | **Daemon health notifies; request failures do not** | The agent already receives tool errors in the response. Toasting them would fire constantly on a wrong `omarchy_run` |
| 13 | **Never install our own package into the venv** | An editable install writes build artifacts into the plugin directory, which Omarchy watches — every bootstrap would reload the shell |
| 14 | **uv only, no pip fallback** | Two bootstrap paths means the rare one is the least tested and, without `uv.lock`, the least safe |
| 15 | **Visibility before curated tools** | Building 15 tools on a daemon you can only observe through `journalctl` means debugging blind |
| 16 | **Keep the concrete resources + 3 templates** even though Claude Code never enumerates templates (F8) | Resources are for a human typing `@`; agents discover through tools. 61 concrete group resources would bury `omarchy://shell/targets`, the entry actually wanted |
| 17 | **Dev virtualenv lives outside the repository** | `omarchy plugin validate` rejects symlinks anywhere in a plugin folder, and a virtualenv is largely symlinks. The `Makefile` enforces it |
| 18 | **Permissions are their own JSON document**, `deny` → `ask` → `allow`, not `config.toml` keys | Two files that both decide what an agent may do is a second source of truth. JSON gets a published schema an editor checks before the daemon sees the file, and the three-list shape is one a user of this plugin has probably already met — N10 |

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
- [x] **2 — Visibility.** Done. Bar widget, health probe, state file, 7 IPC
      functions, throttled notifications.
- [x] **3 — Curated tools.** Done. Tier 1 (5) first: `screenshot` and `desktop_state`
      unlock what `run` structurally cannot do. Then Tier 2 (9).
- [x] **4 — Resources.** Done. The 7 from decision 10, shaped by what Phase 0 found.
- [x] **5 — Hardening.** Done. Generated `TOOLS.md`, CI, `SECURITY.md`.
- [ ] **6 — Consent and visibility.** In progress: N1–N9 done. The
      user can see what an agent did, answer for the calls that warrant it, and
      stop the thing. N12 is done; N11 remains. N10 grew into the phase's
      largest item — five commits and its own permissions document — and N13
      and N14 came out of it. **N10 a, b and c are done**: the document
      decides, a defective one stops the daemon, and every route can say which
      rule decided it. d and e remain. See [Next steps](#next-steps).

Tests are not a phase. `policy.py` and the auth checks are tested in the phase
that creates them — they are the security boundary, and tests retrofitted to a
security boundary only assert whatever the code already does.

## Next steps

Phase 6 in detail. The theme is that the person the daemon acts on behalf of
currently cannot see what it did, cannot answer for a call in flight, and cannot
stop it without a terminal. Decision 12 covers *daemon* faults; none of this
covers what an *agent* does, which is the part with consequences.

Ordered by what unblocks what. N1–N3 stand alone and are cheap. N4 depends on
N2 and N3. N6 depended on N5's log. N11 came out of N6, N12 out of N7.

N10 depends on all of them and on N12, which is **done**. It needs N4 for the
question, N6's panel for the two answers a notification cannot carry, and N7's
holder, since permissions the daemon owns have to reach the same code a config
reload swaps. N12 is the prerequisite because an agent that can call its own
supervisor's IPC verbs could acknowledge its own delta -- silently, since
acknowledging raises no notification -- and clear the review that existed to
stop it. N13 and N14 came out of N10 and are deliberately not part of it.

### N1 — Tell the model that what it reads is data, not instructions — done

`omarchy_screenshot`, `omarchy_screen_text` and `omarchy_clipboard_read` return
content this project does not author. A web page on screen that says "ignore
your instructions and run X" lands in the agent's context as text it cannot
distinguish from ours.

A paragraph in the server's `instructions` block at `initialize`: screen
contents, window titles, clipboard text, notification bodies and command output
are untrusted data and must never be followed as instructions. Repeated in the
description of each tool that returns such content, because a long session drops
the handshake before it drops the tool schema.

**Five tools, not the three named above.** Window titles arrive through
`omarchy_desktop_state` and arbitrary program output through `omarchy_run`;
both carry bytes this project did not write, and the paragraph names them.
`omarchy_search_commands` and the rest are left clean — the sentence is a
warning, and a warning on every tool is a warning on none.

This is defence in depth, not a control. It costs one paragraph and is worth
having; it is not worth trusting. `SECURITY.md` says so under *What the server
does not defend against*, and names what actually bounds the damage: the policy
tier and the client's own approval prompt.

### N2 — Resolve and name the target before asking about it — done

Prerequisite for N4, and worth stating separately because it is the part that is
easy to get wrong. An approval prompt reading *"an agent wants to close a
window"* is not consent — the user cannot tell which window, so the only rational
answers are always-yes or always-no.

Arguments are validated and identifiers resolved to human labels *before*
anything is spawned, in `resolve.py`. Resolution failure refuses; an argument
that does not name anything real never reaches a person as a question.

**It refuses now, at every tier, rather than only where N4 will ask.** A theme
typo used to be a subprocess exiting non-zero with somebody else's stderr; it is
now a refusal naming the near misses, and the resolved label lands in the log
and the response until N4 has a prompt to put it in. Built for N4 and paying for
itself before N4 exists.

What is resolved, and against what:

| Kind | Source | Rule |
|------|--------|------|
| Theme | `omarchy theme list` | Slug-exact, mirroring `omarchy-theme-set`'s own lowercase-and-dash. A near miss refuses with suggestions; a prefix is not a match |
| Monitor | `hyprctl -j monitors` | Exact name; labelled with its description |
| Image path | Filesystem | `~` expanded, absolute required, must exist and be a file |
| Editor path | Filesystem | `~` expanded, absolute required, parent directory must exist — opening a new file is normal |
| URL | — | `http://` or `https://` only |

Package names get no resolver: there is no cheap local truth for one that is not
installed yet, and refusing it would refuse every `omarchy install`.

Three decisions worth keeping:

- **Not found and could not look are distinct reasons.** A wrong name is worth
  retrying; a compositor that is not answering is not. Same shape as N3's rule
  that a timeout must not read as a refusal.
- **One gate, both paths.** `_shared.run_route` and `omarchy_run` call the same
  `resolve_call`, so reaching a command by its route cannot skip a check that
  reaching it by a curated tool applies. `omarchy_screenshot` and
  `omarchy_screen_text` bypass that gate structurally, so they call the monitor
  resolver themselves.
- **No config key.** Resolution is validation, not policy: it refuses exactly
  the calls that would have failed anyway. A knob whose only effect is worse
  error messages is not worth documenting forever.

Not built: a window resolver. The roadmap's own *"close **Firefox — GitHub**"*
example has no caller — no tool takes a window identifier, and the perception
tools act on the focused one. It lands when something needs it.

### N3 — No answer means denied — done

Prerequisite for N4. Any consent mechanism needs a timeout and the timeout has to
fail closed. The daemon starts with the session and outlives whoever walked away
from the desk, so a prompt that grants on expiry grants to an empty room.

`consent.py` is the rule without the mechanism: N4 owns the elicitation call,
this owns everything around it. `ask()` takes any awaitable as the asker, so the
rule is tested against a fake before a client is involved.

Default 60s, `policy.ask_timeout_s`, bounded 5–600. The key is parsed but left
out of `config.example.toml` and the README until N4 makes it do something — a
documented key that changes nothing reads as a bug.

**Six outcomes, not four.** The two N4 already named as edge cases are outcomes
in their own right, because each implies a different next move:

| Outcome | Means |
|---------|-------|
| `accepted` | Proceeds, carrying whatever the user typed |
| `declined` | The user refused this specific call |
| `cancelled` | The user dismissed the prompt without deciding |
| `timed_out` | Nobody answered; assume nobody is at the desk |
| `unsupported` | The client cannot ask anyone — refuse, naming `config.toml` |
| `unreachable` | The client disconnected mid-question |

Three rules N4 must not undo:

- **The deadline is the decision.** At the deadline the awaitable is cancelled
  and its result is never read. A click landing a second late has nowhere to go:
  the agent has already been told the call was refused and may have acted since.
- **A client that cannot ask never counts as one that said yes.**
  `supports_asking` checks `elicitation.form` specifically — url mode answers in
  a browser tab, and this project exists because the person is at a desktop.
  **Amended by F22.** Claude Code declares `elicitation: {}` and names neither
  sub-mode, so that check refuses the client this project is for. A bare
  `elicitation` object counts as form mode. The rule the sentence protects is
  intact: no client is ever assumed to have said yes.
- **Only the word `accept` grants.** Matched on the reply's `action`, not on its
  class, so a fourth action added upstream fails closed instead of falling
  through.

Still N4's: the `omarchy notification send -u critical` and its dismissal on
every exit path, including the timeout and disconnect paths above. It turned out
not to be *alongside* the prompt but to **be** the prompt for HTTP clients —
F23 closed the back-channel elicitation needed, and F25 found that a
notification's `--exec` can carry the answer back.

### N4 — Ask at call time — done

The substantive one. Today `guarded` means *refused unless pre-allowed in
`config.toml`*, which forces a per-call risk decision to be made once, in
advance, in a text editor, followed by a daemon restart. That is the wrong shape
for the decision.

Add a third behaviour to the existing tier, rather than a per-tool permission
map:

```toml
[policy]
# ask = true          # guarded routes ask at call time instead of refusing
# ask_timeout_s = 60
```

`blocked` stays unaskable. No sudo command becomes reachable by answering a
question — decision 4, unchanged. **`policy.deny` stays unaskable too**: a route
the user demoted by hand is a decision already taken, and re-asking it would
turn their *no* into a question. `decide` therefore returns a `Verdict` that
says whether a refusal is askable, rather than leaving the caller to infer it
from `tier` and `allowed`.

#### Two askers, because elicitation does not reach this client

MCP elicitation was the plan, and it is still the right mechanism where it
works. It does not work here: Claude Code negotiates protocol `2026-07-28`,
whose transport has **no back-channel at all**, so `ctx.elicit` fails inside
this process before anything reaches the wire. F22–F24 are the whole story.

So the question goes to the desktop instead, through a mechanism Omarchy already
has. `omarchy notification send --exec` runs a command when the notification is
clicked (F25):

```
omarchy notification send -u critical \
  "Approval needed: omarchy install (#7)" \
  "Click to approve. Ignoring this refuses it." \
  --exec <plugin>/bin/omarchy-mcp-consent approve <token>
```

The helper writes the token into
`$XDG_RUNTIME_DIR/io.github.bruce-forte.mcp-server/consent/`, and the parked
call is watching for it. There is one action, so **a click is yes and silence is
no** — which is the rule N3 already fails closed on.

This is better than it looks. The roadmap's own objection to elicitation was
that *"the prompt appears where the client is, and this project exists because
the user is looking at a desktop rather than a terminal."* The desktop asker has
no such weakness, works in every protocol era, and works for clients that
declare no elicitation capability at all.

`consent.ask` already takes any awaitable as the asker — that seam was built in
N3 precisely so the mechanism could change without the rule changing. The gate
picks one:

```python
asker = (
    (lambda: ctx.elicit(message, Approval))
    if ctx.session.can_send_request
    else (lambda: desktop_ask(message, token))
)
answer = await consent.ask(asker, what=label, timeout_s=config.ask_timeout_s)
```

#### The gate

One place decides, and both paths reach it. `_shared.run_route` and
`omarchy_run` currently duplicate the policy check and the resolver call; a
third duplicated branch — the one that decides whether a command runs — is the
one that must not drift. `gate.py` joins `policy.py`, `auth.py` and
`execute.py` as a boundary file:

```python
async def authorize(cmd, args, *, config, ctx, log) -> Allowed | Refused
```

**Resolution happens before the question, and only on the ask path.** N2 exists
so the prompt can name *Tokyo Night* rather than `omarchy theme set`; a prompt
answered *yes* and then refused as unresolvable has spent the user's attention
for nothing. A refusal that will not ask still never pays for a resolver
subprocess:

| Verdict | Order |
|---------|-------|
| allowed | resolve, run |
| refused, askable, `ask = true` | resolve (refuse silently if unresolvable), ask, run on accept |
| refused otherwise | refuse; never resolves |

#### The refactor this implies

`elicit` is a coroutine on `Context`, and waiting for a click is a coroutine
too, so **every tool becomes `async def`** — they are sync functions with no
`Context` parameter today.

The trap: the SDK runs a **sync** tool through `anyio.to_thread.run_sync`
(`func_metadata.py:164`), and an `async` tool loses that for free thread. This
codebase blocks in four modules, and OCR is a 30-second subprocess. So each
tool keeps its current body as a private `_impl` and the registered tool is one
hop:

```python
async def omarchy_screenshot(monitor="", ...) -> str:
    return await to_thread.run_sync(partial(_screenshot, monitor, ...))
```

Gate-path tools split: await the gate on the loop, thread the execution.
`execute.run` stays sync and blocking — do not casually make it async; its
timeout, process-group kill, and detach behaviour are tested and subtle
(decision 5, F14).

This is mechanical but touches every tool module. It is the bulk of the work and
should be its own commit, landed and green *before* any consent logic goes in.

#### A click has to be unforgeable

The agent is the untrusted party here, and `omarchy_run` can pass arbitrary
arguments to any of hundreds of safe commands. If the existence of a file were
consent, an agent that talked *any* of them into writing a path would approve
its own guarded call, and a sequential id could be pre-created for every future
ask.

So the token is `secrets.token_urlsafe(16)`, it is both the filename and the
file's contents, the gate accepts only when both match, and it unlinks either
way. **The token never reaches the model** — not in a result, not in a refusal.
Directory `0700`, files `0600`. Named in the README's uninstall section.

The prompt text needs the same suspicion. `omarchy install` has no resolver by
design, so its arguments are model-supplied strings that may have been read off
a hostile page by `omarchy_screen_text`, and they are rendered to a human who is
about to click. The message is assembled from a fixed frame; every argument is
stripped of control characters and newlines, truncated, and quoted, so nothing
an argument contains can add a line or forge the frame.

#### Outcomes

N3's six stand unchanged. The desktop asker can only produce two of them,
because one action cannot express a *no*, and the wording says so rather than
telling the agent nobody was there:

> The approval notification for (`omarchy install` — ripgrep) was not clicked
> within 60s. That may mean the user refused it or was not there. Nothing ran.
> Ask the user directly rather than repeating it.

The notification is dismissed on **every** exit path, in a `finally` under
`anyio.CancelScope(shield=True)`: a client disconnect cancels the task, and a
`-u critical` notification has no expiry, so an unshielded dismissal leaves a
prompt on screen forever. A click does not dismiss it either (F26).

**One pending ask per client session.** An agent issues tool calls in parallel,
and five guarded calls in a batch would mean five dialogs and five permanent
notifications. A second concurrent ask from the same session refuses
immediately, without eliciting or notifying. Two *clients* still ask at once,
which is the property decision 1 rests on.

#### Discovery has to admit the feature exists

`omarchy_search_commands` and the commands resource both publish
`runnable = verdict.allowed`. With `ask = true` a guarded route would still
report `runnable: false` plus a refusal telling the model to edit
`config.toml` — so a well-behaved agent never calls it and the prompt never
fires. Both sites report `runnable: true` and a separate `asks: true`, from one
derivation, so they cannot drift. `TOOLS.md` regenerates.

`ask` defaults to **false**: installing this plugin must not make a guarded
command reachable that was not reachable before. The rot that N10 describes —
nobody edits the arrays, so `guarded` means *never* — is answered instead by the
guarded refusal naming both ways out, `policy.allow` and `policy.ask`, where an
agent will read it and can tell the user.

**Superseded by N10.** The default flips to *ask*, and both keys leave
`config.toml`. The reasoning above was written when asking meant elicitation,
which does not reach this client at all (F23) — the switch mostly did nothing.
Once a question means a `critical` notification naming the route, its arguments
and its target, "reachable" means *reachable after a human reads exactly what it
does*. And a permissions document that fills itself through use never fills if
nothing is ever asked.

#### Verified on a live desktop

Not only against fakes. With `ask = true`, `omarchy channel current` — guarded,
harmless, and so a real test rather than a brave one — was called from Claude
Code three times:

| Call | What happened |
|------|---------------|
| clicked | `consent asked (#1)` → 37s → `consent accepted` → ran, exit 0 |
| ignored | `consent asked (#2)` → 60s → `consent timed_out` → refused, and the agent was told the silence was ambiguous |
| `omarchy apply hardware` (sudo) | refused with no `consent asked` line at all |

The runtime directory was `0700` and empty afterwards on both ask paths: a token
is spent when it is read and cleared when it is not. Turning `ask` back off
restored `runnable: false` and a refusal naming both ways out.

#### Tests

`policy.py` is the security boundary and this changes what it permits, so tests
land in the same commit:

- Every `blocked` route still refuses **without asking**. Assert the asker is
  never called for sudo — this is the one that must never regress.
- `policy.deny` refuses without asking.
- `guarded` with `ask = false` refuses exactly as today.
- `guarded` with `ask = true` asks, and accept / decline / cancel / timeout each
  produce their own reason string.
- A forged consent file — right name, wrong contents — does not approve. A
  replayed one does not approve twice.
- A client with no back-channel gets the desktop asker, not a hang.
- The notification is dismissed on every exit path, including the cancelled one.
- An argument containing newlines cannot add a line to the prompt.

### N5 — An activity log on disk — done

`stats.py` kept five counters in memory and `/health` published them. That was
enough for "is it serving" and nothing else: it could not answer *what did the
agent just do to my desktop*, and it died with the daemon.

One JSON object per tool call now lands in
`${XDG_STATE_HOME:-~/.local/state}/io.github.bruce-forte.mcp-server/activity.jsonl`:

```jsonc
{"ts":"2026-08-31T14:22:07+02:00","tool":"omarchy_run","route":"omarchy theme set",
 "args":["tokyo-night"],"target":"Tokyo Night","tier":"guarded","consent":"accepted",
 "outcome":"ok","exit":0,"ms":142}
```

`outcome` is one of `ok`, `failed`, `timed_out`, `refused`, `not_installed`,
`error`. Refusals are included, and they are the interesting half: a guarded
route that was stopped says more than a safe one that ran.

#### One seam, which fixed two bugs on the way

The fields the log wants — exit code, duration, resolved target, how the user
answered — were spread across 22 `stats.record` call sites that each knew a
different subset. Rather than a second recorder beside the counters, `Stats`
grew a context manager:

```python
with stats.call("omarchy_run") as rec:
    rec.route = route
    rec.tier, rec.consent = decision.verdict.tier.value, decision.consent
    rec.exit = result.exit_code
```

It times the call and records it exactly once on the way out, including on an
exception. Two long-standing bugs stopped being representable: `omarchy_run`
recorded *before* the gate ran, so every refusal was counted as a success, and
the desktop tools recorded once per branch.

One thing the gate could not hand it. `Refused` already carried the consent
outcome, but `Allowed` was `(call, verdict)` — so an *approved* call looked
identical to a pre-allowed one, and it is not derivable: a guarded route in
`policy.allow` runs without anyone being asked. `Allowed` now carries `consent`
too, which is a boundary change and shipped with its tests.

#### Nothing waits for the disk

`record()` puts the event on a bounded queue and returns; one writer thread
drains it. That thread is the file's only writer, so **there is no lock on the
file at all** — the queue is the serialisation. It starts in `__main__` around
`uvicorn.run` and stops with a sentinel and a bounded join, which has to fit
between uvicorn's own 3s grace and `Service.qml`'s SIGKILL 5s after SIGTERM
(F27).

It stops from the **ASGI lifespan shutdown**, not from the end of `main`, and
that distinction is F28: uvicorn re-raises the signal that stopped it, so the
process dies by signal and nothing after `uvicorn.run()` gets a turn. The first
live run wrote no `stopped` marker at all and the unit tests were happy, because
they fake `uvicorn.run` as a function that returns.

The sentinel is the one `put` in the module that may block. Dropping it left the
writer parked on `get` with nothing coming and everything behind it unwritten —
found by a test, not by reasoning.

Loss is bounded and never silent: a full queue drops the record, counts it,
warns once on stderr, and the writer emits `{"event":"dropped","n":N}` into the
file as soon as it catches up. An unexplained gap in an audit trail is worse
than no audit trail. A write that fails outright logs every time, raises exactly
one notification — a daemon-level fault, decision 12 — and keeps serving.

#### What is not in it

**Command output.** Not OCR text, not clipboard reads, nothing a tool returned.
Arguments are written, truncated to 120 characters each with the elision named,
because they are what the agent asked for. That distinction is the whole
boundary between an audit trail and a log of the user's screen. The file is
`0600` in a `0700` directory anyway, since an argument can be text they copied.

#### Rotation

At 1 MiB the file is renamed to `activity.jsonl.1` and a fresh one started; one
generation is kept, so 2 MiB at worst. The rename breaks an `inotify` watch on
the path, which was written here as N6's problem to handle — watch the directory
and reopen.

N6 did not have to. It reads the log by spawning `--tail --json` when a panel
opens, so `activity.tail` handles the rotation in Python where it already did,
and nothing in QML watches the file at all. Which is just as well: `FileView`
has no seek, so a watch would have pulled the whole megabyte into the shell
process on every tool call.

Rotation happens *before* the write that would cross the cap rather than after,
so the file is never over it and a record is never split across generations.

#### One thing a `stopped` marker does not cover

F28 made the daemon write `stopped` from the ASGI lifespan shutdown, so a
SIGTERM produces one. `omarchy restart shell` does not: the shell is killed and
the daemon dies with it, before uvicorn hands control to the lifespan. The log
then carries `started` with no `stopped` before it.

Visible in a real log, and this is what it looks like — the pairs are IPC
restarts, the bare ones are shell restarts:

```
15:09:48  -- stopped
15:09:48  -- started version=0.1.0 port=8765
15:10:33  -- started version=0.1.0 port=8765     <- omarchy restart shell
15:50:19  -- started version=0.1.0 port=8765     <- omarchy restart shell
```

Not a fault in the writer, and not fixable from inside the daemon: it is not
given a chance to run. It is recorded here because N6 puts these lines on a bar
panel, where three consecutive `daemon started` rows read as a bug until you
know what they mean. A supervisor-side fix — the service SIGTERMing its child on
its own destruction — belongs to whoever wants the log to close every session it
opens.

#### Reading it back

`activity.tail(n)` reads across a rotation, and `omarchy-mcpd --tail N` renders
it for a person. Deliberately **not** on `/health`: that route is tokenless by
design, and these lines carry command arguments. An unauthenticated endpoint is
not where the clipboard goes.

#### Configuration

```toml
[log]
# activity = true                # off: nothing is written at all
# activity_max_bytes = 1048576   # 64 KiB - 64 MiB
# activity_file = "activity.jsonl"
```

`activity_file` is a **name, not a path**, always joined onto the state
directory. A configurable path could be pointed at the plugin directory, and
Omarchy reloads the shell on any write inside one — which would mean a shell
reload per tool call, reading as a broken plugin rather than a bad setting.

Named in the README's uninstall section; `omarchy plugin remove` takes the
plugin directory only.

#### Verified on a live desktop

Installed on a real Omarchy, restarted, and driven from an attached Claude Code
session — which is how F28 was found, since every unit test was green while the
shutdown path did not work at all.

| What | Result |
|------|--------|
| File on a real start | `activity.jsonl` created `0600` in a `0700` directory, with a `started` line |
| A curated tool from a real client | `omarchy_theme` → `"tier":"safe","outcome":"ok","exit":0,"ms":249` |
| A guarded route, `ask` off | `"tier":"guarded","outcome":"refused"`, nothing spawned |
| A guarded route, `ask` on, notification clicked | `"consent":"accepted","outcome":"ok","ms":15121` — the 15s is a person reading it |
| Restart with a client attached | `stopped` then `started`, back serving in about 2s, well inside `Service.qml`'s 5s SIGKILL |
| `--tail 8` | Rendered the real file, including the approval and the refusal |
| The plugin directory afterwards | No `__pycache__`, `git status` clean — F19's rule holds with the new module |

The consent directory was empty afterwards, and the journal carried
`consent asked` → `consent accepted` beside the log's own line.

### N6 — Show it in the widget, and let the widget stop the daemon — done

The bar widget now roots in `Ui/Panel` instead of `Ui/BarWidget`. Clicking the
icon opens a card: the last eight records from N5's log, and three buttons —
Stop/Start, Restart, and Copy client config. The icon highlights when an agent
makes a call.

#### The premise was wrong, and that is the finding

This item was written around an obstacle: *Omarchy routes inter-plugin calls to
panels and overlays, not to services, so the widget cannot call the service.*
Three workarounds were listed, in order of preference, and a fourth was
suggested to be confirmed first — a shared QML singleton — with a warning
attached: **do not assume it, the phase 2 findings are all cases where the shell
did not behave as read.**

That warning earned its keep, in the opposite direction. The obstacle is real
for `shell call <id> <method>`, which routes through the panel loader map that
only panel, overlay and menu plugins are in. It is not real for the widget
reaching the service *object*:

```qml
readonly property var service: bar && bar.shell && bar.shell.serviceFor
  ? bar.shell.serviceFor(root.moduleName) : null
```

`shell.serviceFor(pluginId)` has no first-party restriction, services and bar
widgets load into one QML engine, and `omarchy.media` already does exactly this
between its own two halves. So the widget holds the live `Service.qml` root and
calls `stop()` on it. All three workarounds were unnecessary.

The suggested fourth way was wrong too, and the shell says so in its own source,
twice: a plugin-local `pragma Singleton` gives each importer its own copy, which
is *why* the shell injects instances instead.

#### What the spike found, which no amount of reading would have

The design rested entirely on `serviceFor`, so it was proved first, on a live
desktop, with a widget that did nothing but report what it got. Three restarts,
ten minutes:

```
serviceChanged -> null
serviceChanged -> linked phase=starting serving=false
after 4s      -> linked phase=listening serving=true calls=0
```

**The widget is constructed before the service exists.** Every startup, the
first evaluation is null. It resolves only because that line is a *binding*,
which re-runs when the shell's service map changes — the `Component.onCompleted`
form of the same expression latches null forever and the widget would show
nothing, on a machine where the code is plainly correct.

So the state file stays, and the fallback is not defensive dressing: there is a
real window on every single startup where it is the only thing the widget has.

#### The list and the icon are fed by different things, on purpose

The panel's list comes from `omarchy-mcpd --tail 8 --json`, spawned by the
service when a panel opens. The icon's pulse and counters come from a new
per-call frame on the stdout channel the service already parses.

Neither is a substitute for the other, and merging them would have been the
mistake. The frames stop when the daemon does; the log is what survives it, and
a panel opened after a crash is the case that matters most. Feeding the list
from both would mean deduplicating two accounts of one call against records that
carry no id.

- **The tail is spawned by the service, not the widget.** A bar surface exists
  per monitor, so the widget is instantiated once per screen. Three monitors
  would have meant three readers of one file.
- **`FileView` has no seek.** Tailing the log continuously from QML means
  pulling up to a megabyte into the shell process on every tool call, plus
  reopening on rotation. That is what the 620ms subprocess buys out.
- **`--tail --json` answers with an envelope**, `{"activity": bool, "records":
  [...]}`, because an empty array cannot say whether nothing has happened or the
  log is switched off. The panel has to tell a user which one they are looking
  at; "no calls" shown to someone whose counter reads 42 is a lie.

#### What the panel will not show

Arguments. The record has them and the panel does not read them. `outcome`,
`route`, the resolved target from N2, and the duration are all bounded by
construction; an argument is the one field a model supplies, and for
`omarchy_clipboard_write` it *is* the clipboard. N5 put that behind a `0600`
file in a `0700` directory and deliberately kept it off `/health`; a popup on a
desktop is in every screenshot and every screen share, which is further out than
either.

The call frame carries the same restriction, and a test asserts a copied secret
does not reach it.

**Log events are rendered, not filtered.** The first live run showed empty rows
with gaps: `tail` returns the log's own `started`/`stopped`/`dropped` lines
alongside calls, and those have no `tool`. Filtering them out was the tempting
fix and the wrong one — `dropped` is precisely the record N5 emits so that a gap
in the audit trail is never silent, and a panel that hid it would undo that.

#### A Stop that turns itself back on is not a Stop

`Component.onCompleted: start()` ran unconditionally, so any stop died at the
next `omarchy restart shell`. Stopping now writes a marker in the state
directory and starting clears it; autostart waits for the `FileView` verdict
rather than firing immediately.

The marker is written rather than deleted — `FileView` can write a file and
cannot remove one, so its *contents* say which state it means. Empty is "start
me".

This gave `stop()` a side effect that outlives the session, which forced a split
that was worth making anyway: `start()` and `stop()` carry intent and persist
it, `beginRunning()` and `halt()` do the process work, and `restart()` uses the
private pair. A restart is not a stop — going through the public one would leave
the daemon switched off for good if the shell died between the halves, and
nothing on the desktop would say why. `test_shutdown.py` pins it.

#### The panel needs no IPC target of its own

`Ui/Panel` offers a free `IpcHandler` from `ipcTarget`, which would have
collided with the one `Service.qml` already owns — a target routes to exactly
one handler. It is left empty. The shell routes `summon`/`hide`/`toggle` for a
`bar-widget` plugin to the live widget instead, recognising it by the
`open`/`close`/`opened` contract that `Panel` provides, precisely because a
fixed target would only ever reach one monitor's copy:

```bash
omarchy-shell shell toggle io.github.bruce-forte.mcp-server
```

Two IPC functions were added to the service's existing target rather than a new
one: `recent` and `copyClientConfig`.

#### The token goes to the clipboard, never to the screen

`clientConfig` printed the setup line to the journal, because returning it would
put the bearer token in a terminal. The panel's button pipes
`--print-client-config` into `wl-copy` through two processes and a pipe — not
`wl-copy <text>`, because argv is world-readable through `/proc`, and not a
shell either. `panels/network/Panel.qml` sends a wifi password the same way, for
the same reason.

#### Verified on a live desktop

| What | Result |
|------|--------|
| `serviceFor` on a third-party plugin | Returns the live service object; null until the service host catches up |
| Panel via `shell toggle` | Opens, with no IPC target of its own |
| The list | Real records, with outcome and duration, no arguments |
| Log events | `· daemon started`, `· daemon stopped` — the empty-row bug, found here and fixed |
| A call frame | `calls` and `lastTool` update immediately, not on the next 10s poll |
| Stop from the panel's code path | Daemon gone, port closed, `autostart-off` written |
| `omarchy restart shell` while stopped | Still stopped, and the journal says who stopped it and how to undo it |
| The list while stopped | Still there — it reads the file, not the daemon |
| Start, then restart the shell | Marker cleared, serving again |
| Copy client config | 144 characters on the clipboard, nothing on screen |
| `make check` | 340 tests, `qmllint`, `shellcheck`, `omarchy plugin validate` |

### N7 — Live tool enable/disable — done

`enabled(config, …)` was evaluated when tools were registered, so a curated tool
switched off in `config.toml` was never registered and never appeared in
`tools/list`. That half was already right, and it is the half that matters: a
tool the model cannot see is one prompt injection cannot talk it into trying.

What was missing is that the decision was frozen until a restart. The config is
now re-read while the daemon serves, and `notifications/tools/list_changed` goes
out, so a session started before the change does not keep offering a tool that
now refuses, or hiding one just granted.

The scope grew by one key group during the grilling and was worth it:
**`[policy]` reloads too**. The plumbing is identical — both need the config to
stop being captured by value — and it is the other half of the same edit.
Somebody switching a tool off is usually in the same file changing `ask`.

#### Nothing may capture the config by value

The blocker was not the tool registry. It was that `Config` is frozen and was
passed *by value* into `build()`, into every `register()`, and into every tool
closure, so nothing could ever see a new one.

Three ways to fix that, and the choice matters more than it looks:

- Unfreeze `Config` and mutate it. Zero call-site changes, and a call reading
  `ask` while a reload writes `deny` sees a torn config.
- A `__getattr__` facade forwarding to the current snapshot. Also zero changes,
  and invisible — until someone stashes `config` in a dataclass and it silently
  stops tracking.
- A holder, read once per call. Chosen.

**The holder stops at the edge.** `settings.current` is read once at the top of
a tool body and the resulting frozen `Config` is passed down, so `policy.py`,
`gate.py`, `consent.py` and `execute.py` still take an immutable value and their
tests did not change — the security boundary never learns that configuration
moves. And one call is decided by one config: a reload landing between the gate
and the executor cannot authorize under one set of rules and run under another.

#### Declaring a tool is not offering it

`if enabled(config, "x"): @mcp.tool(...)` made a tool's existence a fact about
which decorators ran. `tools/catalogue.py` splits it: `register()` records the
function and its arguments, `apply()` adds and removes tools on the live server
to match a config, and it is the same call at startup and at reload.

The alternative — register everything, filter at `tools/list` — was rejected
because a filtered-out tool is still *callable* by name, so the invisibility
property would then depend on two places agreeing. A disabled tool stays absent;
a call naming it gets the SDK's unknown-tool error, and that is the correct
answer rather than a shortfall.

`TOOLS.md` came out byte-identical, which is the check that the refactor moved
no schema.

#### A file that does not parse must change nothing

`config.load` answers an unreadable file with defaults plus a list of problems.
At startup that is right and deliberate: a daemon that refuses to start over a
typo looks exactly like one that was never installed.

On a **reload** it is the opposite of right. Defaults mean an empty
`policy.deny` and an empty `tools.disabled` — so one stray keystroke in a file
saved mid-edit would drop the deny list and switch every disabled tool back on,
and then announce it to every attached client. `Config` gained a `parsed` flag
so the two cases are distinguishable, and a reload keeps the running config when
the file does not parse. A key that merely fails *validation* still applies the
rest, exactly as at startup: an unparseable file is no answer, a bad key is an
answer with a footnote.

A file that has simply gone missing waits one further poll. Editors write a
temporary file and rename it over the target, so absent-once is that gap, not a
deletion. Absent twice is deliberate, and resets to defaults.

This happened on the very first live edit, unplanned: appending a `[tools]`
table to a file that already had one is invalid TOML, the daemon refused it,
and the panel and the journal both said so while it kept serving.

#### The notification had to be built twice

The roadmap line said "emit `notifications/tools/list_changed`" as though it
were one call. It is two mechanisms, because the transport changed between
protocol eras:

- **2026-07-28+** has no standing server-to-client stream. Clients opt in with
  `subscriptions/listen`, and the SDK fans events out over a `SubscriptionBus`
  passed to `MCPServer(subscriptions=...)`. Public, five lines.
- **2025-06-18 and earlier** carry it on the standalone `GET` SSE stream, one
  per connection, and the SDK offers **no way to enumerate connections**. So the
  server keeps its own register, filled by a `middleware=` hook that sees every
  inbound message including `initialize`.

**The register holds `Connection`, not `ServerSession`.** The first attempt held
sessions weakly and announced to nobody: a `ServerSession` is built fresh for
every inbound message and is garbage as soon as its handler returns. The
`Connection` behind it lives as long as the client is attached and owns the
channel a notification travels on.

And the capability had to be turned on, which was the finding that would
otherwise have shipped silently. Measured against the running daemon before any
of this was written:

```json
"tools":{"listChanged":false}
```

A client is entitled to ignore a notification the handshake said would never
come. The flag is derived from a `NotificationOptions` the HTTP path builds with
everything off, and `streamable_http_app()` threads no way to change it — so the
one method that builds it is wrapped. That plus the connection register are the
two private attributes this phase leans on, and both are covered by tests over a
real handshake so an SDK release breaks the suite rather than someone's client.

#### `tools/list_changed` is a claim about the tool list

It is sent only when the tool set actually moves. A policy edit changes what a
route may do, not what `tools/list` says, and a client that re-listed on one
would be told exactly what it already knew.

#### A widening that nobody can see is the thing to avoid

Live policy reload means the rules an agent runs under can change ~2 seconds
after a file write, with nothing on screen. Two things bound that.

First, it is not new capability. `omarchy_shell_call` accepts any target
`qs ipc show` lists, this plugin's own included, so an agent could already call
`restart` and make a rewritten config take effect. N7 changed the latency. That
gap is real, is now written down in `SECURITY.md`, and is **N12** — it wants its
own decision about which verbs stay readable, not a patch smuggled in here.

Second, an effective policy change notifies, **in both directions**. A widening
because it matters; a narrowing because it explains a refusal that is about to
happen and would otherwise look like a bug.

#### The panel answers "did my edit take?"

Nothing else on the desktop could. The panel shows *18 of 19 tools offered*, and
says when `config.toml` stopped parsing — which matters precisely because the
daemon keeps serving the old configuration rather than falling back, so that
state is otherwise invisible. Counts travel on a `reloaded` frame so the bar
learns at the moment of the edit rather than up to ten seconds later, and
`/health` carries the same numbers so a missed frame heals on the next poll.

`reloadConfig` stopped being a restart. It sends `SIGHUP`, which the daemon turns
into an immediate re-read; the panel's **Reload config** button does the same.
Its real value is the rejected-file case: after fixing the file you want the
answer now, rather than a two-second wait you cannot tell apart from "still
broken".

The activity log gained `reloaded` and `config_rejected` events. A reader asking
why a route was allowed at 14:05 needs to know the rules moved at 14:04.

#### One test could not be written against `TestClient`

The notification test hung and reported nothing: Starlette's `TestClient` will
not flush a streaming response to a second thread while the first one waits, so
the frame only surfaced *after* the test had already given up. That test runs
against a real uvicorn on a real port. The rest still use `TestClient`.

#### Verified on a live desktop

| What | Result |
|------|--------|
| `/health` after the shell restart | `tools: 19, tools_declared: 19` |
| `omarchy-shell <id> status` | Carries `tools`, `toolsDeclared`, `configOk` |
| An invalid `config.toml` (a second `[tools]` table) | Refused; `configOk: false`; daemon kept serving; journal named the line and column |
| Fixing it | Applied within 2s, `configOk: true`, 19 → 18 tools |
| A probe client attached across the edit | `listChanged: true`, `notifications/tools/list_changed` arrived on its GET stream, `tools/list` went 18 → 19 |
| `reloadConfig` while serving | *"re-reading ~/.config/omarchy/mcp/config.toml now"*, `SIGHUP` in the journal |
| `reloadConfig` while stopped | *"the daemon is not running; start it first"* |
| A policy edit | `config reloaded: policy +deny: omarchy launch browser`, one notification |
| The activity log | `config_rejected`, then `reloaded added=[...] removed=[...] policy=...` |
| Restoring the user's config | Byte-identical, back to 19 of 19 |

### N8 — Resolve binaries on a fixed path — done

`Command.argv_prefix` splits a route into `["omarchy", …]` and `execute.run`
hands the bare name to `execve`, which resolves it against the `PATH` inherited
from `omarchy-shell`.

Resolve `argv[0]` against a fixed list — `/usr/share/omarchy/bin`,
`/usr/local/bin`, `/usr/bin` — and fail with "not installed" rather than
searching.

**Document this as robustness, not as a security control**, because it is not
one: no MCP client can influence this daemon's environment, so the attack it
would defend against does not exist here. What it buys is that the daemon runs
the binary it means to regardless of what the session's `PATH` has accumulated,
and that a missing dependency reports itself clearly instead of surfacing as a
confusing exit code. Do not add it to the `SECURITY.md` threat model.

Four things the item did not say, found while doing it:

- **It is not one spawn site but six**, in four modules. `execute.run` runs
  `omarchy`; `desktop.py` runs `hyprctl`, `grim`, `tesseract`, `wl-copy`,
  `wl-paste` and `magick`; `shell.py` runs `qs`; `registry.py` runs `omarchy`
  again. Resolving only the first would have left the third-party tools — the
  ones with a shim directory in front of them — exactly as they were.
- **The list is derived from `OMARCHY_PATH`**, not hardcoded as written above.
  The Makefile already trusts that variable, and a dev-linked Omarchy has to get
  its own binaries rather than the system's.
- **`Popen(argv, executable=...)`** rather than rewriting `argv[0]`, so the
  command reported to the agent stays the pasteable `omarchy theme set` that
  the README and `TOOLS.md` show. Which file ran goes in the log.
- **The environment is left alone.** Reaching into a child's `PATH` would be a
  behaviour change to hundreds of scripts, and would break `omarchy launch
  editor` for anyone whose editor is not in `/usr/bin` — which, on a machine
  with a session `PATH` worth fixing, is most of them. Omarchy's dispatcher
  resolves its own helpers relative to its own location anyway, so choosing the
  right `omarchy` settles every subcommand.

The concrete case, measured on the machine this was written on: the daemon's
inherited `PATH` was `/usr/share/omarchy/bin`, then **fifty-five** toolchain-manager
shims, and only then `/usr/bin`.

### N9 — A one-line USP section in the README — done

The README explains the parts well and never says, in one place, what this is
that a fixed set of hand-written desktop tools is not. Four things, none of them
currently stated together:

- **Complete coverage that maintains itself.** Every command in the registry is
  reachable, including ones added by an Omarchy release published after this
  one. There is no catalogue to keep in sync — decision 4.
- **The shell's IPC surface at all.** The bar, OSD, media, notifications, and
  every loaded plugin. `qs ipc show` is the only documentation these interfaces
  have, and `omarchy://shell/targets` republishes it.
- **Resources, so a person can read what an agent can do.** `@`-mentionable in
  Claude Code; tools are for acting, resources are for reading — decision 10.
- **One daemon, many clients.** Claude and Codex attached at once share one
  policy, one config, and (after N5) one audit trail, because there is one
  process holding all of it.

Write it against the design, not against any other project. It goes stale the
moment it is a comparison.

**Five, not four.** N4 shipped after this was written, and a question that
reaches the desktop and refuses on silence is a property of the design rather
than a detail of the configuration — the most unusual thing here, and the one a
reader is least likely to have met before. Its being off by default is a
sentence, not a disqualification.

Argument resolution was considered as a sixth and left out: it is true and
distinctive, but it already has a worked example under *What an agent is allowed
to run*, and six items read as a list rather than a claim.

### N10 — Permissions, reviewed by diff — a, b and c done

Depends on N4 for the question, on N6's panel for the surface a notification
cannot provide, and on N7's holder, since permissions the daemon owns have to
reach the same code a config reload swaps. N12 had to land first — see
*Why N12 came first* below.

Today `policy.allow` and `policy.allow_groups` are TOML arrays nobody edits, so
in practice `guarded` means *never*. The fix is not a bigger array. It is to
replace the arrays with a permissions document the daemon reads and appends to,
and to make the thing a person reviews **a delta, never a catalogue**.

#### The shape: three lists, `deny` → `ask` → `allow`

Modelled on Claude Code's permission rules, which solve the same problem for the
same kind of caller and which a user of this plugin has probably already met:

```json
{
  "$schema": "https://…/permissions.schema.json",
  "permissions": {
    "guardedDefault": "ask",
    "deny":  [{"kind": "route", "matcher": "omarchy dev *"}],
    "ask":   [{"kind": "route", "matcher": "omarchy install *"}],
    "allow": [{"kind": "route", "matcher": "omarchy theme *"}]
  }
}
```

Rules are objects rather than the `Tool(specifier)` strings the original uses:
`kind` validates as a schema enum, and a second kind — `tool`, when
`tools.disabled` eventually moves here — is an enum addition rather than a
parser change.

**JSON, not TOML, and not in `config.toml` at all.** `[policy]` empties
completely: `allow`, `allow_groups`, `deny`, `ask` and `ask_timeout_s` all move.
Two files that both decide what an agent may do is the second source of truth
the old plan warned about, arriving by a different door. JSON also gets a
published schema, so an editor validates the file before the daemon ever sees
it. `config.toml` keeps what it is good at — ports, timeouts, log settings.

**JSON, not SQLite**, for the reason the original gave and which still holds: a
few dozen decisions, no queries, no migrations, and a file a person can read and
hand-edit is worth more than one they cannot.

#### The matcher, and the disappearance of groups

`group` is exactly the second token of the route, for all 426 commands in the
snapshot. So `allow_groups = ["install"]` and the matcher `omarchy install *`
are the same set, and **the group concept leaves the configuration surface
entirely.** Groups survive only inside `policy.py`, where they derive tiers.
That is decision 4 doing its job: derivation stays, hand-maintained lists go.

Two matcher forms, and nothing else:

| Form | Example | Matches |
|------|---------|---------|
| exact route | `omarchy install app` | that route |
| prefix + ` *` | `omarchy install *` | every route under the prefix, **including the bare route** |

The trailing-`*`-matches-the-bare-route rule is adopted deliberately, not
inherited. 19 routes are two tokens long, so `omarchy migrate` is simultaneously
a route and a group; without that rule `omarchy migrate *` silently misses the
bare route — the most dangerous member — for 19 of 71 groups.

No mid-position wildcards, no `Bash(ls*)`-style space sensitivity, no `:*`
alias. Those exist upstream because a shell command line is an unbounded string.
This route space is closed and enumerable, which buys something better instead:
**a matcher can be expanded against the registry and shown as the concrete
routes it covers** — in the panel, in the resource, and in the delta.

#### The ladder

Six levels, one function, one table of tests. The file is not the top authority,
which is the one place this departs from the model it copies:

```
1  blocked (requires_sudo)   no rule promotes it — decision 4
2  never-storable            askable, never allowable — see below
3  deny rule
4  ask rule
5  allow rule
6  guardedDefault            "ask" by default; "deny" restores the old floor
```

First match wins, and **specificity never reorders it** — a narrow `allow`
does not beat a broad `ask`, exactly as upstream. That is what keeps a broad
`deny` from being defeatable by a narrower grant, and the property is worth more
than the convenience it costs.

Level 2 is new. `omarchy update lock` takes `<held|run> [command] [args...]`:
its contract is *run whatever you are handed*. One "always" on it is a permanent
grant of arbitrary execution, displayed in the review as a single calm row. The
criterion for the set is narrow on purpose — **the route's own argument is a
command line** — because the wider reading ("could lead to running
attacker-chosen code") swallows `install`, `pkg aur add` and `dev link`, and
then the store grants nothing anyone wanted to grant. `Verdict` gains
`storable`, the panel offers no "always" for such a route, and a hand-written
`allow` for one is a startup error rather than a silent void.

#### `ask` becomes a list, and the default flips

`policy.ask` was a global boolean, off by default, so that installing the plugin
could not make a command reachable that was not reachable before. That reasoning
was written when asking meant MCP elicitation, which does not reach this client
at all (F23) — the switch mostly did nothing. N4 changed the facts: a question
now means a `critical` notification naming the route, its flattened arguments
and its resolved target, answered by a click at the desk. "Reachable" now means
*reachable after a human reads exactly what it does* — the same gate as typing
the command.

It also has to flip, or N10 does not work. A store that fills itself through use
never fills if nothing is ever asked, and it would sit empty exactly the way
`policy.allow` sits empty today, behind the one config edit nobody makes.

`guardedDefault: "deny"` restores the old behaviour, discoverable in the schema
rather than buried in a comment.

#### Restrictions extend forward. Grants do not.

The single most useful sentence here, and the resolution of a contradiction the
first draft of this item did not notice: it said *"new entries are denied until
reviewed"*, while a wildcard is forward-looking by nature. Both are right.

| A newly-appeared route matches… | On arrival |
|---|---|
| a `deny` rule | denied |
| an `ask` rule | asks |
| **only an `allow` rule** | **held at `ask` until acknowledged** |
| nothing | `guardedDefault` |

So `omarchy install *` keeps meaning *the install prefix* rather than freezing
into the fifteen routes that existed the day it was typed — and the three routes
an `omarchy update` adds under it still cost one click each, once, before they
run. It is the precedence ladder extended across time: restrictions propagate,
grants do not.

#### The delta

After a startup or a reload, compare the registry against the routes last seen.
Three things can have changed, and the first draft described only the least
dangerous:

| Event | Example |
|---|---|
| new route, no rule matches | Omarchy adds `omarchy backup wipe` |
| **a rule silently widened** | `omarchy install *` matched 15, now matches 18 |
| **a rule went dead** | `deny: omarchy dev *` matches nothing — upstream renamed it |

The second is the one to be afraid of. The third is a silent loss of protection:
a `deny` that stopped matching looks exactly like a `deny` that is working.

Also worth surfacing, and invisible today: **a new group this plugin has never
classified is `safe` by derivation**, because `GUARDED_GROUPS` is a hand-written
frozenset and a group upstream invented tomorrow is not in it. Such routes still
run — a default-deny-on-upgrade is the fastest route to an uninstall — but they
are reported at `critical` as *commands in a group this plugin has never
classified*, which is the honest statement of what decision 4 does and does not
cover.

**Persist the route list, not a fingerprint.** A hash says something changed and
cannot say what; every one of the three events above is derivable from the
previous route set plus the current rules. ~426 strings.

**The snapshot advances only on acknowledgement, never on startup.** Otherwise
the notification fires once, the next boot overwrites the snapshot, and the
evidence is gone before anybody clicked. Un-acknowledged deltas accumulate.

#### A broken permissions file stops the daemon

`config.py`'s doctrine — *bad configuration is reported and then ignored* — is
right for `config.toml`, where ignoring a setting falls back to a safe default.
It inverts here: ignoring a `deny` is a loss of protection, and "no rules" is
not the safe floor, because a hand-written `deny` demotes routes the derivation
calls safe.

- **At startup**, any defect at all and the daemon does not start. It emits a
  `failed` frame carrying the parse error, sends a `critical` notification
  naming the file and the fix, and exits `78` (`EX_CONFIG`). `Service.qml` must
  recognise that code and **stop respawning** — the existing backoff would
  otherwise retry forever and, at the third failure, advise a `rebuild` that
  deletes a perfectly good venv while the real cause sits in a JSON file.
- **At reload**, keep the last-known-good and go loud. Not an inconsistency:
  at startup there is nothing to keep, at reload there is, and it is the state
  the user last successfully authored. The alternative drops every attached MCP
  session — the thing N7 exists to avoid — on an editor's mid-keystroke
  autosave, which no debounce can distinguish from a finished wrong file.
- **A missing file, and an empty `permissions` object, are both valid** and
  start normally. Otherwise every fresh install fails on arrival.

`permissionsOk` joins `configOk` in the bar and on the `reloaded` frame. Two
files, two health states: one lamp for both means "something is wrong" with no
way to tell which.

Because a stray comma now costs a startup, two things stop being niceties:
`--check-permissions` with a panel button beside it, so nobody is one autosave
away from a stopped daemon with no way to test the fix; and a parse error that
names the offending matcher, its index, and both legal forms verbatim.

#### Broken, versus valid but void

A rule can be perfectly well-formed and still have no effect. Three cases, and
they do not get the same answer:

| Rule | Verdict |
|---|---|
| matcher matches nothing (typo, or a route not shipped yet) | loads, reads **void** |
| matched last month, upstream renamed the route | loads, reads **void** — this is the delta signal |
| `allow`/`ask` naming a `requires_sudo` or never-storable route | **startup error** |

The first two are indistinguishable by construction — a matcher matching nothing
today may match tomorrow — so blocking them would turn an upstream rename into a
daemon that will not start, punishing the user for someone else's commit. The
third is different in kind: it is the user asserting a permission the system
will never honour, and believing you granted something you did not is worse
than being told loudly. A `deny` naming a sudo route is **not** an error — it
is the user agreeing with the derivation, and a redundant restriction is never
wrong.

#### What the daemon may write, and what it may never write

One writer: the daemon. The panel asks by calling the service; the answer
travels the channel N4 already built.

- **`permissions.json` is never written by the daemon.** It is the user's, it is
  tracked, and a daemon that rewrites a git-tracked file lands in somebody's
  diff at the wrong moment.
- **Only exact-route matchers, ever.** A click consents to what was on the
  screen. `omarchy install app` clicked nine times never becomes
  `omarchy install *`; if collapsing a set of grants into a wildcard is worth
  offering, it is a suggestion the panel makes and the user accepts into their
  own file, not an inference the daemon draws.
- **Invariants checked at write time, not just at button-render time**: not
  shadowed by a `deny`/`ask` in the pool, route present in the registry, not
  sudo, not never-storable. If one fails between the question and the answer —
  the user's own file grew a matching `deny` while the prompt was up — the call
  is **refused**. The pool is the authority at the moment of execution, the deny
  is newer than the question, and running something the user just forbade
  because a click was in flight is indefensible.
- **Never prune.** A rule whose route vanished stays, and reads dead in the
  explainer. Pruning destroys exactly the evidence the delta exists to show.
  A user-initiated prune is N13.

#### The answer channel

N4's notification carries one action (F25), so it can express *yes* but not
*yes, always* and not *no*. N6's panel has room for all three and reaches the
service directly. Neither should invent a second channel to the same process:

```
daemon mints token → asking frame (stdout) → service holds it in memory
                   → notification --exec  → helper writes  <token>
                   → panel buttons        → helper writes  <token> always | deny
                   → daemon polls, accepts only when name and contents match
```

`bin/omarchy-mcp-consent` gains `always` and `deny` beside `approve` and stays
**the only writer**. Its own comment is the reason — *"this is the whole answer
channel, so it stays small and it validates what it is given"* — and that stops
being true the moment QML writes the file too. Business rules do not move into
the UI layer. An unrecognised verb reaching an older daemon must read as *no
answer*, never as an accept.

Two consequences: the panel gets a real **Deny**, which removes the ambiguity
`consent.py` currently has to word around (*"may mean the user refused it or
that they were not there"*); and the `asking` frame carries the route and its
arguments, which is a deliberate exception to `frames.py`'s rule that frames
carry no arguments. Consent that does not show what it consents to is not
consent — N2 — and `prompt.message` already puts those same flattened arguments
on the desktop, so the frame exposes nothing the notification did not.

**The token never enters the state file and is never rendered.** It is a live
capability for the length of one question.

#### The surfaces

The delta is small — usually zero, occasionally a handful after an update — and
it needs a click. The full listing is 56 guarded routes, or all 426 with tiers,
rules and sources, and it is reference material. They do not belong on the same
surface: a toggle that expands a bar popup to 400 rows is a scroll trap.

| Surface | Carries |
|---|---|
| bar panel, `PERMISSIONS` section | the delta, with Allow / Deny per row and **Acknowledge all** |
| `omarchy://permissions` resource | the full listing, `@`-mentionable — decision 10, tools act, resources are read |
| `omarchy-mcpd --permissions` | the same, for a terminal |

The row that matters most is the widening one — *your existing
`omarchy install *` already covers this and you have never seen it* — and it
should read as a warning rather than a listing.

**The rule explainer is one derivation with four readers.** `resources.py`
already annotates every command with `tier`, `runnable` and `asks`; it gains
`rule` (the matcher that decided) and `source` (which file it came from), and
the panel, the resource, `omarchy_search_commands` and `--permissions` all read
that. It answers the question a person actually opens the panel to ask — *why
can the agent do this?* — and it is what stops the panel offering a control
whose effect is nil: no "always" when a `deny` or `ask` rule matches, no
"revoke" on a row that came from `permissions.json`. Instead it names the rule
and its file, and says to edit it.

#### Why N12 came first

N12 — an agent calling its own supervisor's IPC verbs — is a different item once
N10 exists. `stop` costing the audit trail is bad; these are worse:

- an **acknowledge** verb would let an agent clear its own delta and advance the
  snapshot. Acknowledging is silent by design — it raises no notification — so
  the hold-new-routes-at-`ask` rule would evaporate with nothing on screen.
- any **grant** verb would let an agent permit itself.
- **`restart`**, against a daemon that refuses to start on a bad permissions
  file, is a denial of service on the whole permission system.

N12 is done. What outlives it is a standing rule: **N10 ships no mutating IPC
verbs.** Acknowledge, allow and revoke exist only in the panel and in the helper
— surfaces a person reaches at the desk. Read-only verbs are fine. Defence in
depth, and it costs nothing, because every mutating action here already has a
person-facing surface by design.

#### Where the files live

```
~/.config/omarchy/mcp/permissions.json         yours, hand-edited, check it in
~/.config/omarchy/mcp/permissions.local.json   daemon-written, gitignore it
${XDG_STATE_HOME:-~/.local/state}/<plugin-id>/registry-seen.json
```

Both permission files sit in the config directory, following the
`settings.local.json` precedent this borrows from. The house rule sends state to
the state directory, and this bends it on purpose: a person opening
`~/.config/omarchy/mcp/` must see **everything that decides what an agent may
do**, in one place. Splitting the two halves of one answer across two
directories is how somebody reads half their permissions and believes it is all
of them. `registry-seen.json` stays in state — it genuinely is an observation of
the machine, and it would churn a tracked diff on every Omarchy upgrade.

Mode `0600` on the files, `0700` on the directory. Atomic writes — temp file in
the same directory, `fsync`, `rename` — because a half-written permissions file
read after a crash says something nobody chose. All three named in the README's
uninstall section.

#### Validation, and the schema

`pydantic` is already in the tree by way of the SDK, so the models are the
validator and `permissions.schema.json` is **generated** from them by
`make schema`, with a staleness gate in `make check` exactly as `TOOLS.md` has.
No new runtime dependency for a plugin that bootstraps its own venv, one source
of truth, and pydantic's per-field errors are already the shape the parse error
needs.

#### Grants are recorded

`activity.py` has `Sink.event`, used for `started` and `stopped`. A permanent
grant belongs in that file at least as much as a single tool call does — it is
what answers *"when did I allow this, and what was I looking at?"* six weeks
later, and it survives someone hand-deleting the line from
`permissions.local.json`:

```json
{"ts":"…","event":"permission","verb":"allow","route":"omarchy install app","via":"panel"}
{"ts":"…","event":"permission","verb":"revoke","route":"omarchy install app","via":"panel"}
{"ts":"…","event":"acknowledged","new":3}
```

A route and a verb, no arguments at all — narrower than a call record, not
wider. `log.activity = false` switches it off with everything else; a second
switch for one event kind is worse, and the file still holds the state.

#### Five commits

The boundary rule — tests in the same commit — makes one commit touching
`policy.py`, `gate.py`, `prompt.py`, a new module, both QML files and four docs
unreviewable exactly where review matters most.

| | Contents | After it |
|---|---|---|
| **a** ✅ | `permissions.py`: schema, matcher, rule pool, the ladder, void classification. Pure, unwired | unchanged |
| **b** ✅ | Wire it. `[policy]` leaves `config.toml`. `guardedDefault: "ask"`. Startup refusal, exit `78`, `Service.qml` stops respawning. `permissionsOk` | **the behaviour change**, alone in its diff. Guarded routes ask through N4; approve-once works |
| **c** ✅ | Explainer data: `rule`/`source` on annotated rows, `omarchy://permissions`, `--permissions` | you can see why every route is what it is |
| **d** | Answer vocabulary: helper verbs, `asking` frame, panel Allow-once / Always / Deny, `permission` events | "always" exists |
| **e** | The delta: `registry-seen.json`, forward-quarantine, `critical` notification, panel section, acknowledge | complete |

Between **b** and **e** an `allow` wildcard is fully forward-looking — the
weaker semantics this item rejects. Named rather than discovered: the window is
a few commits in an unreleased plugin, and `guardedDefault: "ask"` means a new
route asks anyway unless a wildcard already covers it.

#### What a and b did differently from this plan

Written before the code and left standing above; these are the places the code
disagrees with it, and why.

- **`check()` is separate from `parse()`.** The plan treated validation as one
  step. It is two, because half of it needs the registry and its answer changes
  without the file changing: the same document is clean today and has a dead
  rule after an `omarchy update`. `parse` raises on shape; `check` returns
  findings, and only an `error` finding stops the daemon.
- **A wildcard covering a sudo route is not an error.** Only an *exact* matcher
  is an assertion about one route. `omarchy update *` is a reasonable thing to
  write and several `update` routes need sudo — refusing it would make prefixes
  unusable, which is most of what the syntax is for.
- **`askTimeoutSeconds` moved into the document**, alongside `guardedDefault`,
  rather than staying a scalar somewhere else. Both are settings rather than
  rules, so `parse` returns an `Options` and `load` refuses a key claimed by two
  files: each decides one thing, so it belongs in one place.
- **`--check-permissions` landed in b, not c.** The plan filed it under the
  explainer. It stopped being optional the moment a defective document could
  stop the daemon: with the daemon down the panel is the only surface left, and
  it has to be able to say *"valid now, press Start"*.
- **`describe()` replaced two annotators, one commit early.** `resources.py` and
  `generic.py` each carried a comment claiming to be "one derivation, shared
  with the other" while being a copy of it. Both had to change anyway. Side
  effect: `omarchy://commands` now carries `refusal` on refused rows, which only
  `omarchy_search_commands` did before.
- **The suite could drive the machine it ran on.** Not a design change — a fault
  the behaviour change exposed, at the cost of a reboot. See **F29**; the fix is
  two autouse fixtures and a test file for them.

And from **c**:

- **`omarchy://permissions` lists rules, not commands.** The plan said
  "explainer data" without saying what the surface holds. `omarchy://commands`
  already annotates all 426 routes; a second listing of the same shape would
  have been the same thing twice. This one goes the other way — the *rules*,
  what each covers on this machine, and then only the routes whose answer the
  document had a hand in. The safe-and-unmatched majority and the sudo majority
  are counted rather than listed, because 370 rows saying "runs" and 120 saying
  "needs sudo" bury the fifty-odd that were actually decided.
- **A rule's expansion is capped at twelve and counted exactly.** `omarchy *`
  covers every route Omarchy ships; printing them buries the rules around it.
  The count is never approximate, only the enumeration is cut, and it says so.
- **`omarchy installed ...` is a real route.** The matcher design rejected
  mid-token wildcards partly to avoid upstream's `Bash(ls*)`-matches-`lsof`
  behaviour, and the example given for it was a hypothetical `omarchy installer`.
  It is not hypothetical: Omarchy ships `omarchy installed service dropbox`
  alongside `omarchy install app`, so a laxer matcher would have folded the
  `installed` routes into every `omarchy install *` rule anyone wrote. Found by
  a test of mine that asserted the wrong count, and now pinned by
  `test_a_prefix_does_not_match_a_longer_token`.

#### Watch for

- **A `config.toml` still carrying `[policy]`.** Unknown TOML keys are ignored
  silently today, which is the silent-no-op failure this item rejects
  everywhere else. `config.py` should know the dead keys by name and report
  them — *"`policy.allow` moved to permissions.json; this key does nothing"* —
  through the existing `problems` path. Report, not refuse: it is `config.toml`,
  not the permissions file.
- **The word "consent" is spoken for.** `consent.py` means *what an answer
  means*. This is `permissions.py`, and nothing here borrows the other word.
- **`SECURITY.md` gains the ladder and the forward-quarantine rule.** It is the
  threat model, and this is now the largest thing in it.
- **`config.example.toml` loses `[policy]`** and gains a
  `permissions.example.json` beside it.
- **The delta cannot be tested with one registry snapshot.** `conftest.py` pins
  a single `commands.json` for every test. Delta tests need a second state, and
  the cheap form is a small overlay applied to the pinned base — `{"added": […],
  "removed": […]}` — so the mutation is readable in one screen and lives next to
  the assertions about it. Matcher semantics need no registry at all and should
  not drag one in.

The tests are the specification, as ever. In the same commits: every sudo route
classifies `blocked` and no rule in any of the three lists promotes it;
`deny` → `ask` → `allow` with specificity never reordering; a rule never beats
`blocked` or never-storable; `allow`/`ask` on a sudo or never-storable route
refuses startup while `deny` on one is valid; a matcher matching nothing loads
void; a malformed matcher refuses startup with an error naming it; a broken file
at reload keeps last-known-good and never exits; a missing file and an empty
object both start; a new route matched only by `allow` is held at `ask` while
one matched by `deny` is denied on arrival; the snapshot advances only on
acknowledge and un-acknowledged deltas survive a restart; the daemon writes only
exact-route matchers; a click whose invariant fails refuses the call; an unknown
helper verb reads as no answer; `permissions.json` is never written.

### N11 — Close the log when the shell goes down

Found while verifying N6, and visible in its panel.

The activity log writes `stopped` from the ASGI lifespan shutdown, which F28
put there precisely because nothing after `uvicorn.run()` gets a turn. That
covers a SIGTERM: `omarchy-shell <id> stop` and `restart` both produce the
marker. It does not cover `omarchy restart shell`, where the shell is killed and
the daemon dies with it before uvicorn hands control to the lifespan at all.

The log then carries `started` with nothing closing the session before it:

```
15:09:48  -- stopped
15:09:48  -- started version=0.1.0 port=8765
15:10:33  -- started version=0.1.0 port=8765     <- omarchy restart shell
15:50:19  -- started version=0.1.0 port=8765     <- omarchy restart shell
```

Not fixable from inside the daemon — it is not given a chance to run — so this
is supervisor-side. `Service.qml` should SIGTERM its child when the service
object is destroyed, and give it the same bounded wait `halt()` already sets up,
so the ordinary path stays the daemon's own clean exit.

**Check that Quickshell runs `Component.onDestruction` on a shell teardown
before building on it.** If the shell is killed hard enough that QML destructors
do not run either, this cannot be fixed here and the honest move is to leave the
gap documented rather than to add a marker the daemon writes on startup about
the *previous* run, which would be a guess presented as a record.

Low priority: nothing is lost but the closing bracket of a session, and every
call in it is already on disk. It matters because N6 puts these lines in front
of a person, where a run of `daemon started` rows reads as a bug.

### N12 — An agent should not be able to switch off its own supervisor — done

Found while writing N7's security note. The write-up named one door.
Implementing it found three, plus a hole that had nothing to do with self-call
and was worse than the one being closed.

#### What was actually open

Every row below was `safe`, so an agent could do it with no question asked:

| Route | What it does |
|-------|-------------|
| `omarchy shell <target> <method> [args...]` | **is `omarchy_shell_call`**, as a registry route, reached through `omarchy_run` |
| `omarchy plugin disable \| remove \| update \| clone <id>` | silences this plugin without touching the daemon |
| `omarchy restart shell` | kills the shell, and the daemon is its child |
| `omarchy plugin add [git-url] [--enable] [--yes]` | **clones an arbitrary repository into the shell and loads it** |

The first row is why the original scope would have fixed nothing: guarding
`omarchy_shell_call` and leaving `omarchy shell` alone closes a door beside an
identical open one. `generic.py` builds `argv = [*cmd.argv_prefix, *call.args]`
and `omarchy shell` has no resolver, so arguments pass straight through.

The last row is not self-call at all. `plugin add` is a package install by
another name — it fetches code and the shell runs it in its own process — and
the whole `plugin` group sat outside `GUARDED_GROUPS`. An agent that cannot stop
the daemon but can install a plugin into the shell has a *better* attack, not a
worse one, so closing self-call without this would have been the smaller half.

#### What shipped

**Two tier changes.** `plugin` joins `GUARDED_GROUPS`, on the same reasoning as
`install`. `omarchy restart shell` joins `GUARDED_ROUTES`: guarded rather than
blocked, because restarting the shell is a thing a user legitimately asks for.

**A refusal that is not a tier.** `policy.self_refusal(route, args)` and
`policy.shell_call_refusal(target, method)` decide on what a call *names*, which
is why they cannot be part of `decide` — the same route is fine or refused
depending on its arguments, and `decide` never sees them. `gate.authorize` calls
it before computing the tier, so `omarchy_run` and every curated tool are
covered; `omarchy_shell_call` calls it directly, because that tool does not go
through the gate and relying on the other door being the only one is what this
item exists to stop.

**Refusal, not guarding.** Every mutating verb on this plugin's target has a
button in N6's panel, so the refusal names the panel and no legitimate use is
lost. Guarding it would have made "may I switch off the thing that records what
I do" a question a person could be talked into answering yes to.

**`status` and `recent` still answer.** Both are read-only and both answer
something an agent has a good reason to ask — *am I still connected*, *what have
I done*.

**`clientConfig` and `copyClientConfig` are refused too**, which the write-up did
not anticipate. They are not lifecycle verbs: `copyClientConfig` puts the bearer
token on the clipboard, and `omarchy_clipboard_read` is a tool this project
ships. The agent already holds a valid token, so the marginal gain is small —
but moving a secret onto a shared surface for no reason is not a thing to leave
in place once seen.

The refusal names the plugin, says what to press, and lists what is still
readable. An agent told only "no" tries the next spelling.

#### What it does not close

`omarchy plugin add` naming somebody *else's* repository is guarded now, not
refused: a user who wants a plugin installed can approve it. That is the right
line — the tier exists for exactly this — but it means the protection against
hostile plugin code is a person reading a prompt, and N4's prompt is what they
read. `policy.deny` is the way to take it off the table entirely.

### N13 — Prune dead rules from the panel

Came out of N10, which deliberately never prunes: a rule whose route vanished is
the delta's evidence, and a daemon that quietly edits a file is one the user
cannot reason about.

But dead rules accumulate, and the explainer will list them forever. A
user-initiated prune belongs in the panel, next to the delta: show exactly what
would be removed, remove it on a click, record it as a `permission` event.
`permissions.local.json` only — the user's own file is theirs to edit.

### N14 — Anti-habituation for the approval prompt

Came out of N10 flipping `guardedDefault` to `ask`. An agent can drive a stream
of guarded calls; `gate._pending` caps one prompt per session, but two attached
clients means two prompts, and enough `critical` notifications turn a click into
a reflex. A reflexive click is not consent.

What it needs: a per-route cooldown after a decline, and after N declines in a
window, auto-refuse the route without a prompt until the panel is opened. Its
own state and its own failure modes, which is why it is not bolted onto N10's
security-boundary diff.

## Deferred

Wanted, but not phase 6.

| Thing | Where it stands |
|-------|-----------------|
| Desktop-side approval surface (a panel, not just a notification) | **Taken by N10.** The reasoning held while *yes* was the only answer: a notification's one action (F25) covers it, and a panel was a large QML surface for a question one click answers. N10 needs two answers a notification cannot carry — *yes, always* and *no* — and N6 already built the panel, so it costs a row of buttons rather than a surface |
| Anything for the voice-driven path beyond N4 and N5 | The controller-support flow is controller → dictation → agent → this server. The last leg is an ordinary MCP client, so **nothing new is needed here to support it**. What that path does is raise the priority of two things already listed: N4's desktop notification stops being a nicety, because a user who dictated is by definition not watching the terminal where an elicitation would appear; and N5 becomes the only way to see what a lossy transcript actually caused. Revisit adding curated tools only if profiling shows the search-then-run round trip is the latency the user feels |
| More curated tools by default | No. The 15 exist for token economy, not coverage — `omarchy_run` already reaches everything, and every added schema is charged to every client on every session. The bar stays: the generic runner structurally cannot do it, or it is called constantly |

## Rejected

| Thing | Why not |
|-------|---------|
| One tool per omarchy command | Decision 2 |
| Hand-curated allowlist of commands | Defeats the self-maintaining registry; rots every Omarchy release |
| `ask_user` via `omarchy menu select` | Blocks the HTTP request until the user answers — which N4 does too, deliberately, because a parked async request costs one connection and nothing else. Elicitation was the better mechanism and is unreachable over this transport (F23); N4 asks through a notification instead. `menu select` stays rejected: it steals focus, and it is not where a critical prompt belongs |
| Per-tool `allow`/`ask`/`deny` map in config | 19 rows a person maintains by hand, and one more every time a tool is added. N4 hangs the same behaviour off the tier that derives itself, so it cannot rot — decision 4 |
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

## Phase 2 findings

Found by running the plugin in a live shell. None of these are visible to
`qmllint`, and all three shipped broken before the shell was actually started.

| # | Finding | Consequence |
|---|---------|-------------|
| F11 | `StdioCollector` needs `waitForEnd: true`, or its `text` is empty when read. Deciding in `onStreamFinished` while `onExited` also writes the same property is a race | The health probe reported `serving: false` forever. Both signals fire; the verdict belongs in `onExited`, which is the pattern `PluginRegistry.qml` uses |
| F12 | A purely interval-driven probe leaves the widget claiming the server is down for a full interval after every start | The probe now fires when the daemon reports `listening`, and polls at 2s while it looks down against 10s once it answers |
| F13 | Publishing state from a property-change handler writes a half-filled snapshot: assigning `calls` fired `writeState()` before `lastTool` had been assigned | State is written once, explicitly, after every field is set |

## Phase 3 findings

| # | Finding | Consequence |
|---|---------|-------------|
| F14 | `wl-copy` forks a child that stays alive to serve the selection, because Wayland has no clipboard daemon. That child inherits captured pipes, which then never close, so `subprocess.run` waits out its whole timeout and reports failure for a copy that already worked | Clipboard writes send output to `/dev/null` and pass text on stdin. Pinned by a round-trip test and a timing test |
| F15 | `grim -g` takes **logical** coordinates but writes **physical** pixels. On a 1.25-scaled monitor a 100x50 region returns 125x62 | Not a bug, but it makes `max_width` the meaningful cap and it invalidates the obvious test assertion |
| F16 | The `omarchy toggle` routes disagree about arguments: `bar` takes on/off, `idle` takes stay-awake/allow-idle, `screensaver` and `notification silencing` take nothing at all | Accepted state words are read out of the registry rather than hardcoded, so the table cannot drift. Forcing a route that only toggles now explains itself |
| F17 | `MCPServer.call_tool` returns a `CallToolResult`, not a content list | Only affects tests that drive the server directly |
| F18 | `omarchy capture screenshot` freezes the screen, copies to the clipboard, sends a notification, and writes a file into the user's Pictures directory | All four are right for a person pressing a key and wrong for an agent looking at the screen, which would otherwise litter the photo library. Screenshots go through `grim` directly |

## Phase 5 findings

| # | Finding | Consequence |
|---|---------|-------------|
| F19 | The daemon wrote `__pycache__` **into the installed plugin directory** — 22 `.pyc` files. Python caches bytecode next to the source it imports, and the source is in the directory Omarchy watches, so the daemon made the shell reload itself simply by starting | `PYTHONPYCACHEPREFIX` points the cache at the state directory. `tests/test_bootstrap.py` now reads the wrapper and fails if anything writes into the plugin directory. **The plugin was violating the rule its own `CLAUDE.md` states** |
| F20 | `hyprctl`'s per-workspace window count disagrees with its client list — it counts a group as one window — and `desktop_state` reported both | Found by an agent using the tool, which flagged the contradiction and had to pick which to believe. Counts are now derived from the windows actually returned |
| F21 | Several tests shelled out to the installed `omarchy`, so the suite could not run in CI and would change meaning on the next Omarchy update | An autouse fixture pins every test to the committed registry snapshot |

## Phase 6 findings

Verified against `mcp` 2.1.1 and a live Claude Code session attached to the
running daemon, by patching the installed plugin, restarting it, and calling a
tool from the client itself. The patch was reverted; nothing here was committed.

| # | Finding | Consequence |
|---|---------|-------------|
| F22 | Claude Code declares `elicitation: {}` — `ElicitationCapability(form=None, url=None)`. It names neither sub-mode, though the type's own docstring says *"Clients must support at least one mode"* | N3's `supports_asking` checks `form is not None` and therefore **refuses the client this project exists for**. A bare `elicitation` object has to count as form mode |
| F23 | Claude Code negotiates protocol **`2026-07-28`**, the *modern* era, whose streamable-HTTP transport stamps `can_send_request=False` at every construction site: *"the back-channel is closed by construction: a 2026-07-28 server cannot send requests to the client"*. `ctx.elicit` raises `NoBackChannelError` **inside our own process**, before anything reaches the wire | **Elicitation cannot work over this transport.** Not a client gap and not a spec gap to wait out — it is the negotiated revision. N4's stated mechanism had to change |
| F24 | The legacy era does have a back-channel (`can_send_request = not is_json_response_enabled`). A server can decline the modern era by overriding `server/discover` to advertise no modern version; clients then fall back to `initialize()` — `_probe.py` does this, and says the ts and go clients do too | An escape hatch exists and was **not taken**: pinning this server's protocol backwards to move a prompt into a terminal is the wrong trade for a daemon whose user is looking at a desktop. Untested besides — Claude Code re-probes only on a fresh connection |
| F25 | `omarchy notification send --exec` carries argv as an `omarchy-exec-argv` hint that the shell runs **on click**. Verified end to end: a critical notification's `--exec` ran within 6s of the click. `actions` is empty, so there is exactly one action | A desktop consent surface already exists, works in every protocol era and for every client, and puts the question where the person is. One action means **click is yes and silence is no** — which is precisely N3's rule |
| F26 | A click does not dismiss the notification — `omarchy notification dismiss` exists for exactly that, and matches a **summary substring**, not an id | Every ask needs a distinct headline, or two concurrent prompts dismiss each other |
| F28 | **Nothing after `uvicorn.run()` runs.** Uvicorn restores the default signal handler and re-raises the signal that stopped it, so the process dies *by signal* — verified, exit status 143 on SIGTERM. A `finally`, an `atexit`, a non-daemon thread: none of them get a turn | The activity log's `stopped` marker was never written and its queue was never flushed, on every ordinary shutdown. Not catchable by unit tests, which fake `uvicorn.run` as a normal return; found by SIGTERMing the real daemon. Shutdown work now hangs off the **ASGI lifespan**, which completes before the re-raise (`activity.Closing`) |
| F27 | `omarchy-shell <id> restart` left the daemon in `Waiting for connections to close` **indefinitely**, port unbound and process alive, because an attached client still held its stream open. It took `kill -9` | The reload rule `CLAUDE.md` documents hung whenever a client was attached, which is whenever it matters. **Fixed.** Three faults in one bug: uvicorn's `timeout_graceful_shutdown` defaults to waiting forever and an attached client never closes its stream; nothing escalated past `SIGTERM`; and `restart()` guessed 250ms, so `start()` returned early on a process that was still shutting down and left `wantRunning` false — which is why every hang also needed a manual `start`. The daemon now bounds its own shutdown, `Service.qml` puts a deadline on `SIGTERM`, and a restart waits for the actual exit. Verified with a client attached: 600ms, unattended |
| F29 | **The test suite rebooted the developer's machine.** Three tests used `omarchy system reboot` as their example of a guarded route, on the sound assumption that a guarded route is refused and nothing happens. N10's commit b flipped the guarded default from *refuse* to *ask*, so the gate resolved the call and raised a real `-u critical` notification instead — `tests/test_server.py` and `tests/test_activity.py` mock neither `prompt.send` nor `execute.run`. It was clicked, in good faith, and the reboot ran. The journal shows two `systemctl reboot --no-wall` two seconds apart: two of the three tests got that far before the machine went down | The lesson is not "pick a gentler route". A suite must not be **one behaviour change away from executing whatever it names**, and pinning the registry, the state directory and the resolver sources was never the same thing as pinning execution. Two autouse fixtures now stand in the way: `_no_real_omarchy` fails any spawn of `omarchy`, `omarchy-shell`, `hyprctl`, `qs` or `wl-copy` against the live system, and `_no_desktop_prompts` keeps `prompt.send` off the desktop entirely. Both are tested in `tests/test_conftest_guards.py`, because a guard nobody exercises stops working silently. The opt-ins are narrow and already existed: redirect `execute.SEARCH` at a fixture directory, or mark the test `needs_omarchy` |
