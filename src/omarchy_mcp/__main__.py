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
    frames,
    notify,
    permissions as permissions_module,
    token as token_module,
)
from .paths import CONFIG_FILE, PERMISSIONS_FILE, PERMISSIONS_FILES
from .registry import RegistryError, all_commands
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
        commands = all_commands()
    except RegistryError as exc:
        log.warning("permissions not checked against the registry: %s", exc)
        return loaded

    findings = permissions_module.errors(permissions_module.check(loaded, commands))
    if findings:
        detail = "\n".join(f"  {f.rule.matcher!r}: {f.reason}" for f in findings)
        return _refuse_to_start(log, f"{PERMISSIONS_FILE}:\n{detail}")
    return loaded


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
        commands = all_commands()
    except RegistryError as exc:
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

    if args.check_permissions:
        return _check_permissions(log)

    permissions = _load_permissions(log)
    if permissions is None:
        return EX_CONFIG

    tok = token_module.ensure()

    if args.print_client_config:
        print(client_config_json(cfg.port, tok) if args.json else client_config_line(cfg.port, tok))
        return 0

    with activity.writer(cfg, log) as sink:
        # The frame is not the audit trail: it goes out whether or not the log
        # is on, because a user who turned the log off did not ask the bar to
        # stop telling them an agent is doing something.
        stats = Stats(sink=sink, on_call=lambda rec: frames.call(rec.tool, rec.result))
        # The holder the reloader swaps. Everything downstream reads it.
        settings = Settings(cfg, permissions)
        app = build(settings, tok, log, stats=stats, reload_from=CONFIG_FILE)
        if sink is not None:
            # The log is closed from the lifespan shutdown, because nothing
            # after uvicorn.run() runs -- it dies by signal (F28).
            app = activity.Closing(app, sink)

        import uvicorn

        log.info("serving on http://127.0.0.1:%d/mcp", cfg.port)
        frames.emit("listening", port=cfg.port, version=__version__)

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
