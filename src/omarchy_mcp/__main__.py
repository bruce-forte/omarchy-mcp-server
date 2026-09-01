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

from . import __version__, activity, config as config_module, frames, token as token_module
from .paths import CONFIG_FILE
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


def _logger(level: str) -> logging.Logger:
    logging.basicConfig(
        stream=sys.stderr,
        level=LOG_LEVELS.get(level, logging.INFO),
        format="%(asctime)s %(levelname)s %(message)s",
    )
    return logging.getLogger("omarchy-mcp")


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
        settings = Settings(cfg)
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
