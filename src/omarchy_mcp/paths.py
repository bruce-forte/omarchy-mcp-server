"""Where this plugin is allowed to put things.

Omarchy watches ``~/.config/omarchy/plugins/`` and reloads the shell on any
write inside it, so nothing here ever resolves into the plugin directory.
"""

from __future__ import annotations

import os
from pathlib import Path

PLUGIN_ID = "io.github.bruce-forte.mcp-server"


def _xdg(var: str, default: str) -> Path:
    return Path(os.environ.get(var) or Path.home() / default)


#: Persistent state: the venv, the bearer token, the bootstrap stamp.
STATE_DIR = _xdg("XDG_STATE_HOME", ".local/state") / PLUGIN_ID

#: User configuration. Optional -- everything works without it.
CONFIG_DIR = _xdg("XDG_CONFIG_HOME", ".config") / "omarchy" / "mcp"
CONFIG_FILE = CONFIG_DIR / "config.toml"

#: Dies with the session.
RUNTIME_DIR = Path(os.environ.get("XDG_RUNTIME_DIR") or "/tmp")

TOKEN_FILE = STATE_DIR / "token"

#: Read by BarWidget.qml. Written by Service.qml, not by the daemon: only
#: panels and overlays can be called by the shell, so a widget cannot ask a
#: service anything and has to read a file instead.
STATE_FILE = RUNTIME_DIR / "omarchy-mcp.state"

OMARCHY_PATH = Path(os.environ.get("OMARCHY_PATH") or "/usr/share/omarchy")
