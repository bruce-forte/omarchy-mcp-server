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

.PHONY: check test lint validate agents tools schema lsp sync clean run guard py elicit

check: guard agents test lint validate

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

# `ruff` and `pyright` need the dev environment -- one to find the config, the
# other to resolve `mcp`, `anyio` and the rest -- so this depends on `sync`
# where the QML and shell linters do not. Both are pinned in the dev group
# rather than run through `uvx`, so CI and a desktop check the same version.
#
# `ruff check` only. The line breaks here are hand-placed, and `ruff format`
# would rewrite most of the source to no benefit; see the note in pyproject.toml.
lint: sync
	uv run --frozen ruff check .
	uv run --frozen pyright
	qmllint -I "$(OMARCHY_PATH)/shell" Service.qml BarWidget.qml
	for f in bin/*; do bash -n "$$f"; done
	uvx --from shellcheck-py shellcheck --severity=style bin/*

validate:
	omarchy plugin validate .

# No agent-control file may ship.
#
# `omarchy plugin add` clones this repository into
# `~/.config/omarchy/plugins/<id>/`, and a coding agent working in that
# directory -- or anywhere above it -- discovers and obeys a `CLAUDE.md`, an
# `AGENTS.md`, a `.claude/` skill or a `.cursorrules` on its own. That is an
# instruction channel into somebody else's agent which no reviewer of this
# daemon ever looked at, and it is worst here of all places: this plugin hands
# an agent command execution. The marketplace security review refused the
# listing over exactly this; see ROADMAP N18.
#
# The list is checked against `git ls-files`, because what `plugin add` clones
# is what git tracks. It is recursive by construction -- `git ls-files` walks
# the whole tree -- so a file reintroduced three directories down is caught
# too. Matching is case-insensitive: a `claude.md` is read the same on the
# case-insensitive filesystems some contributors are on.
#
# Ordinary contributor documentation is fine under a name no agent auto-loads.
# That is what CONTRIBUTING.md is.
AGENT_FILES := (^|/)(\.(claude|codex|cursor|windsurf|aider|continue|roo|cline|gemini|opencode|amazonq|junie|trae|kilocode|augment|goose|agentfiles)([/.]|$$)|(claude|agent|agents|gemini|skill|llms?)\.md$$|copilot-instructions\.md$$|\.(cursorrules|windsurfrules|clinerules)$$|\.mcp\.json$$)
agents:
	@found=$$(git ls-files | grep -Ei '$(AGENT_FILES)' || true); \
	test -z "$$found" || { \
	  echo "error: agent-control files are tracked, and this repository ships"; \
	  echo "       to ~/.config/omarchy/plugins/, where an agent would read them:"; \
	  echo "$$found" | sed 's/^/         /'; \
	  echo "       Untrack them ('git rm --cached'); .gitignore already lists the"; \
	  echo "       usual names. Contributor docs belong in CONTRIBUTING.md."; \
	  exit 1; }

# What CI checks that the other targets do not: that the generated reference is
# current, and that the shipped config still pins nothing.
	@PYTHONPATH=src uv run --frozen python -m tests.generate_tools_doc > /tmp/TOOLS.md.check
	@diff -q TOOLS.md /tmp/TOOLS.md.check >/dev/null || { \
	  echo "error: TOOLS.md is stale. Run 'make tools' and commit it."; exit 1; }
	@grep -vE '^[[:space:]]*(#|$$)' config.example.toml | grep -vE '^\[' >/dev/null && { \
	  echo "error: config.example.toml has an uncommented key."; exit 1; } || true
	@PYTHONPATH=src uv run --frozen python -m tests.generate_permissions_schema \
	  > /tmp/permissions.schema.json.check
	@diff -q permissions.schema.json /tmp/permissions.schema.json.check >/dev/null || { \
	  echo "error: permissions.schema.json is stale. Run 'make schema' and commit it."; exit 1; }
	@test ! -e pyrightconfig.json || { \
	  PYTHONPATH=src uv run --frozen python -m tests.generate_pyright_config \
	    > /tmp/pyrightconfig.json.check && \
	  diff -q pyrightconfig.json /tmp/pyrightconfig.json.check >/dev/null; } || { \
	  echo "error: pyrightconfig.json is stale, so your editor and CI disagree."; \
	  echo "       Run 'make lsp'. It is git-ignored; delete it to opt out."; exit 1; }

# Regenerate TOOLS.md from the server's own schemas, so the documentation
# cannot drift from what the server actually advertises.
tools: sync
	PYTHONPATH=src uv run --frozen python -m tests.generate_tools_doc > TOOLS.md

# Point an editor's language server at the dev virtualenv.
#
# `make lint` runs pyright through `uv run`, which finds the environment. nvim,
# VS Code and Zed launch pyright directly, it looks for a `.venv` beside
# `pyproject.toml` and finds none -- the virtualenv is deliberately outside this
# directory -- and every third-party import reads as unresolved.
#
# The file is git-ignored and machine-specific: pyright expands neither `~` nor
# an environment variable in `venvPath`, so the absolute path has to be written
# out here. Re-run this after changing `[tool.pyright]`; `make check` fails if
# the file exists and has gone stale.
lsp: sync
	@PYTHONPATH=src uv run --frozen python -m tests.generate_pyright_config \
	  > pyrightconfig.json
	@echo "wrote pyrightconfig.json -> $(UV_PROJECT_ENVIRONMENT)"

# Regenerate the permissions JSON Schema from the pydantic models that enforce
# it, so an editor and the daemon cannot disagree about what the file may say.
schema: sync
	PYTHONPATH=src uv run --frozen python -m tests.generate_permissions_schema > permissions.schema.json

# Ad-hoc python against the dev environment, without creating ./.venv:
#   make py CMD='-c "import omarchy_mcp; print(omarchy_mcp.__version__)"'
py: sync
	@PYTHONPATH=src uv run --frozen python $(CMD)

# Run the daemon in the foreground, as the plugin would.
run:
	./bin/omarchy-mcpd

# Answer a real approval over MCP elicitation -- the consent path Claude Code
# cannot take, because the protocol revision it negotiates carries no
# back-channel (ROADMAP F23). Declines by default, so it changes nothing:
#   make elicit                                  # the form: which theme?
#   make elicit ARGS='--accept'                  # say yes to the consent question
#   make elicit ARGS='"omarchy theme set" Nord'  # skip the form
#
# It starts its own daemon **from this checkout** and stops it again, so what
# you are testing is the code in front of you. Talking to the installed plugin
# instead would leave you debugging whatever was committed when you last ran
# `omarchy plugin update`, which reads as the feature not working:
#   make py CMD='examples/elicit_client.py --port 8765'
ELICIT_PORT ?= 8799
elicit: sync
	@echo "starting a daemon from this checkout on port $(ELICIT_PORT)..."
	@./bin/omarchy-mcpd --port $(ELICIT_PORT) > /tmp/omarchy-mcp-elicit.log 2>&1 & \
	pid=$$!; \
	trap 'kill $$pid 2>/dev/null; wait $$pid 2>/dev/null' EXIT INT TERM; \
	for _ in $$(seq 1 60); do \
	  curl -sf "http://127.0.0.1:$(ELICIT_PORT)/health" >/dev/null 2>&1 && break; \
	  sleep 0.25; \
	done; \
	curl -sf "http://127.0.0.1:$(ELICIT_PORT)/health" >/dev/null 2>&1 || { \
	  echo "the daemon did not come up; see /tmp/omarchy-mcp-elicit.log"; exit 1; }; \
	uv run --frozen python examples/elicit_client.py --port $(ELICIT_PORT) $(ARGS)

clean:
	rm -rf .venv .pytest_cache .ruff_cache pyrightconfig.json
	find . -name __pycache__ -type d -prune -exec rm -rf {} +
