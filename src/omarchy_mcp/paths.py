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

#: What an agent is permitted to do. Two files, both here rather than one here
#: and one in the state directory: a person opening this folder has to see
#: everything that decides what an agent may do, in one place. Splitting the two
#: halves of one answer across two directories is how somebody reads half their
#: permissions and believes it is all of them.
#:
#: The first is the user's, hand-edited, and meant to be checked into a dotfiles
#: repository. The second is the daemon's, written when a person answers "always"
#: at the desk, and belongs in .gitignore. The daemon never writes the first.
PERMISSIONS_FILE = CONFIG_DIR / "permissions.json"
PERMISSIONS_LOCAL_FILE = CONFIG_DIR / "permissions.local.json"

#: Read in the order given: pooled either way, but the user's own rule is the one
#: the explainer should quote when both say the same thing.
PERMISSIONS_FILES = (PERMISSIONS_FILE, PERMISSIONS_LOCAL_FILE)

#: Dies with the session.
RUNTIME_DIR = Path(os.environ.get("XDG_RUNTIME_DIR") or "/tmp")

TOKEN_FILE = STATE_DIR / "token"

#: The activity log, and the one generation kept beside it. A filename rather
#: than a path everywhere it is configurable: a log inside the plugin directory
#: would make Omarchy reload the shell once per tool call.
ACTIVITY_FILE = "activity.jsonl"

#: Where a clicked approval notification drops its token. Dies with the
#: session, which is right: an approval that outlived the desktop it was granted
#: on would be a stale yes.
CONSENT_DIR = RUNTIME_DIR / PLUGIN_ID / "consent"

#: The plugin directory itself, resolved from this file rather than guessed, so
#: a checkout and an installed copy both find their own helper.
PLUGIN_DIR = Path(__file__).resolve().parents[2]

#: Run by a notification's --exec when the user clicks it.
CONSENT_HELPER = PLUGIN_DIR / "bin" / "omarchy-mcp-consent"

#: Read by BarWidget.qml. Written by Service.qml, not by the daemon: only
#: panels and overlays can be called by the shell, so a widget cannot ask a
#: service anything and has to read a file instead.
STATE_FILE = RUNTIME_DIR / "omarchy-mcp.state"

OMARCHY_PATH = Path(os.environ.get("OMARCHY_PATH") or "/usr/share/omarchy")
