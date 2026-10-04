"""MCP auto-installer for pm-server.

Registers (or unregisters) pm-server as an MCP server in supported hosts.

Hosts (see :mod:`pmlens.hosts` for the registry that drives dispatch):
    - Claude Code: registers via ``claude mcp add`` (user scope).
    - Codex CLI / Grok Build: edit ``~/.codex/config.toml`` and
      ``~/.grok/config.toml`` via tomlkit with timestamped backup,
      idempotent field-level updates, and atomic write. Sub-tables under
      ``[mcp_servers.pmlens.*]`` (such as per-tool ``approval_mode``
      customizations) are preserved. The two formats are identical, so
      one implementation serves both.
    - Cursor: edits ``~/.cursor/mcp.json`` (``{"mcpServers": {...}}``) with
      the same backup/idempotency contract. An unparseable file is reported
      as ``failed`` and left untouched rather than overwritten.

Public surface:
    - ``install(target="claude-code") / uninstall(target="claude-code")``:
      orchestrators that dispatch to per-host installers and isolate any
      failure into a structured ``InstallResult`` entry.
    - ``install_claude_code() / uninstall_claude_code()``: per-host
      functions returning ``InstallResult``.
    - ``install_codex() / uninstall_codex()``: per-host functions for
      Codex CLI.
    - ``install_mcp() / uninstall_mcp()``: backward-compat wrappers
      preserved from v0.4.x; return the Claude Code message string.
      **Deprecated** (PMSERV-055): emit ``DeprecationWarning`` and are
      scheduled for removal in v1.0.0 — use ``install``/``uninstall`` or
      the per-host functions instead.
    - ``migrate_from_pm_agent()``: unchanged migration helper.
"""

from __future__ import annotations

import copy
import json
import os
import shutil
import subprocess
import sys
import warnings
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal

import tomlkit

from .hosts import HOSTS, HostSpec
from .utils import (
    _atomic_write_text,
    _codex_config_path,
    _host_config_path,
    _resolve_targets,
    _timestamped_backup,
)


# Per-host path seams. Every host-config path a writer touches is resolved
# through a module-level function here, NOT through HostSpec.config_file
# directly. That is deliberate: tests monkeypatch these names to redirect a
# writer at tmp_path, and a writer that reaches around them writes to the
# developer's REAL ~/.codex, ~/.cursor or ~/.grok. Keep the seam.
def _grok_config_path() -> Path:
    """Grok Build config path (lazy; honors monkeypatched HOME)."""
    return _host_config_path("grok")


def _cursor_config_path() -> Path:
    """Cursor MCP config path (lazy; honors monkeypatched HOME)."""
    return _host_config_path("cursor")


def _cursor_config_dir() -> Path:
    """Cursor's config DIRECTORY — its existence is the install signal.

    Unlike Codex and Grok, an installed Cursor may have no ``mcp.json`` yet
    (or an empty one), so the file's absence must not be read as "Cursor is
    not installed".
    """
    return _cursor_config_path().parent


#: Keys inside a ``pmlens`` server entry that pmlens owns and may rewrite or
#: delete. Anything else in that entry was put there by the user and survives
#: both install and uninstall.
#:
#: Per registration format, NOT shared: pmlens writes ``type`` only on JSON
#: hosts (Cursor documents it as required for stdio). Listing it for the TOML
#: hosts would make uninstall delete a ``type`` key the user wrote themselves —
#: a key pmlens never created — and would flip the residual-key computation, so
#: the message would claim a clean unregister where sub-tables were preserved.
_MANAGED_SERVER_KEYS_TOML: tuple[str, ...] = ("command", "args", "startup_timeout_sec")
_MANAGED_SERVER_KEYS_JSON: tuple[str, ...] = ("type", "command", "args", "startup_timeout_sec")

#: Env keys pmlens owns inside a server entry (see ``_lens_mode_active``).
_MANAGED_ENV_KEYS: tuple[str, ...] = ("PM_LENS", "PM_DESKTOP_WRITE")


def _managed_env(*, lens_mode: bool, desktop_write_mode: bool) -> dict[str, str]:
    """The env block pmlens wants on a server entry for the current mode."""
    env: dict[str, str] = {}
    if lens_mode:
        env["PM_LENS"] = "1"
    if desktop_write_mode:
        env["PM_DESKTOP_WRITE"] = "1"
    return env


def _lens_mode_active() -> bool:
    """True when PM_LENS is set to a truthy value in the installer's env.

    PMSERV-087 / WF-026 FINDING-F: when this is True, install_* functions
    must propagate ``PM_LENS=1`` into the host config so that the spawned
    pm-server process actually engages Lens mode. Otherwise a Lens
    distribution (e.g. via .mcpb) would silently start in RW mode.
    """
    return os.environ.get("PM_LENS", "").lower() in {"1", "true", "yes", "on"}


def _desktop_write_mode_active() -> bool:
    """True when PM_DESKTOP_WRITE is set to a truthy value (ADR-019 / WF-028).

    Mirror of ``_lens_mode_active``. When True, install_* functions
    propagate ``PM_DESKTOP_WRITE=1`` into the host config so the spawned
    pm-server process engages the Desktop outbox writers (OUTBOX_WRITE_ALLOWLIST
    becomes reachable under PM_LENS=1).
    """
    return os.environ.get("PM_DESKTOP_WRITE", "").lower() in {"1", "true", "yes", "on"}


# --- Result types ---------------------------------------------------------


#: The statuses a single-host (un)install can yield. Annotating
#: ``InstallResult.status`` with this Literal pushes validation to the type
#: checker: every ``InstallResult(status=...)`` construction site is checked
#: statically, so the runtime "unexpected value" fallback that used to guard
#: ``InstallSummary.overall_status`` is no longer load-bearing (PMSERV-054).
InstallStatus = Literal[
    "installed",
    "uninstalled",
    "already_registered",
    "skipped",
    "failed",
]


@dataclass(frozen=True)
class InstallResult:
    """Outcome of (un)registering pm-server in a single host.

    Attributes:
        target: Host identifier (e.g. ``"claude-code"`` or ``"codex"``).
        status: One of ``"installed"``, ``"uninstalled"``,
            ``"already_registered"``, ``"skipped"``, ``"failed"``.
        message: Human-readable detail. Backward-compat-sensitive
            substrings (``"already registered"``, ``"user scope"``,
            ``"Failed to register"``) are preserved here.
        backup_path: Path to a config-file backup if the host required
            file editing. ``None`` for hosts that mutate via a CLI
            command (such as Claude Code), and ``None`` in dry-run mode
            since no backup is created.
        is_dry_run: True when produced by a dry-run invocation. The
            ``status`` describes the action that *would* have been
            taken; no side effects (subprocess execution, backups,
            file writes) occurred.
    """

    target: str
    status: InstallStatus
    message: str
    backup_path: str | None = None
    is_dry_run: bool = False
    lens_mode: bool = False


#: Aggregation priority for ``InstallSummary.overall_status`` — most to least
#: significant. Typed against :data:`InstallStatus` so a stray value is a type
#: error; ``test_status_priority_covers_all_statuses`` guards the inverse
#: (a status added to the Literal but not given a priority slot) (PMSERV-054).
_STATUS_PRIORITY: tuple[InstallStatus, ...] = (
    "failed",
    "installed",
    "uninstalled",
    "already_registered",
    "skipped",
)


