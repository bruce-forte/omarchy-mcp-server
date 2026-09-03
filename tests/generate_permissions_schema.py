"""Emit permissions.schema.json from the models that actually validate the file.

The schema is a build artifact, not a second source of truth. `permissions.py`'s
pydantic models are what the daemon enforces; this writes down what they accept
so an editor can check the file before the daemon ever sees it -- which matters
more here than for `config.toml`, because a defective permissions document stops
the daemon rather than falling back to defaults.

Run `make schema`. `make check` fails if the committed copy is stale.
"""

from __future__ import annotations

import json

from omarchy_mcp.permissions import PermissionsDocument

TITLE = "Omarchy MCP server permissions"

DESCRIPTION = (
    "What an agent connected to the Omarchy MCP server is permitted to do. "
    "Rules are consulted deny, then ask, then allow; the first match decides, "
    "and a narrower rule never reorders that. A matcher is either an exact "
    'route ("omarchy install app") or a prefix with a trailing " *" '
    '("omarchy install *"), which also matches the bare route. Commands '
    "requiring sudo are refused whatever this file says."
)


def main() -> None:
    schema = PermissionsDocument.model_json_schema(by_alias=True)
    schema = {
        "$schema": "https://json-schema.org/draft/2020-12/schema",
        "$id": "https://raw.githubusercontent.com/bruce-forte/omarchy-mcp-server/master/permissions.schema.json",
        "title": TITLE,
        "description": DESCRIPTION,
        **{k: v for k, v in schema.items() if k not in ("title", "description")},
    }
    print(json.dumps(schema, indent=2))


if __name__ == "__main__":
    main()
