"""MCP auto-installer for pm-server.

Registers (or unregisters) pm-server as an MCP server in supported hosts.

Hosts:
    - Claude Code: registers via ``claude mcp add`` (user scope).
    - Codex CLI: edits ``~/.codex/config.toml`` via tomlkit with
      timestamped backup, idempotent field-level updates, and atomic
      write. Sub-tables under ``[mcp_servers.pm-server.*]`` (such as
      per-tool ``approval_mode`` customizations) are preserved.

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
    - ``migrate_from_pm_agent()``: unchanged migration helper.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

import tomlkit

from .utils import (
    _atomic_write_text,
    _codex_config_path,
    _resolve_targets,
    _timestamped_backup,
)


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
    status: str
    message: str
    backup_path: str | None = None
    is_dry_run: bool = False
    lens_mode: bool = False


@dataclass(frozen=True)
class InstallSummary:
    """Aggregated results across hosts processed by ``install``/``uninstall``."""

    results: list[InstallResult] = field(default_factory=list)

    @property
    def overall_status(self) -> str:
        """Aggregate status across hosts.

        Priority order:
            failed > installed > uninstalled > already_registered > skipped.
        """
        for level in ("failed", "installed", "uninstalled", "already_registered", "skipped"):
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

    Idempotent: if ``claude mcp get pm-server`` already succeeds, the
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
    pm_server_path = shutil.which("pm-server")
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
        [claude_path, "mcp", "get", "pm-server"],
        capture_output=True,
        text=True,
        timeout=10,
    )
    if result.returncode == 0:
        return InstallResult(
            target="claude-code",
            status="already_registered",
            message="PM Server is already registered in Claude Code",
            is_dry_run=dry_run,
            lens_mode=lens_mode,
        )

    if dry_run:
        suffix = " in Lens (read-only) mode" if lens_mode else ""
        return InstallResult(
            target="claude-code",
            status="installed",
            message=f"would register PM Server in Claude Code (user scope){suffix}.",
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
    add_cmd.extend(["pm-server", "--", pm_server_path, "serve"])

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
                f"PM Server registered in Claude Code (user scope){suffix}. "
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

    Performs a pre-check via ``claude mcp get pm-server`` so the
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
        [claude_path, "mcp", "get", "pm-server"],
        capture_output=True,
        text=True,
        timeout=10,
    )
    if pre_check.returncode != 0:
        return InstallResult(
            target="claude-code",
            status="skipped",
            message="PM Server not registered in Claude Code",
            is_dry_run=dry_run,
        )

    if dry_run:
        return InstallResult(
            target="claude-code",
            status="uninstalled",
            message="would unregister PM Server from Claude Code",
            is_dry_run=True,
        )

    result = subprocess.run(
        [claude_path, "mcp", "remove", "pm-server", "--scope", "user"],
        capture_output=True,
        text=True,
        timeout=10,
    )
    if result.returncode == 0:
        return InstallResult(
            target="claude-code",
            status="uninstalled",
            message="PM Server unregistered from Claude Code",
        )

    return InstallResult(
        target="claude-code",
        status="failed",
        message=f"Removal failed: {result.stderr}",
    )


# --- Host: Codex CLI ------------------------------------------------------


def _resolve_pm_server_path() -> Path:
    """Resolve the absolute pm-server binary path for sandbox-safe registration.

    Codex sandboxes restrict PATH inheritance, so a bare ``pm-server``
    name does not resolve. The canonical location is the binary in the
    same directory as the running Python interpreter (works for pip /
    pipx / pyenv / venv installations). Falls back to ``shutil.which``
    only if the canonical location is missing.

    Raises:
        FileNotFoundError: if pm-server cannot be located.
    """
    candidate = Path(sys.executable).resolve().parent / "pm-server"
    if candidate.exists():
        return candidate
    fallback = shutil.which("pm-server")
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


def install_codex(*, dry_run: bool = False) -> InstallResult:
    """Register pm-server as a Codex CLI MCP server.

    Edits ``~/.codex/config.toml`` via tomlkit:
        - Detect: skip if config.toml does not exist (no side effect on
          non-Codex installations).
        - Resolve: absolute pm-server path via :func:`_resolve_pm_server_path`.
        - Backup: timestamped copy under ``~/.codex/config.toml.bak.<ts>``.
        - Update: field-level edits to ``[mcp_servers.pm-server]`` so any
          user-defined sub-tables (such as per-tool ``approval_mode``
          customizations) and surrounding comments are preserved.
        - Atomic write: tempfile + os.replace.

    Args:
        dry_run: When ``True``, detection, parsing, path resolution, and
            in-memory mutation still occur (so the predicted status is
            accurate), but ``_backup_codex_config`` and
            ``_atomic_write_toml`` are skipped — the config file on disk
            is untouched and ``backup_path`` is ``None``.

    Returns:
        ``InstallResult`` with ``target="codex"``. On install/update,
        ``backup_path`` points at the saved-aside copy (``None`` for
        dry-run).
    """
    lens_mode = _lens_mode_active()
    desktop_write_mode = _desktop_write_mode_active()
    config_path = _codex_config_path()
    if not config_path.exists():
        return InstallResult(
            target="codex",
            status="skipped",
            message="~/.codex/config.toml not found — Codex CLI not installed",
            is_dry_run=dry_run,
            lens_mode=lens_mode,
        )

    pm_server_path = _resolve_pm_server_path()

    doc = tomlkit.parse(config_path.read_text(encoding="utf-8"))

    # Early return if already registered with matching command AND matching
    # PM_LENS/PM_DESKTOP_WRITE env presence — no mutation, no backup needed.
    # PMSERV-087 + PMSERV-100: we compare both env flags, so flipping either
    # one across reinstalls re-registers cleanly without stale env left over.
    if "mcp_servers" in doc and "pm-server" in doc["mcp_servers"]:
        existing = doc["mcp_servers"]["pm-server"]
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
                target="codex",
                status="already_registered",
                message="PM Server is already registered in Codex",
                is_dry_run=dry_run,
                lens_mode=lens_mode,
            )

    if dry_run:
        # Predict the outcome without creating a backup or writing anything.
        suffix = " in Lens (read-only) mode" if lens_mode else ""
        if "mcp_servers" not in doc or "pm-server" not in doc.get("mcp_servers", {}):
            message = (
                f"would register PM Server in Codex (user scope){suffix}. "
                "Would back up to ~/.codex/config.toml.bak.<ts> before write."
            )
        else:
            message = (
                f"would update PM Server in Codex{suffix} "
                "(path or PM_LENS env changed). "
                "Would back up to ~/.codex/config.toml.bak.<ts> before write."
            )
        return InstallResult(
            target="codex",
            status="installed",
            message=message,
            backup_path=None,
            is_dry_run=True,
            lens_mode=lens_mode,
        )

    backup_path = _backup_codex_config(config_path)

    if "mcp_servers" not in doc:
        doc["mcp_servers"] = tomlkit.table()
    if "pm-server" not in doc["mcp_servers"]:
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
        doc["mcp_servers"]["pm-server"] = section
        suffix = " in Lens (read-only) mode" if lens_mode else ""
        message = (
            f"PM Server registered in Codex (user scope){suffix}. "
            f"Backup at {backup_path}. Restart Codex to activate."
        )
    else:
        section = doc["mcp_servers"]["pm-server"]
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
            f"PM Server updated in Codex{suffix} (path or PM_LENS env changed). "
            f"Backup at {backup_path}. Restart Codex to activate."
        )

    _atomic_write_toml(config_path, doc)

    return InstallResult(
        target="codex",
        status="installed",
        message=message,
        backup_path=str(backup_path),
        lens_mode=lens_mode,
    )


def uninstall_codex(*, dry_run: bool = False) -> InstallResult:
    """Remove pm-server registration from Codex CLI config.

    Removes only the top-level fields (``command``, ``args``,
    ``startup_timeout_sec``). If the user has customized sub-tables
    such as ``[mcp_servers.pm-server.tools.pm_init]``, the parent
    section is preserved with a notice in the result message —
    those customizations are left untouched and require manual
    cleanup if no longer wanted.

    Args:
        dry_run: When ``True``, detection and parsing still run so the
            predicted outcome (full removal vs sub-tables-preserved) is
            accurate, but ``_backup_codex_config`` and
            ``_atomic_write_toml`` are skipped.

    Returns:
        ``InstallResult`` with ``target="codex"``.
    """
    config_path = _codex_config_path()
    if not config_path.exists():
        return InstallResult(
            target="codex",
            status="skipped",
            message="~/.codex/config.toml not found — nothing to uninstall",
            is_dry_run=dry_run,
        )

    doc = tomlkit.parse(config_path.read_text(encoding="utf-8"))

    if "mcp_servers" not in doc or "pm-server" not in doc["mcp_servers"]:
        return InstallResult(
            target="codex",
            status="skipped",
            message="pm-server not registered in Codex",
            is_dry_run=dry_run,
        )

    if dry_run:
        # Predict whether the section would be fully removed or only
        # top-level fields stripped (sub-tables preserved).
        section = doc["mcp_servers"]["pm-server"]
        managed = ("command", "args", "startup_timeout_sec")
        residual_keys = [k for k in section.keys() if k not in managed]
        if not residual_keys:
            message = (
                "would unregister PM Server from Codex. "
                "Would back up to ~/.codex/config.toml.bak.<ts> before write."
            )
        else:
            message = (
                "would remove PM Server top-level fields from Codex. "
                "Sub-tables would be preserved — remove manually if no longer needed. "
                "Would back up to ~/.codex/config.toml.bak.<ts> before write."
            )
        return InstallResult(
            target="codex",
            status="uninstalled",
            message=message,
            backup_path=None,
            is_dry_run=True,
        )

    backup_path = _backup_codex_config(config_path)

    section = doc["mcp_servers"]["pm-server"]
    for key in ("command", "args", "startup_timeout_sec"):
        if key in section:
            del section[key]

    if not section:
        del doc["mcp_servers"]["pm-server"]
        if not doc["mcp_servers"]:
            del doc["mcp_servers"]
        message = f"PM Server unregistered from Codex. Backup at {backup_path}."
    else:
        message = (
            "PM Server top-level fields removed from Codex. "
            "Sub-tables preserved — remove manually if no longer needed. "
            f"Backup at {backup_path}."
        )

    _atomic_write_toml(config_path, doc)

    return InstallResult(
        target="codex",
        status="uninstalled",
        message=message,
        backup_path=str(backup_path),
    )


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


def install(target: str = "claude-code", *, dry_run: bool = False) -> InstallSummary:
    """Register pm-server with one or more host MCP clients.

    Args:
        target: ``"claude-code"`` (default), ``"codex"``, ``"auto"``, or
            ``"all"`` (``"auto"`` and ``"all"`` are synonyms; both run
            every known host and skip the ones that aren't installed).
        dry_run: When ``True``, propagated to every host installer; no
            side effects (subprocess execution, backups, file writes)
            occur, but read-only detection still runs so each result's
            ``status`` reflects the action that *would* have been taken.

    Returns:
        ``InstallSummary`` with one ``InstallResult`` per processed host.
    """
    results: list[InstallResult] = []
    for host in _resolve_targets(target):
        if host == "claude-code":
            results.append(_safe_call(lambda: install_claude_code(dry_run=dry_run), host))
        elif host == "codex":
            results.append(_safe_call(lambda: install_codex(dry_run=dry_run), host))
    return InstallSummary(results=results)


def uninstall(target: str = "claude-code", *, dry_run: bool = False) -> InstallSummary:
    """Remove pm-server registrations from one or more host MCP clients.

    Symmetric to :func:`install`. Same target and dry-run semantics.
    """
    results: list[InstallResult] = []
    for host in _resolve_targets(target):
        if host == "claude-code":
            results.append(_safe_call(lambda: uninstall_claude_code(dry_run=dry_run), host))
        elif host == "codex":
            results.append(_safe_call(lambda: uninstall_codex(dry_run=dry_run), host))
    return InstallSummary(results=results)


# --- Backward-compat wrappers (v0.4.x public API) -------------------------


def install_mcp() -> str:
    """Backward-compat wrapper kept from v0.4.x.

    Returns the human-readable message from :func:`install_claude_code`.
    The structured form is ``install(target="claude-code")`` which yields
    an :class:`InstallSummary`.
    """
    return install_claude_code().message


def uninstall_mcp() -> str:
    """Backward-compat wrapper kept from v0.4.x.

    Returns the message from :func:`uninstall_claude_code`.
    """
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

    # 2. 新 pm-server を登録
    install_mcp()

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