@dataclass(frozen=True)
class InstallSummary:
    """Aggregated results across hosts processed by ``install``/``uninstall``."""

    results: list[InstallResult] = field(default_factory=list)

    @property
    def overall_status(self) -> InstallStatus:
        """Aggregate status across hosts.

        Priority order: failed > installed > uninstalled >
        already_registered > skipped (see :data:`_STATUS_PRIORITY`).

        Returns ``"skipped"`` for an empty summary. Because every
        ``InstallResult.status`` is a valid :data:`InstallStatus` (enforced
        statically), a non-empty summary always matches one priority level —
        the final ``return`` is reached only when there are no results, not
        as a runtime defense against an unexpected value (PMSERV-054).
        """
        for level in _STATUS_PRIORITY:
            if any(r.status == level for r in self.results):
                return level
        return "skipped"

    @property
    def message(self) -> str:
        """Joined human-readable summary across all targets."""
        if not self.results:
            return "no targets processed"
        return "\n".join(f"[{r.target}] {r.message}" for r in self.results)


# --- Host: Claude Code ----------------------------------------------------


def install_claude_code(*, dry_run: bool = False) -> InstallResult:
    """Register pm-server as a Claude Code MCP server (user scope).

    Idempotent: if ``claude mcp get pmlens`` already succeeds, the
    call short-circuits with ``status="already_registered"``.

    Args:
        dry_run: When ``True``, the read-only detection (``shutil.which``
            and ``claude mcp get``) still runs so the predicted status
            is accurate, but ``claude mcp add`` is never executed.

    Returns:
        ``InstallResult`` with ``target="claude-code"``. In dry-run mode
        the ``is_dry_run`` field is ``True`` and the ``status`` reflects
        the outcome that *would* have occurred.
    """
    lens_mode = _lens_mode_active()
    desktop_write_mode = _desktop_write_mode_active()
    pm_server_path = shutil.which("pmlens") or shutil.which("pm-server")
    if pm_server_path is None:
        return InstallResult(
            target="claude-code",
            status="failed",
            message="pm-server command not found in PATH",
            is_dry_run=dry_run,
            lens_mode=lens_mode,
        )

    claude_path = shutil.which("claude")
    if claude_path is None:
        return InstallResult(
            target="claude-code",
            status="skipped",
            message="claude command not found. Install Claude Code first.",
            is_dry_run=dry_run,
            lens_mode=lens_mode,
        )

    result = subprocess.run(
        [claude_path, "mcp", "get", "pmlens"],
        capture_output=True,
        text=True,
        timeout=10,
    )
    if result.returncode == 0:
        return InstallResult(
            target="claude-code",
            status="already_registered",
            message="PM Lens is already registered in Claude Code",
            is_dry_run=dry_run,
            lens_mode=lens_mode,
        )

    if dry_run:
        suffix = " in Lens (read-only) mode" if lens_mode else ""
        return InstallResult(
            target="claude-code",
            status="installed",
            message=f"would register PM Lens in Claude Code (user scope){suffix}.",
            is_dry_run=True,
            lens_mode=lens_mode,
        )

    add_cmd: list[str] = [
        claude_path,
        "mcp",
        "add",
        "--transport",
        "stdio",
        "--scope",
        "user",
    ]
    if lens_mode:
        # PMSERV-087: propagate PM_LENS=1 to the spawned process. Without
        # this, a Lens-context installer call (e.g. from a Desktop .mcpb
        # distribution that re-uses install_claude_code) leaves Lens mode
        # disengaged at server startup.
        add_cmd.extend(["--env", "PM_LENS=1"])
    if desktop_write_mode:
        # PMSERV-100 / ADR-019: propagate PM_DESKTOP_WRITE=1 so the spawned
        # server registers the outbox-write tools. Mirrors the PM_LENS
        # propagation contract; harmless under PM_LENS=0 (no-op gating).
        add_cmd.extend(["--env", "PM_DESKTOP_WRITE=1"])
    add_cmd.extend(["pmlens", "--", pm_server_path, "serve"])

    result = subprocess.run(
        add_cmd,
        capture_output=True,
        text=True,
        timeout=10,
    )
    if result.returncode == 0:
        suffix = " in Lens (read-only) mode" if lens_mode else ""
        return InstallResult(
            target="claude-code",
            status="installed",
            message=(
                f"PM Lens registered in Claude Code (user scope){suffix}. "
                "Restart Claude Code to activate."
            ),
            lens_mode=lens_mode,
        )

    return InstallResult(
        target="claude-code",
        status="failed",
        message=f"Failed to register: {result.stderr}",
        lens_mode=lens_mode,
    )


def uninstall_claude_code(*, dry_run: bool = False) -> InstallResult:
    """Remove pm-server from Claude Code MCP servers (user scope).

    Performs a pre-check via ``claude mcp get pmlens`` so the
    "not registered" case yields ``status="skipped"`` instead of being
    folded into ``status="failed"`` (which is reserved for actual
    removal errors). This makes the live and dry-run paths share a
    single detection step.

    Args:
        dry_run: When ``True``, the read-only ``claude mcp get`` check
            still runs but ``claude mcp remove`` is never executed.

    Returns:
        ``InstallResult`` with ``target="claude-code"``.
    """
    claude_path = shutil.which("claude")
    if claude_path is None:
        return InstallResult(
            target="claude-code",
            status="skipped",
            message="claude command not found",
            is_dry_run=dry_run,
        )

    pre_check = subprocess.run(
        [claude_path, "mcp", "get", "pmlens"],
        capture_output=True,
        text=True,
        timeout=10,
    )
    if pre_check.returncode != 0:
        return InstallResult(
            target="claude-code",
            status="skipped",
            message="PM Lens not registered in Claude Code",
            is_dry_run=dry_run,
        )

    if dry_run:
        return InstallResult(
            target="claude-code",
            status="uninstalled",
            message="would unregister PM Lens from Claude Code",
            is_dry_run=True,
        )

    result = subprocess.run(
        [claude_path, "mcp", "remove", "pmlens", "--scope", "user"],
        capture_output=True,
        text=True,
        timeout=10,
    )
    if result.returncode == 0:
        return InstallResult(
            target="claude-code",
            status="uninstalled",
            message="PM Lens unregistered from Claude Code",
        )

    return InstallResult(
        target="claude-code",
        status="failed",
        message=f"Removal failed: {result.stderr}",
    )


# --- Host: Codex CLI ------------------------------------------------------


def _resolve_pm_server_path() -> Path:
    """Resolve the absolute server binary path for sandbox-safe registration.

    Prefers the ``pmlens`` binary (the canonical body identity) and falls
    back to the legacy ``pm-server`` name for mid-flight installs. Codex
    sandboxes restrict PATH inheritance, so a bare name does not resolve:
    the canonical location is the binary in the same directory as the
    running Python interpreter (works for pip / pipx / pyenv / venv
    installations). Falls back to ``shutil.which`` only if the canonical
    location is missing.

    The interpreter path is made absolute but its symlinks are NOT followed
    (PMSERV-254): a venv's ``bin/python`` is a symlink to the base
    interpreter, so resolving it left the venv and registered whatever
    ``pm-server`` sat next to the base Python — for a pipx release run from a
    pyenv base, that was the editable development install.

    Raises:
        FileNotFoundError: if neither pmlens nor pm-server can be located.
    """
    # PMSERV-137 A4: prefer the ``pmlens`` binary (the canonical body identity
    # the user installs via pipx at cutover); keep ``pm-server`` as the
    # mid-flight fallback so registration still resolves before the switch-over.
    bin_dir = Path(os.path.abspath(sys.executable)).parent
    for _name in ("pmlens", "pm-server"):
        candidate = bin_dir / _name
        if candidate.exists():
            return candidate
    fallback = shutil.which("pmlens") or shutil.which("pm-server")
    if fallback:
        return Path(fallback).resolve()
    raise FileNotFoundError("pm-server binary not found")


