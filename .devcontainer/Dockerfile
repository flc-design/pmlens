# PM Lens — development container (PMSERV-140 / ADR-036).
#
# Purpose: a DISPOSABLE, isolated HOME so pmlens's installer/hooks/migrate code
# — which mutates ~/.claude/settings.json, ~/.codex/config.toml and ~/.pm/ —
# can be exercised without ever touching the HOST's real global config. The host
# keeps a stable, pip/pipx-installed pmlens as its "tool"; this container is the
# "development" half of that split (the two never share a HOME).
#
# Safety crux (see Agent research / ADR-036): HOME is a container-local path
# owned by a non-root user. We NEVER bind-mount the host's ~/.claude, ~/.codex,
# ~/.ssh, etc. — only the project dir is mounted at run time.

FROM python:3.13-slim

# uv installed from PyPI at a pinned version. We deliberately AVOID
# `COPY --from=ghcr.io/astral-sh/uv:<ver>` — the ghcr.io pull is unreliable on
# some networks (observed: DeadlineExceeded). pip + PyPI is the more available
# path; pip is already in the base image.
RUN pip install --no-cache-dir uv==0.11.25

# Minimal host tools: git (the test suite + tooling expect it; pmlens itself
# only ever READS .git/* as TEXT, never shells to git) and jq (the plugin shell
# hooks parse hook JSON with jq). ca-certificates ship in the base image.
RUN apt-get update \
    && apt-get install -y --no-install-recommends git jq \
    && rm -rf /var/lib/apt/lists/*

# Non-root user with a container-local HOME. The installer writes to
# /home/pmdev/.claude etc. — disposable, never the host's.
ARG USERNAME=pmdev
ARG UID=1000
RUN useradd --create-home --uid ${UID} --shell /bin/bash ${USERNAME}

USER ${USERNAME}
WORKDIR /workspaces/pmlens

# Put the venv in the pmdev-owned HOME (not under the bind-mounted workspace):
# a named volume mounted into the workspace would be root-owned and unwritable
# by the non-root user, and the host's own .venv would shadow it. VIRTUAL_ENV in
# HOME is writable, persists in the HOME volume, and uv targets it. Never
# auto-download a Python — use the base image's interpreter. PM_DEV_CONTAINER
# lets tooling detect the sandbox.
ENV VIRTUAL_ENV=/home/pmdev/.venv \
    UV_LINK_MODE=copy \
    UV_PYTHON_DOWNLOADS=never \
    PATH=/home/pmdev/.venv/bin:$PATH \
    PM_DEV_CONTAINER=1

# The editable dev install happens at first use (postCreateCommand / `make
# dev-*`) because the source is bind-mounted at run time, not baked into the
# image — so source edits are reflected live.
CMD ["bash"]
