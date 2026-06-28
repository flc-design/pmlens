# PM Lens — developer tasks (PMSERV-140 / ADR-036).
#
# The dev-* targets run INSIDE an isolated Docker container whose HOME is
# container-local, so pmlens's installer/hooks/migrate code can be exercised
# without ever touching the host's ~/.claude / ~/.codex / ~/.pm. The host keeps
# a stable pip/pipx pmlens as its "tool"; this is the "development" half.
#
# Quick start:
#   make dev-build        # build the image (once / after Dockerfile changes)
#   make dev-test         # run the suite inside the isolated container
#   make dev-shell        # interactive shell in the sandbox
#   make dev-clean        # wipe the disposable HOME + venv volumes

IMAGE    := pmlens-dev
HOME_VOL := pmlens-dev-home
WORKDIR  := /workspaces/pmlens

# Attach a TTY only when one is present, so CI / non-interactive use still works.
TTY := $(shell [ -t 0 ] && echo -it || echo "")

# A named volume backs the disposable HOME (~/.claude, ~/.codex, ~/.pm AND the
# venv at $$VIRTUAL_ENV=/home/pmdev/.venv all live here, pmdev-owned). Only the
# project dir is bind-mounted; the host's HOME is never mounted.
DOCKER_RUN := docker run --rm $(TTY) \
	-v "$(CURDIR)":$(WORKDIR) \
	-v $(HOME_VOL):/home/pmdev \
	-w $(WORKDIR) $(IMAGE)

# Idempotently ensure the editable dev install exists in the persisted venv.
# The venv path is hardcoded (matches the image's VIRTUAL_ENV) rather than passed
# as a shell variable: a $VAR here would be expanded by the HOST shell (empty)
# before reaching the container, making `uv venv` fall back to the bind-mounted
# host .venv. Guard on pyvenv.cfg, not the dir, so a fresh empty volume re-creates.
ENSURE := ([ -f /home/pmdev/.venv/pyvenv.cfg ] || uv venv /home/pmdev/.venv) && uv pip install -e '.[dev]' --quiet

.PHONY: help dev-build dev-setup dev-shell dev-test dev-lint dev-sandbox dev-clean

help: ## Show this help
	@grep -E '^[a-zA-Z_-]+:.*?## .*$$' $(MAKEFILE_LIST) \
		| awk 'BEGIN{FS=":.*?## "}{printf "  \033[36m%-14s\033[0m %s\n", $$1, $$2}'

dev-build: ## Build the dev container image
	docker build -t $(IMAGE) -f .devcontainer/Dockerfile .

dev-setup: ## One-time editable install into the persisted venv volume
	$(DOCKER_RUN) bash -c "$(ENSURE)"

dev-shell: ## Interactive shell inside the isolated dev container
	$(DOCKER_RUN) bash -c "$(ENSURE); exec bash"

dev-test: ## Run the full test suite inside the isolated container
	$(DOCKER_RUN) bash -c "$(ENSURE); pytest -q"

dev-lint: ## Run ruff check + format check inside the container
	$(DOCKER_RUN) bash -c "$(ENSURE); ruff check src/ tests/ && ruff format --check src/ tests/"

dev-sandbox: ## Dry-run the installer against the DISPOSABLE container HOME (proves host isolation)
	$(DOCKER_RUN) bash -c "$(ENSURE); python -c \"from pmlens import installer; print(installer.install(target='all', dry_run=True).message)\"; echo '--- container HOME (disposable; host ~/.claude untouched) ---'; ls -la /home/pmdev"

dev-clean: ## Remove the disposable HOME volume (resets ~/.claude + the venv)
	-docker volume rm $(HOME_VOL)