def _backup_codex_config(config_path: Path) -> Path:
    """Backward-compat wrapper. Delegates to ``utils._timestamped_backup``."""
    return _timestamped_backup(config_path)


def _atomic_write_toml(path: Path, doc: tomlkit.TOMLDocument) -> None:
    """Write a TOML document atomically. Delegates to ``utils._atomic_write_text``.

    The shared helper uses ``tempfile.mkstemp`` for a randomised temp
    name so concurrent writers cannot collide on a fixed ``.tmp`` suffix
    (PMSERV-044 cross-check R8 fix).
    """
    _atomic_write_text(path, tomlkit.dumps(doc))


def _install_toml_host(
    spec: HostSpec, config_path: Path, *, dry_run: bool = False
) -> InstallResult:
    """Register pmlens as an MCP server in a TOML-configured host.

    Codex CLI and Grok Build use structurally identical config: a
    ``[mcp_servers.pmlens]`` table with ``command`` / ``args`` / ``env``
    inside a user-level ``config.toml``. This is the Codex implementation
    generalised over :class:`~pmlens.hosts.HostSpec` rather than copied
    (PMSERV-165) — the idempotency comparison, the sub-table preservation and
    the dry-run prediction are subtle enough that a second copy would drift.

    Behaviour:
        - Detect: skip if the config file does not exist (no side effect on
          machines where the host is not installed).
        - Resolve: absolute pmlens path via :func:`_resolve_pm_server_path`.
        - Backup: timestamped copy alongside the config.
        - Update: field-level edits so user-defined sub-tables (per-tool
          ``approval_mode`` and the like) and surrounding comments survive.
        - Atomic write: tempfile + os.replace.

    Args:
        spec: The host to register with. Must have ``registration="toml"``.
        dry_run: When ``True``, detection, parsing, path resolution and
            in-memory mutation still occur (so the predicted status is
            accurate), but no backup is taken and nothing is written.

    Returns:
        ``InstallResult`` with ``target=spec.host_id``. On install/update,
        ``backup_path`` points at the saved-aside copy (``None`` for dry-run).
    """
    lens_mode = _lens_mode_active()
    desktop_write_mode = _desktop_write_mode_active()
    mcp_key = spec.mcp_key
    if not config_path.exists():
        return InstallResult(
            target=spec.host_id,
            status="skipped",
            message=f"{spec.config_label} not found — {spec.display_name} not installed",
            is_dry_run=dry_run,
            lens_mode=lens_mode,
        )

    pm_server_path = _resolve_pm_server_path()

    doc = tomlkit.parse(config_path.read_text(encoding="utf-8"))

    # Early return if already registered with matching command AND matching
    # PM_LENS/PM_DESKTOP_WRITE env presence — no mutation, no backup needed.
    # PMSERV-087 + PMSERV-100: we compare both env flags, so flipping either
    # one across reinstalls re-registers cleanly without stale env left over.
    if mcp_key in doc and "pmlens" in doc[mcp_key]:
        existing = doc[mcp_key]["pmlens"]
        existing_command = existing.get("command")
        existing_env = existing.get("env") or {}
        existing_has_lens = str(existing_env.get("PM_LENS", "")) == "1"
        existing_has_desktop_write = str(existing_env.get("PM_DESKTOP_WRITE", "")) == "1"
        if (
            existing_command is not None
            and str(existing_command) == str(pm_server_path)
            and existing_has_lens == lens_mode
            and existing_has_desktop_write == desktop_write_mode
        ):
            return InstallResult(
                target=spec.host_id,
                status="already_registered",
                message=f"PM Lens is already registered in {spec.display_name}",
                is_dry_run=dry_run,
                lens_mode=lens_mode,
            )

    if dry_run:
        # Predict the outcome without creating a backup or writing anything.
        suffix = " in Lens (read-only) mode" if lens_mode else ""
        if mcp_key not in doc or "pmlens" not in doc.get(mcp_key, {}):
            message = (
                f"would register PM Lens in {spec.display_name} (user scope){suffix}. "
                f"Would back up to {spec.config_label}.bak.<ts> before write."
            )
        else:
            message = (
                f"would update PM Lens in {spec.display_name}{suffix} "
                "(path or PM_LENS env changed). "
                f"Would back up to {spec.config_label}.bak.<ts> before write."
            )
        return InstallResult(
            target=spec.host_id,
            status="installed",
            message=message,
            backup_path=None,
            is_dry_run=True,
            lens_mode=lens_mode,
        )

    backup_path = _timestamped_backup(config_path)

    if mcp_key not in doc:
        doc[mcp_key] = tomlkit.table()
    if "pmlens" not in doc[mcp_key]:
        section = tomlkit.table()
        section["command"] = str(pm_server_path)
        section["args"] = ["serve"]
        section["startup_timeout_sec"] = 30
        if lens_mode or desktop_write_mode:
            env_table = tomlkit.inline_table()
            if lens_mode:
                env_table["PM_LENS"] = "1"
            if desktop_write_mode:
                env_table["PM_DESKTOP_WRITE"] = "1"
            section["env"] = env_table
        doc[mcp_key]["pmlens"] = section
        suffix = " in Lens (read-only) mode" if lens_mode else ""
        message = (
            f"PM Lens registered in {spec.display_name} (user scope){suffix}. "
            f"Backup at {backup_path}. Restart {spec.display_name} to activate."
        )
    else:
        section = doc[mcp_key]["pmlens"]
        section["command"] = str(pm_server_path)
        section["args"] = ["serve"]
        if "startup_timeout_sec" not in section:
            section["startup_timeout_sec"] = 30
        env_table = section.get("env")
        if lens_mode or desktop_write_mode:
            if env_table is None:
                env_table = tomlkit.inline_table()
            if lens_mode:
                env_table["PM_LENS"] = "1"
            elif "PM_LENS" in env_table:
                del env_table["PM_LENS"]
            if desktop_write_mode:
                env_table["PM_DESKTOP_WRITE"] = "1"
            elif "PM_DESKTOP_WRITE" in env_table:
                del env_table["PM_DESKTOP_WRITE"]
            section["env"] = env_table
        else:
            # Non-Lens, non-DesktopWrite reinstall must clear any stale flags
            # so the process starts in the requested mode. Leave other env
            # keys untouched (users may have added their own).
            if env_table is not None:
                for stale_key in ("PM_LENS", "PM_DESKTOP_WRITE"):
                    if stale_key in env_table:
                        del env_table[stale_key]
                if len(env_table) == 0:
                    del section["env"]
        suffix = " in Lens (read-only) mode" if lens_mode else ""
        message = (
            f"PM Lens updated in {spec.display_name}{suffix} "
            "(path or PM_LENS env changed). "
            f"Backup at {backup_path}. Restart {spec.display_name} to activate."
        )

    _atomic_write_toml(config_path, doc)

    return InstallResult(
        target=spec.host_id,
        status="installed",
        message=message,
        backup_path=str(backup_path),
        lens_mode=lens_mode,
    )


