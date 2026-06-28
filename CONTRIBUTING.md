# Contributing to PM Lens (pmlens)

Thanks for helping improve PM Lens! This guide covers the **development
environment**, which is intentionally split into two halves.

## The "tool" vs "development" split (ADR-036)

pmlens is unusual: its installer and hooks **mutate host global config** —
`~/.claude/settings.json`, `~/.codex/config.toml`, `~/.pm/`. If you develop it
from a plain editable install (`pip install -e .`) that is *also* your registered
MCP server, a bug in the installer/migrate/hook code can corrupt the very tool
every one of your projects depends on.

So we separate the two:

| | What | Where |
|---|---|---|
| **Tool (production)** | a stable, released `pmlens` you actually use for PM | host, via `pipx install pmlens` (or a dedicated venv), registered at user scope |
| **Development** | the editable source you hack on | inside a **Docker container with a disposable HOME** |

The two **never share a HOME**, so development can exercise the real
install/migrate/hook paths without touching your host config.

## Prerequisites

- Docker (the dev workflow runs inside a container)
- `make`
- For the host "tool": `pipx` (recommended) — `pipx install pmlens`

## Quick start (containerized dev)

```bash
make dev-build     # build the dev image (once, and after Dockerfile changes)
make dev-test      # run the full test suite inside the isolated container
make dev-lint      # ruff check + format --check inside the container
make dev-shell     # interactive shell in the sandbox
make dev-clean     # wipe the disposable HOME + venv volumes (reset the sandbox)
```

`make help` lists every target. VS Code / `devcontainer` CLI users can instead
open `.devcontainer/devcontainer.json` directly — its `postCreateCommand` runs
the editable install.

## Why it's safe

The container runs as a **non-root user** whose `HOME` is `/home/pmdev`, backed
by a **named volume** — so the installer/hooks write a *disposable* `~/.claude`,
`~/.codex`, `~/.pm` inside the container. We **only** bind-mount the project
directory; we **never** bind-mount the host's `~/.claude`, `~/.codex`, `~/.ssh`,
etc. That redirected, container-local HOME is the structural guarantee that
development cannot corrupt your real host config.

## Exercising global side-effects (install / migrate / hooks)

This is the payoff of the sandbox — run the dangerous paths against the
disposable HOME:

```bash
make dev-sandbox   # dry-runs the installer against the container HOME

# or, interactively:
make dev-shell
# inside (HOME=/home/pmdev is disposable):
python -c "from pmlens import installer; print(installer.install(target='all', dry_run=True).message)"
ls -la ~/.claude ~/.codex ~/.pm 2>/dev/null   # inspect what WOULD be written
```

Your host's real `~/.claude/settings.json` stays untouched. Run `make dev-clean`
to reset the sandbox HOME between experiments.

## Conventions

- **Style/lint/test:** `ruff` (check + format) and `pytest`; both run in-container
  via `make dev-lint` / `make dev-test`. Match the surrounding code.
- **Security invariants** (do not break): `yaml.safe_load` only, no
  `subprocess(..., shell=True)`, `Path(...).resolve()` on user paths, Jinja
  `autoescape`, and the read-path RO invariant (read tools never shell out —
  ADR-028).
- **Docker-touching code is CLI-only:** anything that spawns Docker is a
  write/subprocess action and must NOT be an MCP read tool (it would violate the
  RO_ALLOWLIST / ADR-028).
- **Guided flow:** the `Docker Development` workflow (`pm_workflow_start`) and the
  `docker-dev` skill / `pmlens-docker-dev` agent walk through sandbox → change →
  test → exercise side-effects → verify isolation → promote.

## Releasing the host tool

When a change must reach the host's stable pmlens, do it **deliberately and
separately** from the dev container: `pipx upgrade pmlens` (after the release is
published). That keeps the "tool" upgrade an explicit, host-level action — never
a side effect of development.
