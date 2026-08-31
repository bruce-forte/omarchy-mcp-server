# The dev environment lives outside the repository on purpose.
#
# `omarchy plugin validate` refuses symlinks anywhere inside a plugin folder,
# and a virtualenv is largely symlinks. A .venv in this directory therefore
# makes the plugin fail validation. Every target below sets
# UV_PROJECT_ENVIRONMENT so that a bare `uv run` never creates one here.
#
# Run `make check` before committing.

PLUGIN_ID := io.github.bruce-forte.mcp-server
export UV_PROJECT_ENVIRONMENT := $(if $(XDG_STATE_HOME),$(XDG_STATE_HOME),$(HOME)/.local/state)/$(PLUGIN_ID)/dev-venv

.PHONY: check test lint validate tools sync clean run

check: test lint validate

sync:
	uv sync --frozen

test: sync
	uv run --frozen pytest -q

lint:
	qmllint -I "$(OMARCHY_PATH)/shell" Service.qml BarWidget.qml
	bash -n bin/omarchy-mcpd

validate:
	omarchy plugin validate .

# Regenerate TOOLS.md from the server's own schemas, so the documentation
# cannot drift from what the server actually advertises.
tools: sync
	PYTHONPATH=src uv run --frozen python -m tests.generate_tools_doc > TOOLS.md

# Run the daemon in the foreground, as the plugin would.
run:
	./bin/omarchy-mcpd

clean:
	rm -rf .venv .pytest_cache .ruff_cache
	find . -name __pycache__ -type d -prune -exec rm -rf {} +