def install_codex(*, dry_run: bool = False) -> InstallResult:
    """Register pmlens as a Codex CLI MCP server (``~/.codex/config.toml``)."""
    return _install_toml_host(HOSTS["codex"], _codex_config_path(), dry_run=dry_run)


def install_grok(*, dry_run: bool = False) -> InstallResult:
    """Register pmlens as a Grok Build MCP server (``~/.grok/config.toml``).

    Grok Build reads Claude Code's and Cursor's MCP configs as *compatibility*
    sources already, so a user who ran ``pmlens install`` for Claude Code often
    finds pmlens working in Grok with no further action. Registering natively
    still matters: the compatibility scan is opt-out
    (``[compat.claude] mcps = false``), it is merged at LOWER priority than
    ``config.toml``, and it makes pmlens's presence explicit rather than
    inherited.
    """
    return _install_toml_host(HOSTS["grok"], _grok_config_path(), dry_run=dry_run)


def _uninstall_toml_host(
    spec: HostSpec, config_path: Path, *, dry_run: bool = False
) -> InstallResult:
    """Remove the pmlens registration from a TOML-configured host.

    Removes only the top-level fields (``command``, ``args``,
    ``startup_timeout_sec``). If the user has customized sub-tables such as
    ``[mcp_servers.pmlens.tools.pm_init]``, the parent section is preserved
    with a notice in the result message — those customizations are left
    untouched and require manual cleanup if no longer wanted.

    Args:
        spec: The host to unregister from. Must have ``registration="toml"``.
        dry_run: When ``True``, detection and parsing still run so the
            predicted outcome (full removal vs sub-tables-preserved) is
            accurate, but nothing is backed up or written.

    Returns:
        ``InstallResult`` with ``target=spec.host_id``.
    """
    mcp_key = spec.mcp_key
    if not config_path.exists():
        return InstallResult(
            target=spec.host_id,
            status="skipped",
            message=f"{spec.config_label} not found — nothing to uninstall",
            is_dry_run=dry_run,
        )

    doc = tomlkit.parse(config_path.read_text(encoding="utf-8"))

    if mcp_key not in doc or "pmlens" not in doc[mcp_key]:
        return InstallResult(
            target=spec.host_id,
            status="skipped",
            message=f"PM Lens not registered in {spec.display_name}",
            is_dry_run=dry_run,
        )

    if dry_run:
        # Predict whether the section would be fully removed or only
        # top-level fields stripped (sub-tables preserved).
        section = doc[mcp_key]["pmlens"]
        residual_keys = [k for k in section.keys() if k not in _MANAGED_SERVER_KEYS_TOML]
        if not residual_keys:
            message = (
                f"would unregister PM Lens from {spec.display_name}. "
                f"Would back up to {spec.config_label}.bak.<ts> before write."
            )
        else:
            message = (
                f"would remove PM Lens top-level fields from {spec.display_name}. "
                "Sub-tables would be preserved — remove manually if no longer needed. "
                f"Would back up to {spec.config_label}.bak.<ts> before write."
            )
        return InstallResult(
            target=spec.host_id,
            status="uninstalled",
            message=message,
            backup_path=None,
            is_dry_run=True,
        )

    backup_path = _timestamped_backup(config_path)

    section = doc[mcp_key]["pmlens"]
    for key in _MANAGED_SERVER_KEYS_TOML:
        if key in section:
            del section[key]

    if not section:
        del doc[mcp_key]["pmlens"]
        if not doc[mcp_key]:
            del doc[mcp_key]
        message = f"PM Lens unregistered from {spec.display_name}. Backup at {backup_path}."
    else:
        message = (
            f"PM Lens top-level fields removed from {spec.display_name}. "
            "Sub-tables preserved — remove manually if no longer needed. "
            f"Backup at {backup_path}."
        )

    _atomic_write_toml(config_path, doc)

    return InstallResult(
        target=spec.host_id,
        status="uninstalled",
        message=message,
        backup_path=str(backup_path),
    )


def uninstall_codex(*, dry_run: bool = False) -> InstallResult:
    """Remove the pmlens registration from Codex CLI config."""
    return _uninstall_toml_host(HOSTS["codex"], _codex_config_path(), dry_run=dry_run)


def uninstall_grok(*, dry_run: bool = False) -> InstallResult:
    """Remove the pmlens registration from Grok Build config."""
    return _uninstall_toml_host(HOSTS["grok"], _grok_config_path(), dry_run=dry_run)


# --- Host: Cursor (JSON) --------------------------------------------------


def _read_json_config(path: Path) -> dict:
    """Load a host's JSON MCP config, tolerating the empty-file case.

    A 0-byte ``~/.cursor/mcp.json`` is a real state on real machines (Cursor
    creates the file before anything writes servers into it), and
    ``json.loads("")`` raises. Treat empty/whitespace as "no servers yet".
    Malformed JSON is NOT swallowed — the caller turns it into a failure
    rather than overwriting a file it could not understand.
    """
    if not path.exists():
        return {}
    raw = path.read_text(encoding="utf-8").strip()
    if not raw:
        return {}
    doc = json.loads(raw)
    if not isinstance(doc, dict):
        raise ValueError(f"{path} does not contain a JSON object")
    return doc


def _json_shape_error(doc: dict, mcp_key: str) -> str | None:
    """Describe why ``doc`` cannot safely hold a server entry, or None if it can.

    ``doc.get(key) or {}`` reads a ``null`` or list value as "absent", and
    ``setdefault`` will not replace an existing non-dict, so without this check
    the assignment ``doc[key]["pmlens"] = ...`` raises TypeError — after the
    backup and mkdir side effects have already run.
    """
    # `null` counts as non-object: `doc.get(key) or {}` reads it as absent, but
    # `setdefault` will not replace it, so the assignment would still blow up.
    # Refusing beats normalising — we did not write that value and do not know
    # what the user meant by it.
    if mcp_key in doc and not isinstance(doc[mcp_key], dict):
        return f'has a non-object "{mcp_key}" ({type(doc[mcp_key]).__name__})'
    servers = doc.get(mcp_key) or {}
    entry = servers.get("pmlens") if isinstance(servers, dict) else None
    if entry is not None and not isinstance(entry, dict):
        return f'has a non-object "{mcp_key}.pmlens" ({type(entry).__name__})'
    return None


def _json_residual_keys(entry: dict) -> list[str]:
    """Keys in a JSON server entry that pmlens did NOT put there."""
    residual = [k for k in entry if k not in _MANAGED_SERVER_KEYS_JSON and k != "env"]
    residual += [k for k in (entry.get("env") or {}) if k not in _MANAGED_ENV_KEYS]
    return residual


def _write_json_config(path: Path, doc: dict) -> None:
    """Atomically write a host's JSON MCP config, with a trailing newline."""
    _atomic_write_text(path, json.dumps(doc, indent=2, ensure_ascii=False) + "\n")


