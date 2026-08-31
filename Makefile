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

.PHONY: check test lint validate tools sync clean run guard py

check: guard test lint validate

# A bare `uv run` or `uv sync` outside these targets creates ./.venv, and
# `omarchy plugin validate` then fails on the symlinks inside it with a message
# that does not explain itself. Fail early and say what to do instead.
# Locally a .venv must not exist at all, since `omarchy plugin validate` runs
# against this directory. CI checks the weaker property -- that it is not
# tracked by git -- because there `uv sync` creates one on purpose.
guard:
	@test ! -e .venv || { \
	  echo "error: ./.venv exists. omarchy plugin validate rejects symlinks in a"; \
	  echo "       plugin folder. Remove it with 'make clean' and use make targets,"; \
	  echo "       or 'make py CMD=...', which put the venv in the state directory."; \
	  exit 1; }
	@test -z "$$(git ls-files .venv)" || { \
	  echo "error: .venv is tracked by git."; exit 1; }

sync:
	uv sync --frozen

test: sync
	uv run --frozen pytest -q

lint:
	qmllint -I "$(OMARCHY_PATH)/shell" Service.qml BarWidget.qml
	bash -n bin/omarchy-mcpd
	uvx --from shellcheck-py shellcheck --severity=style bin/omarchy-mcpd

validate:
	omarchy plugin validate .

# What CI checks that the other targets do not: that the generated reference is
# current, and that the shipped config still pins nothing.
	@PYTHONPATH=src uv run --frozen python -m tests.generate_tools_doc > /tmp/TOOLS.md.check
	@diff -q TOOLS.md /tmp/TOOLS.md.check >/dev/null || { \
	  echo "error: TOOLS.md is stale. Run 'make tools' and commit it."; exit 1; }
	@grep -vE '^[[:space:]]*(#|$$)' config.example.toml | grep -vE '^\[' >/dev/null && { \
	  echo "error: config.example.toml has an uncommented key."; exit 1; } || true

# Regenerate TOOLS.md from the server's own schemas, so the documentation
# cannot drift from what the server actually advertises.
tools: sync
	PYTHONPATH=src uv run --frozen python -m tests.generate_tools_doc > TOOLS.md

# Ad-hoc python against the dev environment, without creating ./.venv:
#   make py CMD='-c "import omarchy_mcp; print(omarchy_mcp.__version__)"'
py: sync
	@PYTHONPATH=src uv run --frozen python $(CMD)

# Run the daemon in the foreground, as the plugin would.
run:
	./bin/omarchy-mcpd

clean:
	rm -rf .venv .pytest_cache .ruff_cache
	find . -name __pycache__ -type d -prune -exec rm -rf {} +
