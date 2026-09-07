"""Daemon entry point.

Started by ``bin/omarchy-mcpd``, which is started by ``Service.qml``. Being a
child of ``omarchy-shell`` is what gives this process ``WAYLAND_DISPLAY``,
``HYPRLAND_INSTANCE_SIGNATURE`` and ``DBUS_SESSION_BUS_ADDRESS`` from the live
graphical session -- every command that touches the desktop needs them.
"""

from __future__ import annotations

import argparse
import dataclasses
import json
import logging
import sys

from . import (
    __version__,
    activity,
    config as config_module,
    execute,
    frames,
    notify,
    delta as delta_module,
    permissions as permissions_module,
    registry,
    token as token_module,
)
from .paths import (
    CONFIG_FILE,
    PERMISSIONS_FILE,
    PERMISSIONS_FILES,
    PERMISSIONS_LOCAL_FILE,
    REGISTRY_SEEN_FILE,
)
from .server import build, client_config_json, client_config_line
from .settings import Settings
from .stats import Stats

LOG_LEVELS = {"debug": logging.DEBUG, "info": logging.INFO, "warn": logging.WARNING,
              "error": logging.ERROR}

#: How long a SIGTERM waits for in-flight work before connections are cancelled.
#:
#: Uvicorn's default is to wait forever, and an attached MCP client holds its
#: stream open for the life of the session -- so the documented restart
#: (`omarchy-shell <id> restart`) hung indefinitely whenever a client was
#: attached, which is whenever restarting matters. Three seconds is long enough
#: for a tool call that is nearly done and short enough that a restart feels
#: like one.
SHUTDOWN_GRACE_S = 3

#: `EX_CONFIG`. Exit code for a permissions document that will not load.
#:
#: Distinct because `Service.qml` must tell it apart from a crash. The
#: supervisor retries a crash with a backoff and, after three, advises a
#: `rebuild` that deletes the virtualenv -- exactly the wrong advice for a
#: missing comma, and it would keep retrying a file that cannot fix itself.
EX_CONFIG = 78


def _logger(level: str) -> logging.Logger:
    logging.basicConfig(
        stream=sys.stderr,
        level=LOG_LEVELS.get(level, logging.INFO),
        format="%(asctime)s %(levelname)s %(message)s",
    )
    return logging.getLogger("omarchy-mcp")


def _load_permissions(log):
    """The permissions document, or ``None`` when the daemon must not start.

    Unlike `config.toml`, a defective permissions file is not reported and
    ignored. Ignoring it would mean running under rules nobody wrote: "no rules"
    is not a safe floor, because a hand-written ``deny`` demotes routes the
    derivation calls safe, and there is no known-good document to fall back to
    at startup. So the daemon refuses to start, says exactly why on the desktop,
    and exits `EX_CONFIG` so the supervisor stops trying.

    A missing file is not a defect: no file at all is the ordinary state of a
    fresh install, and it means the derived tiers with nothing added.
    """
    try:
        loaded = permissions_module.load(PERMISSIONS_FILES)
    except permissions_module.PermissionsError as exc:
        return _refuse_to_start(log, str(exc))

    # Findings need the registry, which needs Omarchy. A machine without one has
    # bigger problems than its permissions file, and `registry.py` reports them;
    # skipping the check here is better than refusing to start over a registry
    # that was never going to load.
    try:
        commands = registry.all_commands()
    except registry.RegistryError as exc:
        log.warning("permissions not checked against the registry: %s", exc)
        return loaded

    findings = permissions_module.errors(permissions_module.check(loaded, commands))
    if findings:
        detail = "\n".join(f"  {f.rule.matcher!r}: {f.reason}" for f in findings)
        return _refuse_to_start(log, f"{PERMISSIONS_FILE}:\n{detail}")
    return loaded


def _review(perms, log):
    """What has changed under the rules since anybody last looked.

    Computed once at startup and held; `reload.py` recomputes it when the
    registry or the document moves. A fresh install has no snapshot, so it is
    baselined here and silently: every route is "new" on day one, and reviewing
    four hundred of them is the catalogue this feature exists to avoid.
    """
    try:
        commands = registry.all_commands()
    except registry.RegistryError as exc:
        log.warning("no registry, so nothing can be compared against it: %s", exc)
        return delta_module.Review()

    seen = delta_module.load_seen(REGISTRY_SEEN_FILE)
    found = delta_module.compute(seen, commands, perms)

    if found.first_run:
        delta_module.save_seen(REGISTRY_SEEN_FILE, commands)
        log.info("registry baselined: %d commands", len(commands))
        return found

    if not found:
        return found

    log.info("permissions review pending: %s", found.headline)
    notify.send(
        f"MCP server: {found.headline}",
        delta_module.message(found),
        urgency="critical" if found.urgent else "normal",
        log=log,
    )
    return found