def _install_json_host(
    spec: HostSpec, config_path: Path, marker: Path, *, dry_run: bool = False
) -> InstallResult:
    """Register pmlens as an MCP server in a JSON-configured host (Cursor).

    Same contract as :func:`_install_toml_host`, over
    ``{"mcpServers": {"pmlens": {...}}}``. Two differences that matter:

    * **Presence is signalled by the directory, not the file.** ``~/.cursor``
      exists on an installed Cursor; ``mcp.json`` may not exist yet, or may
      exist as an empty file. So the config file is created when absent
      instead of being treated as "host not installed".
    * **Cursor documents ``type`` as required** for stdio servers, unlike
      Claude Code's ``.mcp.json`` where the transport is inferred.
    """
    lens_mode = _lens_mode_active()
    desktop_write_mode = _desktop_write_mode_active()

    if not marker.exists():
        return InstallResult(
            target=spec.host_id,
            status="skipped",
            message=f"{spec.config_label} not found — {spec.display_name} not installed",
            is_dry_run=dry_run,
            lens_mode=lens_mode,
        )

    pm_server_path = _resolve_pm_server_path()
    try:
        doc = _read_json_config(config_path)
    except (ValueError, json.JSONDecodeError) as e:
        return InstallResult(
            target=spec.host_id,
            status="failed",
            message=(
                f"{spec.config_label} is not valid JSON ({e}); refusing to "
                "overwrite it. Fix or move the file, then re-run."
            ),
            is_dry_run=dry_run,
            lens_mode=lens_mode,
        )

    # Validate the SHAPE before any side effect. `_read_json_config` only
    # checks the top level, and `doc.get(key) or {}` masks a null/array
    # `mcpServers` — which would then blow up with a TypeError at the
    # assignment below, AFTER the backup and mkdir had already run.
    shape_error = _json_shape_error(doc, spec.mcp_key)
    if shape_error is not None:
        return InstallResult(
            target=spec.host_id,
            status="failed",
            message=(
                f"{spec.config_label} {shape_error}; refusing to overwrite it. "
                "Fix or move the file, then re-run."
            ),
            is_dry_run=dry_run,
            lens_mode=lens_mode,
        )

    env = _managed_env(lens_mode=lens_mode, desktop_write_mode=desktop_write_mode)
    servers = doc.get(spec.mcp_key) or {}
    existing = servers.get("pmlens") or {}
    # PMSERV165-1: merge, never replace. The TOML path edits the env table
    # field by field so unrelated user keys survive; doing otherwise here would
    # silently delete them on every install.
    desired_env = {
        k: v for k, v in (existing.get("env") or {}).items() if k not in _MANAGED_ENV_KEYS
    }
    desired_env.update(env)
    if (
        existing.get("command") == str(pm_server_path)
        and existing.get("args") == ["serve"]
        # `type` is part of what we write, so it must be part of what we
        # compare — otherwise an entry missing the field Cursor requires
        # short-circuits to already_registered and is never repaired.
        and existing.get("type") == "stdio"
        # Compare against what we WOULD write, not against the managed subset:
        # comparing the user's full env to `env` never matches when they added
        # a key, so every run rewrote a byte-identical file and took a backup.
        and (existing.get("env") or {}) == desired_env
    ):
        return InstallResult(
            target=spec.host_id,
            status="already_registered",
            message=f"PM Lens is already registered in {spec.display_name}",
            is_dry_run=dry_run,
            lens_mode=lens_mode,
        )

    suffix = " in Lens (read-only) mode" if lens_mode else ""
    verb = "update" if existing else "register"
    if dry_run:
        return InstallResult(
            target=spec.host_id,
            status="installed",
            message=(
                f"would {verb} PM Lens in {spec.display_name} (user scope){suffix}. "
                f"Would back up to {spec.config_label}.bak.<ts> before write."
                if config_path.exists()
                else f"would {verb} PM Lens in {spec.display_name} (user scope){suffix}. "
                f"Would create {spec.config_label}."
            ),
            backup_path=None,
            is_dry_run=True,
            lens_mode=lens_mode,
        )

    backup_path = _timestamped_backup(config_path) if config_path.exists() else None
    config_path.parent.mkdir(parents=True, exist_ok=True)

    # Field-level edit, mirroring the TOML path: keys the user added to OUR
    # entry (and every other server) survive.
    entry = dict(existing)
    entry["type"] = "stdio"
    entry["command"] = str(pm_server_path)
    entry["args"] = ["serve"]
    if desired_env:
        entry["env"] = desired_env
    else:
        entry.pop("env", None)

    doc.setdefault(spec.mcp_key, {})
    doc[spec.mcp_key]["pmlens"] = entry
    _write_json_config(config_path, doc)

    where = f"Backup at {backup_path}." if backup_path else f"Created {spec.config_label}."
    return InstallResult(
        target=spec.host_id,
        status="installed",
        message=(
            f"PM Lens {'updated in' if existing else 'registered in'} "
            f"{spec.display_name} (user scope){suffix}. {where} "
            f"Restart {spec.display_name} to activate."
        ),
        backup_path=str(backup_path) if backup_path else None,
        lens_mode=lens_mode,
    )


def _uninstall_json_host(
    spec: HostSpec, config_path: Path, *, dry_run: bool = False
) -> InstallResult:
    """Remove the pmlens registration from a JSON-configured host (Cursor)."""
    if not config_path.exists():
        return InstallResult(
            target=spec.host_id,
            status="skipped",
            message=f"{spec.config_label} not found — nothing to uninstall",
            is_dry_run=dry_run,
        )

    try:
        doc = _read_json_config(config_path)
    except (ValueError, json.JSONDecodeError) as e:
        return InstallResult(
            target=spec.host_id,
            status="failed",
            message=(
                f"{spec.config_label} is not valid JSON ({e}); refusing to "
                "overwrite it. Fix or move the file, then re-run."
            ),
            is_dry_run=dry_run,
        )

    shape_error = _json_shape_error(doc, spec.mcp_key)
    if shape_error is not None:
        return InstallResult(
            target=spec.host_id,
            status="failed",
            message=(
                f"{spec.config_label} {shape_error}; refusing to overwrite it. "
                "Fix or move the file, then re-run."
            ),
            is_dry_run=dry_run,
        )

    servers = doc.get(spec.mcp_key) or {}
    if "pmlens" not in servers:
        return InstallResult(
            target=spec.host_id,
            status="skipped",
            message=f"PM Lens not registered in {spec.display_name}",
            is_dry_run=dry_run,
        )

    entry = servers["pmlens"]
    residual_keys = _json_residual_keys(entry)

    if dry_run:
        message = (
            f"would unregister PM Lens from {spec.display_name}. "
            f"Would back up to {spec.config_label}.bak.<ts> before write."
            if not residual_keys
            else (
                f"would remove PM Lens managed fields from {spec.display_name}. "
                "User-added keys would be preserved — remove manually if no longer needed. "
                f"Would back up to {spec.config_label}.bak.<ts> before write."
            )
        )
        return InstallResult(
            target=spec.host_id,
            status="uninstalled",
            message=message,
            backup_path=None,
            is_dry_run=True,
        )

    backup_path = _timestamped_backup(config_path)
    for key in _MANAGED_SERVER_KEYS_JSON:
        entry.pop(key, None)
    # pmlens wrote these env keys, so pmlens removes them. Leaving them behind
    # produced `{"pmlens": {"env": {"PM_LENS": "1"}}}` — an MCP server entry
    # with no command — while the message claimed "user-added keys preserved".
    leftover_env = {k: v for k, v in (entry.get("env") or {}).items() if k not in _MANAGED_ENV_KEYS}
    if leftover_env:
        entry["env"] = leftover_env
    else:
        entry.pop("env", None)

    if not entry:
        del servers["pmlens"]
        if not servers:
            doc.pop(spec.mcp_key, None)
        message = f"PM Lens unregistered from {spec.display_name}. Backup at {backup_path}."
    else:
        message = (
            f"PM Lens managed fields removed from {spec.display_name}. "
            "User-added keys preserved — remove manually if no longer needed. "
            f"Backup at {backup_path}."
        )

    _write_json_config(config_path, doc)

    return InstallResult(
        target=spec.host_id,
        status="uninstalled",
        message=message,
        backup_path=str(backup_path),
    )


