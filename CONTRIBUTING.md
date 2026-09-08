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

## Dependency lockfiles

`uv.lock` is the canonical dependency resolution. `requirements.lock` is its
generated export for `pip install --require-hashes`, including all runtime
dependencies, extras, groups, hashes, and platform/Python markers. Keep both
files in the same change. This replaces their independent update paths
(PMSERV-186); their development and hash-verified-install roles remain separate.

Use Python 3.11+ and **uv 0.11.25**, matching CI and the dev container:

```bash
python -m pip install uv==0.11.25   # in your development/tooling environment
make lock-check                   # offline, checks without writing either file
make lock-sync                    # after a pyproject edit or a Dependabot uv update
make lock-refresh                 # explicitly upgrade the full dependency tree
# For a targeted update instead:
python scripts/lockfiles.py refresh --package fastmcp
```

`lock-sync` runs `uv lock`, which retains compatible existing pins, then exports
the result. `lock-refresh` explicitly passes `--upgrade` (or `--upgrade-package`
for a targeted update), so existing output preferences cannot silently turn a
refresh into a no-op. These commands update repository lockfiles without
installing the project or changing the registered host tool. The script also
accepts `--uv /path/to/uv` before the subcommand.

`lock-check` uses `uv export --locked --offline` with a temporary empty cache.
It rejects stale project metadata and any difference in the generated pins,
markers or hashes. It does **not** assert that the selected versions are the
latest on PyPI. Upstream releases therefore do not make an unrelated PR fail;
updates enter through Dependabot or an explicit refresh. The check runs in
both CI and the tagged release's `verify` job, independently of whether
Dependabot succeeds. A Dependabot `uv.lock` update needs `lock-sync` before it
can pass. The `pip` updater watches declared project ranges, not
`requirements.lock`.

After a refresh, review the lockfile diff and test it in a **fresh disposable
environment**:

```bash
pip install --require-hashes -r requirements.lock
pip install -e . --no-deps
pip check
pytest -q
```

CI performs that hash-verified install and constraint check alongside the
existing floating dependency matrix. The editable install's build backend
(hatchling) is still obtained by pip build isolation and is outside the
runtime/dev dependency hash guarantee. See uv's
[export documentation](https://docs.astral.sh/uv/concepts/projects/export/) and
[locking and upgrade semantics](https://docs.astral.sh/uv/concepts/projects/sync/).

## Cutting a release

**[docs/RELEASING.md](docs/RELEASING.md)** is the runbook: the pre-flight
version-surface checklist, what each pipeline gate means when it goes red, the
recovery paths, and the PyPI trusted-publisher bindings.

The part nobody guesses: push the tag **by name**, not with `--follow-tags`.
Every tag that reaches the remote matches the `v*` trigger and starts a release
run, so `--follow-tags` — which also sends any unrelated stale annotated tags
you happen to have locally — can start runs you did not ask for.

The tag then stops at **one** manual approval in the `pypi` environment, which
publishes `pmlens` and the `pm-server` compatibility wrapper together, in that
order. Before PMSERV-174 it was two runs with two independent approvals, and
approving only one published a version whose committed plugin pin could not
resolve.

## Upgrading the host tool

When a change must reach the host's stable pmlens, do it **deliberately and
separately** from the dev container: `pipx upgrade pmlens` (after the release is
published). That keeps the "tool" upgrade an explicit, host-level action — never
a side effect of development.