def _prunable(log):
    """Dead rules in the daemon's own file, if the registry can be read."""
    try:
        commands = registry.all_commands()
    except registry.RegistryError:
        return ()
    return permissions_module.prunable(PERMISSIONS_LOCAL_FILE, commands)


def _print_review(log, *, as_json: bool = False) -> int:
    """What changed under the rules, computed fresh from the same inputs.

    Read-only, and it carries no token: acknowledging is a separate action on a
    surface a person is at. Recomputed rather than asked of the running daemon so
    it answers whether or not one is running -- which is the case the bar panel
    opens in most often.
    """
    try:
        loaded = permissions_module.load(PERMISSIONS_FILES)
    except permissions_module.PermissionsError as exc:
        print(f"{exc}\n\nThe rules cannot be read, so nothing can be compared to them.")
        return EX_CONFIG

    try:
        commands = registry.all_commands()
    except registry.RegistryError as exc:
        print(f"The registry is unavailable: {exc}")
        return 1

    found = delta_module.compute(
        delta_module.load_seen(REGISTRY_SEEN_FILE), commands, loaded
    )
    body = delta_module.as_dict(found)
    dead_local = permissions_module.prunable(PERMISSIONS_LOCAL_FILE, commands)
    body["prunable"] = [
        {"effect": r.effect.value, "matcher": r.matcher, "source": r.source}
        for r in dead_local
    ]
    if as_json:
        print(json.dumps(body, indent=2))
        return 0

    def _say_prunable():
        if not dead_local:
            return
        print(f"\n{len(dead_local)} rule(s) in permissions.local.json match nothing:")
        for rule in dead_local:
            print(f"  {rule.effect.value} {rule.matcher!r}")
        print("Prune them from the bar panel; they are shown before anything goes.")

    if found.first_run:
        print("No snapshot yet. The next start records one; nothing to review.")
        _say_prunable()
        return 0
    if not found:
        print("Nothing has changed under your rules since you last acknowledged.")
        _say_prunable()
        return 0

    print(f"{found.headline}\n")
    print(delta_module.message(found))
    for rule in found.widened:
        print(f"\n{rule.effect} {rule.matcher!r} ({rule.source}) now also covers:")
        for route in rule.routes:
            print(f"  {route}")
    held = [a for a in found.arrivals if a.quarantined]
    if held:
        print("\nHeld at ask until acknowledged:")
        for arrival in held:
            print(f"  {arrival.route}  (under {arrival.rule!r})")
    fresh = [a for a in found.arrivals if not a.quarantined]
    if fresh:
        print("\nNew:")
        for arrival in fresh:
            flag = "  [group never classified]" if arrival.unclassified else ""
            print(f"  {arrival.route:<44} {arrival.effect}{flag}")
    if found.gone:
        print(f"\nGone: {', '.join(found.gone)}")
    _say_prunable()
    print("\nAcknowledge in the bar panel to stop being told and advance the snapshot.")
    return 0


#: Worst first, because a rule carries at most one and the flag beside it is
#: the first thing read. An error stops the daemon; a shadowed rule silently
#: does nothing; a redundant one is merely removable.
FINDING_LEVELS = ("error", "void", "shadowed", "redundant")


def _print_permissions(log, *, as_json: bool = False) -> int:
    """What an agent may run, and which rule says so.

    The same report as `omarchy://permissions`, for a terminal. Answers the
    question the resource answers -- *why can the agent do this?* -- for someone
    who is not attached to an MCP client, which includes anyone whose daemon is
    refusing to start.
    """
    try:
        loaded = permissions_module.load(PERMISSIONS_FILES)
    except permissions_module.PermissionsError as exc:
        print(f"{exc}\n\nNothing is in force; the daemon would not start.")
        return EX_CONFIG

    try:
        commands = registry.all_commands()
    except registry.RegistryError as exc:
        print(f"The registry is unavailable, so rules cannot be expanded: {exc}")
        return 1

    report = permissions_module.explain(loaded, commands)
    if as_json:
        print(json.dumps(report, indent=2))
        return 0

    print(
        f"guardedDefault: {report['guardedDefault']} "
        f"(from {report['guardedDefaultSource']})"
    )
    print(f"askTimeoutSeconds: {report['askTimeoutSeconds']}")
    print(f"precedence: {' -> '.join(report['precedence'])}\n")

    if not report["rules"]:
        print("No rules. Guarded commands take the default above; everything else runs.\n")
    for rule in report["rules"]:
        flag = next((f" [{level}]" for level in FINDING_LEVELS if level in rule), "")
        print(f"{rule['effect']:>5}  {rule['matcher']}{flag}")
        print(f"       {rule['source']} -- covers {rule['covers']}")
        for route in rule["routes"]:
            print(f"         {route}")
        if "more" in rule:
            print(f"         ... and {rule['more']} more")
        for level in FINDING_LEVELS:
            if level in rule:
                print(f"       {level}: {rule[level]}")
        print()

    counts = report["counts"]["byEffect"]
    print(
        f"{report['counts']['commands']} commands: "
        + ", ".join(f"{counts[e]} {e}" for e in report["precedence"])
    )
    print(
        f"{report['counts']['listed']} of them are decided by this document; the rest "
        f"are safe and unmatched, or need sudo."
    )
    if report["neverGranted"]:
        print(
            "\nAsked about every time, never granted (the argument is a command line): "
            + ", ".join(report["neverGranted"])
        )
    return 0