def install_cursor(*, dry_run: bool = False) -> InstallResult:
    """Register pmlens as a Cursor MCP server (``~/.cursor/mcp.json``)."""
    return _install_json_host(
        HOSTS["cursor"], _cursor_config_path(), _cursor_config_dir(), dry_run=dry_run
    )


def uninstall_cursor(*, dry_run: bool = False) -> InstallResult:
    """Remove the pmlens registration from Cursor's MCP config."""
    return _uninstall_json_host(HOSTS["cursor"], _cursor_config_path(), dry_run=dry_run)


# --- Orchestrator ---------------------------------------------------------


def _safe_call(fn: Callable[[], InstallResult], host: str) -> InstallResult:
    """Run a host-specific installer, isolating exceptions per host.

    A failure in one host MUST NOT abort sibling hosts (ADR-007 case C).
    """
    try:
        return fn()
    except Exception as e:
        return InstallResult(
            target=host,
            status="failed",
            message=f"unexpected error: {e}",
        )


#: host_id -> per-host (un)installer. Keyed off the same registry as
#: ``_KNOWN_HOSTS``, and ``_dispatch`` fails loudly on a host missing from
#: these tables. The previous if/elif chains had NO else, so a host added to
#: the registry but not wired up produced zero results, no error, and an
#: overall_status of "skipped" — a half-added host that looked like success.
_INSTALLERS: dict[str, Callable[..., InstallResult]] = {
    "claude-code": install_claude_code,
    "codex": install_codex,
    "cursor": install_cursor,
    "grok": install_grok,
}

_UNINSTALLERS: dict[str, Callable[..., InstallResult]] = {
    "claude-code": uninstall_claude_code,
    "codex": uninstall_codex,
    "cursor": uninstall_cursor,
    "grok": uninstall_grok,
}


def _dispatch(
    target: str, table: dict[str, Callable[..., InstallResult]], *, dry_run: bool
) -> list[InstallResult]:
    """Run every host in ``target`` through ``table``, isolating failures."""
    results: list[InstallResult] = []
    for host in _resolve_targets(target):
        fn = table.get(host)
        if fn is None:
            results.append(
                InstallResult(
                    target=host,
                    status="failed",
                    message=(
                        f"host {host!r} is in the host registry but has no "
                        "installer wired up — this is a pmlens bug, not a "
                        "configuration problem"
                    ),
                    is_dry_run=dry_run,
                )
            )
            continue
        results.append(_safe_call(lambda fn=fn: fn(dry_run=dry_run), host))
    return results


def install(target: str = "claude-code", *, dry_run: bool = False) -> InstallSummary:
    """Register pm-server with one or more host MCP clients.

    Args:
        target: ``"auto"``, ``"all"``, or a single host id from
            :data:`pmlens.hosts.HOSTS` — currently ``"claude-code"``
            (the default), ``"codex"``, ``"cursor"`` or ``"grok"``.
            ``"auto"`` and ``"all"`` are synonyms here; both run every
            known host and skip the ones that aren't installed. The
            authoritative list is ``utils.TARGET_CHOICES``.
        dry_run: When ``True``, propagated to every host installer; no
            side effects (subprocess execution, backups, file writes)
            occur, but read-only detection still runs so each result's
            ``status`` reflects the action that *would* have been taken.

    Returns:
        ``InstallSummary`` with one ``InstallResult`` per processed host.
    """
    return InstallSummary(results=_dispatch(target, _INSTALLERS, dry_run=dry_run))


def uninstall(target: str = "claude-code", *, dry_run: bool = False) -> InstallSummary:
    """Remove pm-server registrations from one or more host MCP clients.

    Symmetric to :func:`install`. Same target and dry-run semantics.
    """
    return InstallSummary(results=_dispatch(target, _UNINSTALLERS, dry_run=dry_run))


# --- Backward-compat wrappers (v0.4.x public API) -------------------------


# Removal target for the v0.4.x compat wrappers (PMSERV-055). Kept as a
# named constant so the message and any future gating stay in one place.
_LEGACY_WRAPPER_REMOVAL_VERSION = "1.0.0"


def install_mcp() -> str:
    """Backward-compat wrapper kept from v0.4.x.

    Returns the human-readable message from :func:`install_claude_code`.
    The structured form is ``install(target="claude-code")`` which yields
    an :class:`InstallSummary`.

    .. deprecated::
        Scheduled for removal in pm-server v1.0.0 (PMSERV-055). Use
        ``install(target="claude-code")`` for the structured
        :class:`InstallSummary`, or :func:`install_claude_code` for the
        single :class:`InstallResult` whose ``.message`` this returns.
    """
    warnings.warn(
        "install_mcp() is deprecated and will be removed in pm-server "
        f'v{_LEGACY_WRAPPER_REMOVAL_VERSION}; use install(target="claude-code") '
        "or install_claude_code() instead.",
        DeprecationWarning,
        stacklevel=2,
    )
    return install_claude_code().message


def uninstall_mcp() -> str:
    """Backward-compat wrapper kept from v0.4.x.

    Returns the message from :func:`uninstall_claude_code`.

    .. deprecated::
        Scheduled for removal in pm-server v1.0.0 (PMSERV-055). Use
        ``uninstall(target="claude-code")`` or
        :func:`uninstall_claude_code` instead.
    """
    warnings.warn(
        "uninstall_mcp() is deprecated and will be removed in pm-server "
        f'v{_LEGACY_WRAPPER_REMOVAL_VERSION}; use uninstall(target="claude-code") '
        "or uninstall_claude_code() instead.",
        DeprecationWarning,
        stacklevel=2,
    )
    return uninstall_claude_code().message


# --- pm-agent migration ---------------------------------------------------


