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

from . import __version__, activity, config as config_module, token as token_module
from .paths import CONFIG_FILE
from .server import build, client_config_json, client_config_line
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
    parser.add_argument("--json", action="store_true", help="with --print-client-config, emit JSON")
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
        # Reads the file directly, so it works whether or not a daemon is running.
        for body in activity.tail(args.tail, activity.path_for(cfg)):
            print(activity.render(body))
        return 0

    tok = token_module.ensure()

    if args.print_client_config:
        print(client_config_json(cfg.port, tok) if args.json else client_config_line(cfg.port, tok))
        return 0

    with activity.writer(cfg, log) as sink:
        app = build(cfg, tok, log, stats=Stats(sink=sink))

        import uvicorn

        log.info("serving on http://127.0.0.1:%d/mcp", cfg.port)
        # One JSON line on stdout per state change: Service.qml reads this to
        # decide what the bar widget should say.
        print(
            json.dumps({"state": "listening", "port": cfg.port, "version": __version__}),
            flush=True,
        )

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
            print(
                json.dumps({"state": "failed", "port": cfg.port, "error": str(exc)}),
                flush=True,
            )
            return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