def _check_permissions(log) -> int:
    """Validate the document and say so, without starting anything.

    Exists because a defective document now costs a startup: with the daemon
    down, the bar panel is the only surface left, and it has to be able to say
    "valid now, press Start" rather than making someone restart to find out.
    Prints rather than notifies -- this one is run by a person who is looking.
    """
    for path in PERMISSIONS_FILES:
        print(f"{path}: {'found' if path.exists() else 'not present'}")
    try:
        loaded = permissions_module.load(PERMISSIONS_FILES)
    except permissions_module.PermissionsError as exc:
        print(f"\n{exc}")
        return EX_CONFIG

    try:
        commands = registry.all_commands()
    except registry.RegistryError as exc:
        print(f"\nParses. Not checked against the registry: {exc}")
        return 0

    findings = permissions_module.check(loaded, commands)
    for finding in findings:
        print(f"  {finding.level}: {finding.rule.matcher!r} -- {finding.reason}")

    rules = len(loaded.rules)
    print(f"\n{rules} rule{'' if rules == 1 else 's'}, guardedDefault={loaded.guarded_default.value}")
    if permissions_module.errors(findings):
        print("The daemon would refuse to start.")
        return EX_CONFIG
    print("The daemon would start.")
    return 0


#: The three files a person edits, and the only things `--edit` will open.
#:
#: Fixed names rather than a path argument: the verb is reachable from a bar
#: button, and one that could be talked into opening -- or creating -- anything
#: else is a different feature with a different threat model.
EDITABLE = ("config", "local", "permissions")


def _editable(which: str):
    """The path for one of those names, looked up **when it is asked for**.

    Not a module-level dict. `from .paths import X` copies the value at import,
    so a table built at import time holds the real paths however the test suite
    redirects this module's bindings -- and `conftest.WRITTEN_PATHS` redirects
    exactly these. A table like that would have written the developer's own
    `permissions.json` the first time a test ran `--edit`.
    """
    return {
        "permissions": PERMISSIONS_FILE,
        "local": PERMISSIONS_LOCAL_FILE,
        "config": CONFIG_FILE,
    }[which]

#: How long the spawn itself is given. The editor is detached, so this bounds
#: starting it, not using it.
EDITOR_TIMEOUT_MS = 5_000


def _edit(which: str, log) -> int:
    """Open one of the three files in the user's editor, creating it if absent.

    `omarchy launch editor` is Omarchy's own opener, so this finds whatever
    editor the user actually has -- the case N8 refused to break by rewriting
    `PATH`.

    **Seeding is why this is a command rather than a button that spawns an
    editor.** A permissions file that does not exist yet opens as an empty
    buffer with no ``$schema`` line, and that line is what makes an editor
    validate a matcher before the daemon ever sees it. The template lives in
    `permissions.py` beside the writer, not in QML: it is business, not chrome.

    `config.toml` is not seeded here. `bin/omarchy-mcpd` owns that template,
    documented knobs and all, and a second copy in Python would be a second
    thing to keep current.
    """
    path = _editable(which)
    if which != "config" and permissions_module.seed(path):
        print(f"{path}: created")
    elif not path.exists():
        print(f"{path}: not present yet; it is written when the daemon starts")

    try:
        result = execute.run(
            ["omarchy", "launch", "editor", str(path)],
            timeout_ms=EDITOR_TIMEOUT_MS,
            max_output_b=4096,
            detach=True,
        )
    except execute.NotInstalled as exc:
        print(f"cannot open an editor: {exc}")
        return 1

    log.info("editing %s (pid %s)", path, result.pid)
    print(f"opened {path}")
    return 0