def migrate_from_pm_agent():
    """pm-agent から pm-server への移行。"""
    claude_path = shutil.which("claude")
    if claude_path is None:
        print("✗ 'claude' command not found. Install Claude Code first.")
        return

    # 1. 旧 pm-agent の MCP 登録を解除
    try:
        subprocess.run(
            [claude_path, "mcp", "remove", "pm-agent", "--scope", "user"],
            check=True,
            capture_output=True,
            text=True,
            timeout=10,
        )
        print("✓ Old pm-agent MCP registration removed")
    except subprocess.CalledProcessError:
        print("  pm-agent was not registered (skipping)")

    # 2. 新 pm-server を登録（非推奨ラッパーではなく per-host 関数を直接呼ぶ:
    #    PMSERV-055 で install_mcp() に DeprecationWarning を付けたため）
    install_claude_code()

    # 3. registry チェック
    registry_path = Path.home() / ".pm" / "registry.yaml"
    if registry_path.exists():
        print(f"✓ Registry at {registry_path} is intact")
    else:
        print("⚠ Registry not found at ~/.pm/registry.yaml")

    # 4. CLAUDE.md 内の pm-agent 言及を警告
    if registry_path.exists():
        import yaml

        data = yaml.safe_load(registry_path.read_text()) or {}
        projects = data.get("projects", [])
        for proj in projects:
            proj_path = proj.get("path", "")
            claude_md = Path(proj_path) / "CLAUDE.md"
            if claude_md.exists():
                content = claude_md.read_text()
                content_lower = content.lower()
                has_ref = any(kw in content_lower for kw in ("pm-agent", "pm_agent", "pm agent"))
                if has_ref:
                    print(f"⚠ {claude_md} contains 'pm-agent' references — please update manually")

    print("\n✓ Migration complete. Restart Claude Code to activate.")


# --- Phase-3 rebrand migration: pm-server identity -> pmlens ---------------
#
# PMSERV-137 / ADR-034. The user-facing namespace flip (FastMCP name + the
# `pm-server` registration KEY -> `pmlens`) lands in step 6; this updater is
# built and tested BEFORE the flip and goes live with it. It moves an existing
# install from the legacy identity to the new one across three surfaces —
# Claude Code MCP key, Codex config table, and settings.json auto-approve
# permissions — on the SAME structured framework as install()/_safe_call/
# InstallSummary/dry_run (NOT cloned from the print-based migrate_from_pm_agent).
# Every mutating path is dry-run previewable, writes a timestamped backup, and is
# idempotent/re-runnable. The settings.json perm rewrite is ADDITIVE (the legacy
# entries are kept) so it is harmless and reversible.

#: Legacy vs new MCP registration key (server-name) and settings.json perm prefix.
_OLD_MCP_KEY = "pm-server"
_NEW_MCP_KEY = "pmlens"
_LEGACY_PERM_PREFIX = "mcp__pm-server__"
_NEW_PERM_PREFIX = "mcp__pmlens__"

#: Result target labels for the migrate surfaces.
_SETTINGS_TARGET = "claude-code-settings"


def migrate_claude_code(*, dry_run: bool = False) -> InstallResult:
    """Re-register the Claude Code MCP server under the new ``pmlens`` key.

    Probes for the legacy ``pm-server`` registration; if present, registers
    ``pmlens`` (user scope, mirroring :func:`install_claude_code`'s env
    propagation) and removes the old key. Idempotent: a machine with no
    legacy registration yields ``status="skipped"``.

    SCOPE NOTE: ``claude mcp`` add/remove/get are driven at ``--scope user``
    (the framework-wide convention). A ``pm-server`` registered at *project*
    or *local* scope is NOT enumerable here and is surfaced as an explicit
    manual-removal instruction in the message rather than silently reported
    clean (ADR-034 residual risk: SCOPE LEAK).
    """
    claude_path = shutil.which("claude")
    if claude_path is None:
        return InstallResult(
            target="claude-code",
            status="skipped",
            message="claude command not found. Install Claude Code first.",
            is_dry_run=dry_run,
        )

    probe = subprocess.run(
        [claude_path, "mcp", "get", _OLD_MCP_KEY],
        capture_output=True,
        text=True,
        timeout=10,
    )
    if probe.returncode != 0:
        return InstallResult(
            target="claude-code",
            status="skipped",
            message=(
                f"{_OLD_MCP_KEY} not registered in Claude Code (user scope) — nothing to migrate"
            ),
            is_dry_run=dry_run,
        )

    scope_note = (
        f" If {_OLD_MCP_KEY} is also registered at project or local scope, remove it "
        f"manually with `claude mcp remove {_OLD_MCP_KEY} --scope project|local`."
    )

    if dry_run:
        return InstallResult(
            target="claude-code",
            status="installed",
            message=(
                f"would register {_NEW_MCP_KEY} and remove {_OLD_MCP_KEY} (user scope)."
                + scope_note
            ),
            is_dry_run=True,
        )

    pm_server_path = shutil.which("pmlens") or shutil.which("pm-server")
    if pm_server_path is None:
        return InstallResult(
            target="claude-code",
            status="failed",
            message="pm-server command not found in PATH",
            is_dry_run=dry_run,
        )

    add_cmd: list[str] = [
        claude_path,
        "mcp",
        "add",
        "--transport",
        "stdio",
        "--scope",
        "user",
    ]
    if _lens_mode_active():
        add_cmd.extend(["--env", "PM_LENS=1"])
    if _desktop_write_mode_active():
        add_cmd.extend(["--env", "PM_DESKTOP_WRITE=1"])
    add_cmd.extend([_NEW_MCP_KEY, "--", pm_server_path, "serve"])

    add = subprocess.run(add_cmd, capture_output=True, text=True, timeout=10)
    if add.returncode != 0:
        return InstallResult(
            target="claude-code",
            status="failed",
            message=f"failed to register {_NEW_MCP_KEY}: {add.stderr}",
            is_dry_run=dry_run,
        )

    remove = subprocess.run(
        [claude_path, "mcp", "remove", _OLD_MCP_KEY, "--scope", "user"],
        capture_output=True,
        text=True,
        timeout=10,
    )
    if remove.returncode != 0:
        return InstallResult(
            target="claude-code",
            status="failed",
            message=(
                f"registered {_NEW_MCP_KEY} but failed to remove {_OLD_MCP_KEY}: {remove.stderr}"
            ),
            is_dry_run=dry_run,
        )

    return InstallResult(
        target="claude-code",
        status="installed",
        message=(
            f"migrated Claude Code: {_OLD_MCP_KEY} -> {_NEW_MCP_KEY} (user scope). "
            "Restart Claude Code to activate." + scope_note
        ),
        is_dry_run=dry_run,
    )