def _refuse_to_start(log, detail: str) -> None:
    """Say it three ways, because each reaches a different person.

    stderr for `journalctl`, a frame so the bar has the reason without reading
    anything, and a critical notification because the only other client is a
    language model that is not attached yet and never will be.
    """
    log.error("permissions: %s", detail)
    frames.emit("failed", error=f"permissions: {detail}")
    notify.send(
        "MCP server: permissions not loaded",
        f"{detail}\n\nThe server did not start. Fix the file and start it from "
        f"the bar panel, or run: omarchy-mcpd --check-permissions",
        urgency="critical",
        log=log,
    )
    return None


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="omarchy-mcpd", description=__doc__)
    parser.add_argument("--port", type=int, help="override the configured port")
    parser.add_argument(
        "--print-client-config",
        action="store_true",
        help="print the client setup command and exit",
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="with --print-client-config or --tail, emit JSON instead of text",
    )
    parser.add_argument(
        "--tail",
        type=int,
        nargs="?",
        const=20,
        metavar="N",
        help="print the last N activity records and exit",
    )
    parser.add_argument(
        "--review",
        action="store_true",
        help="print what has changed under your rules since you last acknowledged",
    )
    parser.add_argument(
        "--permissions",
        action="store_true",
        help="print the rules in force and what they cover, and exit",
    )
    parser.add_argument(
        "--edit",
        choices=EDITABLE,
        help="open a file in your editor, creating it from a template if absent",
    )
    parser.add_argument(
        "--check-permissions",
        action="store_true",
        help="validate permissions.json and exit; 0 if the daemon would start",
    )
    parser.add_argument("--version", action="version", version=__version__)
    args = parser.parse_args(argv)

    cfg = config_module.load()
    if args.port:
        cfg = dataclasses.replace(cfg, port=args.port)

    log = _logger(cfg.log_level)
    for problem in cfg.problems:
        log.warning("%s: %s", CONFIG_FILE, problem)

    if args.tail is not None:
        # Reads the file directly, so it works whether or not a daemon is
        # running -- which is the case the bar panel opens in most often.
        records = activity.tail(args.tail, activity.path_for(cfg))
        if args.json:
            # An envelope rather than a bare array, because an empty list is
            # ambiguous: nothing has happened yet, or the log is off and never
            # will. The bar panel has to tell those apart -- "no calls" shown
            # to someone whose counter reads 42 is a lie the widget would be
            # telling on the daemon's behalf.
            print(json.dumps({"activity": cfg.activity, "records": records}))
        else:
            for body in records:
                print(activity.render(body))
        return 0

    if args.review:
        return _print_review(log, as_json=args.json)

    if args.permissions:
        return _print_permissions(log, as_json=args.json)

    if args.check_permissions:
        return _check_permissions(log)

    if args.edit:
        return _edit(args.edit, log)

    permissions = _load_permissions(log)
    if permissions is None:
        return EX_CONFIG

    tok = token_module.ensure()

    if args.print_client_config:
        print(client_config_json(cfg.port, tok) if args.json else client_config_line(cfg.port, tok))
        return 0

    review = _review(permissions, log)

    with activity.writer(cfg, log) as sink:
        # The frame is not the audit trail: it goes out whether or not the log
        # is on, because a user who turned the log off did not ask the bar to
        # stop telling them an agent is doing something.
        stats = Stats(sink=sink, on_call=lambda rec: frames.call(rec.tool, rec.result))
        # The holder the reloader swaps. Everything downstream reads it.
        settings = Settings(cfg, permissions)
        settings.swap_unreviewed(review.quarantined)
        # Minted before the server is built, because the reloader is what
        # watches for the answer and it is built in there.
        dead = _prunable(log)
        prune_token = delta_module.new_token() if dead else ""

        app = build(
            settings,
            tok,
            log,
            stats=stats,
            reload_from=CONFIG_FILE,
            review=review,
            prune_token=prune_token,
        )
        if sink is not None:
            # The log is closed from the lifespan shutdown, because nothing
            # after uvicorn.run() runs -- it dies by signal (F28).
            app = activity.Closing(app, sink)

        import uvicorn

        log.info("serving on http://127.0.0.1:%d/mcp", cfg.port)
        frames.emit("listening", port=cfg.port, version=__version__)
        if review:
            # After `listening`, so the bar has somewhere to put it. The
            # notification is separate and has already gone.
            frames.review(
                review.token,
                review.headline,
                len(review.arrivals),
                len(review.widened),
                len(review.dead),
            )

        # Housekeeping rather than a warning, so it is offered and never
        # notified about: a rule that matches nothing grants nothing.
        if prune_token:
            frames.prunable(prune_token, len(dead))

        try:
            uvicorn.run(
                app,
                host=cfg.host,
                port=cfg.port,
                log_level=cfg.log_level,
                access_log=False,
                timeout_graceful_shutdown=SHUTDOWN_GRACE_S,
            )
        except OSError as exc:
            log.error("cannot listen on port %d: %s", cfg.port, exc)
            frames.emit("failed", port=cfg.port, error=str(exc))
            return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