def migrate_codex(*, dry_run: bool = False) -> InstallResult:
    """Re-key the Codex ``[mcp_servers.pm-server]`` table to ``[mcp_servers.pmlens]``.

    Deep-copies the ENTIRE legacy table — including user-authored
    ``[mcp_servers.pm-server.tools.*]`` and ``env`` sub-tables — to the new
    key, then deletes the old key. The deep-copy is load-bearing: tomlkit
    containers hold live references, so a plain ``servers["pmlens"] =
    servers["pm-server"]`` assignment would alias the same object and corrupt
    (or orphan) the sub-tables when the old key is deleted. Routing through
    ``uninstall_codex`` + ``install_codex`` is also wrong — uninstall preserves
    user sub-tables UNDER the old key and install builds a fresh table without
    them (silent config loss). Idempotent + timestamped backup.
    """
    config_path = _codex_config_path()
    if not config_path.exists():
        return InstallResult(
            target="codex",
            status="skipped",
            message="~/.codex/config.toml not found — Codex CLI not installed",
            is_dry_run=dry_run,
        )

    doc = tomlkit.parse(config_path.read_text(encoding="utf-8"))
    servers = doc.get("mcp_servers")
    if not servers or _OLD_MCP_KEY not in servers:
        return InstallResult(
            target="codex",
            status="skipped",
            message=f"{_OLD_MCP_KEY} not registered in Codex — nothing to migrate",
            is_dry_run=dry_run,
        )
    if _NEW_MCP_KEY in servers:
        return InstallResult(
            target="codex",
            status="already_registered",
            message=f"{_NEW_MCP_KEY} already registered in Codex — migration already applied",
            is_dry_run=dry_run,
        )

    if dry_run:
        return InstallResult(
            target="codex",
            status="installed",
            message=(
                f"would deep-copy [mcp_servers.{_OLD_MCP_KEY}] (incl. tools.*/env "
                f"sub-tables) to [mcp_servers.{_NEW_MCP_KEY}] then delete the old key. "
                "Would back up to ~/.codex/config.toml.bak.<ts> before write."
            ),
            backup_path=None,
            is_dry_run=True,
        )

    backup_path = _backup_codex_config(config_path)
    # Deep-copy so user sub-tables survive byte-for-byte under the new key; a
    # plain move would alias live tomlkit refs and orphan them on delete.
    servers[_NEW_MCP_KEY] = copy.deepcopy(servers[_OLD_MCP_KEY])
    del servers[_OLD_MCP_KEY]
    _atomic_write_toml(config_path, doc)

    return InstallResult(
        target="codex",
        status="installed",
        message=(
            f"migrated Codex: [mcp_servers.{_OLD_MCP_KEY}] -> "
            f"[mcp_servers.{_NEW_MCP_KEY}] (sub-tables preserved). Backup at {backup_path}."
        ),
        backup_path=str(backup_path),
        is_dry_run=dry_run,
    )


def migrate_settings_perms(*, dry_run: bool = False) -> InstallResult:
    """Additively rewrite ``mcp__pm-server__*`` auto-approve perms to ``mcp__pmlens__*``.

    Walks ``permissions.{allow,ask,deny}`` in ``~/.claude/settings.json`` and,
    for every legacy ``mcp__pm-server__<tool>`` entry, APPENDS the
    ``mcp__pmlens__<tool>`` twin if it is missing — the legacy entry is KEPT.
    This is the fix for the one silent-breakage surface: after the namespace
    flip, auto-approved tools would otherwise revert to prompting. Additive
    means idempotent, harmless, and reversible (a downgrade still matches the
    retained old entries). Timestamped backup before write.
    """
    from . import hooks

    path = hooks._settings_path()
    settings = hooks._load_settings(path)
    perms = settings.get("permissions")
    if not isinstance(perms, dict):
        return InstallResult(
            target=_SETTINGS_TARGET,
            status="skipped",
            message="no permissions block in settings.json — nothing to migrate",
            is_dry_run=dry_run,
        )

    additions: dict[str, list[str]] = {}
    total = 0
    for list_name in ("allow", "ask", "deny"):
        entries = perms.get(list_name)
        if not isinstance(entries, list):
            continue
        existing = {e for e in entries if isinstance(e, str)}
        new_entries: list[str] = []
        for entry in entries:
            if isinstance(entry, str) and entry.startswith(_LEGACY_PERM_PREFIX):
                twin = _NEW_PERM_PREFIX + entry[len(_LEGACY_PERM_PREFIX) :]
                if twin not in existing and twin not in new_entries:
                    new_entries.append(twin)
        if new_entries:
            additions[list_name] = new_entries
            total += len(new_entries)

    if total == 0:
        return InstallResult(
            target=_SETTINGS_TARGET,
            status="skipped",
            message="no mcp__pm-server__* permissions to migrate",
            is_dry_run=dry_run,
        )

    if dry_run:
        return InstallResult(
            target=_SETTINGS_TARGET,
            status="installed",
            message=(
                f"would additively add {total} mcp__pmlens__* permission(s) "
                "(keeping the existing mcp__pm-server__* entries) and back up "
                "settings.json first."
            ),
            is_dry_run=True,
        )

    backup = _timestamped_backup(path)
    for list_name, new_entries in additions.items():
        perms[list_name].extend(new_entries)
    hooks._save_settings(path, settings)

    return InstallResult(
        target=_SETTINGS_TARGET,
        status="installed",
        message=(
            f"added {total} mcp__pmlens__* permission(s) additively "
            f"(mcp__pm-server__* kept). Backup at {backup}."
        ),
        backup_path=str(backup),
        is_dry_run=dry_run,
    )


def migrate_to_pmlens(*, dry_run: bool = False) -> InstallSummary:
    """Orchestrate the full pm-server -> pmlens migration across every surface.

    Runs the Claude Code re-registration, the Codex table re-key, and the
    settings.json additive perm rewrite, each isolated via :func:`_safe_call`
    so one surface's failure never aborts the others (ADR-007 case C). Returns
    an :class:`InstallSummary`; ``dry_run`` previews all three without writing.
    """
    results = [
        _safe_call(lambda: migrate_claude_code(dry_run=dry_run), "claude-code"),
        _safe_call(lambda: migrate_codex(dry_run=dry_run), "codex"),
        _safe_call(lambda: migrate_settings_perms(dry_run=dry_run), _SETTINGS_TARGET),
    ]
    return InstallSummary(results=results)


def legacy_pm_server_awareness(*, identity_is_pmlens: bool) -> dict | None:
    """Read-only awareness probe for the pm-server -> pmlens cutover (ADR-034).

    Returns a banner dict when THIS build runs the new ``pmlens`` identity
    (``identity_is_pmlens=True`` — true only after the step-6 FastMCP-name flip)
    AND the user still has legacy ``pm-server`` config: ``mcp__pm-server__*``
    permissions in ``~/.claude/settings.json`` and/or a ``[mcp_servers.pm-server]``
    table in ``~/.codex/config.toml``. The banner carries the count of permission
    entries that ``migrate-from-pm-server`` would rewrite, so the user sees the
    blast radius before running it. Returns ``None`` otherwise.

    Reads config files ONLY — it never spawns ``claude mcp get`` nor shells to
    git, honouring the read-path RO invariant (ADR-028). Fully defensive: any
    parse/IO error is treated as "no legacy config" so a read tool never raises.
    """
    if not identity_is_pmlens:
        return None

    from . import hooks

    perm_entries = 0
    try:
        settings = hooks._load_settings(hooks._settings_path())
        perms = settings.get("permissions")
        if isinstance(perms, dict):
            for list_name in ("allow", "ask", "deny"):
                entries = perms.get(list_name)
                if isinstance(entries, list):
                    perm_entries += sum(
                        1
                        for e in entries
                        if isinstance(e, str) and e.startswith(_LEGACY_PERM_PREFIX)
                    )
    except Exception:
        perm_entries = 0

    codex_legacy = False
    try:
        config_path = _codex_config_path()
        if config_path.exists():
            import tomllib

            doc = tomllib.loads(config_path.read_text(encoding="utf-8"))
            servers = doc.get("mcp_servers")
            codex_legacy = isinstance(servers, dict) and _OLD_MCP_KEY in servers
    except Exception:
        codex_legacy = False

    if perm_entries == 0 and not codex_legacy:
        return None

    return {
        "message": (
            "PM Server is now PM Lens. Run `pmlens migrate-from-pm-server` to "
            "re-register the MCP server and migrate your settings."
        ),
        "perm_entries": perm_entries,
        "codex_legacy": codex_legacy,
    }
