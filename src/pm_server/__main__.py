"""CLI entry point for PM Server."""

from __future__ import annotations

import click

from . import __version__


@click.group()
@click.version_option(version=__version__, prog_name="pm-server")
def cli():
    """PM Server — Claude Code Project Management."""


_TARGET_CHOICES = ["claude-code", "codex", "auto", "all"]


def _print_install_summary(summary) -> None:
    """Render an InstallSummary as one ``prefix target: message`` line per host.

    ``"✗"`` is used only for ``status == "failed"``; every other status
    (``installed``, ``uninstalled``, ``already_registered``, ``skipped``)
    is treated as success and rendered with ``"✓"``. Dry-run results
    are tagged with ``[dry-run]`` between the prefix and the target.
    """
    if not summary.results:
        click.echo("✗ No hosts processed.")
        return
    for r in summary.results:
        prefix = "✗" if r.status == "failed" else "✓"
        dry_tag = "[dry-run] " if r.is_dry_run else ""
        click.echo(f"{prefix} {dry_tag}{r.target}: {r.message}")


def _print_inject_summary(summary) -> None:
    """Render an InjectSummary as one ``prefix target_file: message`` line per host.

    Per PMSERV-044 cross-check R6: this is the **single source of truth**
    for ``[dry-run]`` and backup-path presentation. ``InjectResult.message``
    intentionally does not embed those — they are layered here so the
    one-line-per-host invariant holds even when both CLI and Python API
    consume the same data class.
    """
    if not summary.results:
        click.echo("✗ No hosts processed.")
        return

    # Surface a fallback warning ahead of the per-host lines so the user
    # sees it before scrolling past success indicators.
    if summary.detection_source == "fallback":
        click.echo(
            "⚠ No host detected via filesystem / marker / env. "
            "Defaulted to claude-code only — pass --target=codex if running "
            "under Codex CLI."
        )

    for r in summary.results:
        prefix = "✗" if r.status == "failed" else "✓"
        dry_tag = "[dry-run] " if r.is_dry_run else ""
        click.echo(f"{prefix} {dry_tag}{r.target_file}: {r.message}")
        if r.backup_path:
            click.echo(f"    backup: {r.backup_path}")


@cli.command()
@click.option(
    "--target",
    "-t",
    type=click.Choice(_TARGET_CHOICES),
    default="claude-code",
    show_default=True,
    help="MCP host to register pm-server with. 'auto'/'all' process every known host.",
)
@click.option(
    "--dry-run",
    is_flag=True,
    default=False,
    help="Show what would happen without making changes.",
)
def install(target: str, dry_run: bool):
    """Register PM Server as an MCP server in the chosen host(s)."""
    from . import installer

    summary = installer.install(target=target, dry_run=dry_run)
    _print_install_summary(summary)
    if any(r.status == "failed" for r in summary.results):
        raise click.exceptions.Exit(1)


@cli.command()
@click.option(
    "--target",
    "-t",
    type=click.Choice(_TARGET_CHOICES),
    default="claude-code",
    show_default=True,
    help="MCP host to remove pm-server from. 'auto'/'all' process every known host.",
)
@click.option(
    "--dry-run",
    is_flag=True,
    default=False,
    help="Show what would happen without making changes.",
)
def uninstall(target: str, dry_run: bool):
    """Remove PM Server from MCP host(s)."""
    from . import installer

    summary = installer.uninstall(target=target, dry_run=dry_run)
    _print_install_summary(summary)
    if any(r.status == "failed" for r in summary.results):
        raise click.exceptions.Exit(1)


@cli.command()
def serve():
    """Start the MCP server (called by Claude Code via stdio)."""
    from .server import mcp

    mcp.run(transport="stdio")


@cli.command()
@click.argument("scan_path", default=".")
def discover(scan_path: str):
    """Scan for projects and register them.

    Mirrors the ``pm_discover`` MCP tool (PMSERV-066): a single registry
    transaction batches every new entry so the CLI does not differ in
    locking semantics from the in-process MCP path.
    """
    from pathlib import Path

    from .discovery import discover_projects
    from .models import RegistryEntry
    from .storage import (
        GLOBAL_PM_DIR,
        _yaml_transaction,
        load_registry,
        save_registry,
    )

    found = discover_projects(Path(scan_path))
    if not found:
        click.echo("No projects with .pm/ found.")
        return

    newly_registered: list[dict] = []
    with _yaml_transaction(GLOBAL_PM_DIR, "registry"):
        registry = load_registry()
        registered_paths = {p.path for p in registry.projects}
        for proj in found:
            resolved = str(Path(proj["path"]).resolve())
            if resolved in registered_paths:
                continue
            registry.projects.append(RegistryEntry(path=resolved, name=proj["name"]))
            registered_paths.add(resolved)
            newly_registered.append(proj)
        if newly_registered:
            save_registry(registry)

    for proj in newly_registered:
        click.echo(f"  ✓ {proj['name']} ({proj['path']})")

    click.echo(f"\n{len(newly_registered)} project(s) registered (out of {len(found)} found).")


@cli.command()
def status():
    """Show current project status."""
    from .server import pm_status
    from .utils import resolve_project_path

    try:
        resolve_project_path()
    except Exception as e:
        click.echo(f"Error: {e}")
        return

    result = pm_status()
    proj = result["project"]
    tasks = result["tasks"]

    click.echo(f"\n  {proj['display_name'] or proj['name']} ({proj['status']})")
    click.echo(
        f"  Tasks: {tasks['total']} total — "
        f"todo:{tasks.get('todo', 0)} in_progress:{tasks.get('in_progress', 0)} "
        f"done:{tasks.get('done', 0)} blocked:{tasks.get('blocked', 0)}"
    )

    if result["blockers"]:
        click.echo(f"\n  ⚠ {len(result['blockers'])} blocker(s):")
        for b in result["blockers"]:
            click.echo(f"    {b['id']}: {b['title']}")
    click.echo()


@cli.command()
def migrate():
    """pm-agent からの移行。旧 MCP 登録を解除し pm-server として再登録。"""
    from .installer import migrate_from_pm_agent

    migrate_from_pm_agent()


@cli.command("context-inject")
def context_inject_cmd():
    """Print session context to stdout for Claude Code injection.

    Outputs a context block with previous session summary,
    in-progress task memories, recent decisions, and recent memories.
    Designed for future SessionStart hook integration.
    """
    from .context import inject_context

    inject_context()


@cli.group()
def hook():
    """Manage Claude Code hooks for PM Server."""


@hook.command("post-tool-use")
def hook_post_tool_use():
    """Handle PostToolUse events (called by Claude Code)."""
    from .hooks import handle_post_tool_use

    handle_post_tool_use()


@cli.command("install-hooks")
def install_hooks_cmd():
    """Install PM Server hooks into Claude Code settings."""
    from .hooks import install_hooks

    msg = install_hooks()
    prefix = "✓" if "installed" in msg or "skipped" in msg else "✗"
    click.echo(f"{prefix} {msg}")


@cli.command("uninstall-hooks")
def uninstall_hooks_cmd():
    """Remove PM Server hooks from Claude Code settings."""
    from .hooks import uninstall_hooks

    msg = uninstall_hooks()
    prefix = "✓" if "removed" in msg or "skipped" in msg else "✗"
    click.echo(f"{prefix} {msg}")


@cli.command("update-claudemd")
@click.option("--all", "all_projects", is_flag=True, help="Update all registered projects.")
def update_claudemd_cmd(all_projects: bool):
    """Update PM Server rules in CLAUDE.md.

    Without --all: updates current project only.
    With --all: updates all registered projects.

    .. deprecated:: 0.6.0
        Backward-compat alias. Prefer ``pm-server update-rules`` which
        supports AGENTS.md (Codex CLI) in addition to CLAUDE.md.
        Output format is byte-stable with v0.4.x for this command.
    """
    from pathlib import Path

    from .claudemd import update_claudemd

    if all_projects:
        from .storage import load_registry

        registry = load_registry()
        if not registry.projects:
            click.echo("No registered projects found.")
            return

        for entry in registry.projects:
            root = Path(entry.path)
            if root.exists():
                result = update_claudemd(root)
                click.echo(f"  {entry.name}: {result}")
            else:
                click.echo(f"  {entry.name}: path not found (skipped)")
    else:
        from .utils import resolve_project_path

        try:
            root = resolve_project_path()
            result = update_claudemd(root)
            click.echo(f"  {result}")
        except Exception as e:
            click.echo(f"Error: {e}")


@cli.command("update-rules")
@click.option(
    "--target",
    "-t",
    type=click.Choice(_TARGET_CHOICES),
    default="auto",
    show_default=True,
    help=(
        "Which host's rule file to update. 'auto' detects via "
        "filesystem/marker/env; 'all' forces every known host."
    ),
)
@click.option(
    "--dry-run",
    is_flag=True,
    default=False,
    help="Show what would happen without making changes.",
)
@click.option(
    "--all",
    "all_projects",
    is_flag=True,
    default=False,
    help="Apply to every registered project (target/dry_run apply per-project).",
)
def update_rules_cmd(target: str, dry_run: bool, all_projects: bool):
    """Inject PM Server rules into CLAUDE.md and/or AGENTS.md.

    Compared to ``update-claudemd``: also handles AGENTS.md for Codex
    CLI (ADR-008). Default ``target=auto`` detects which hosts are
    installed on this machine and updates only those rule files.
    """
    from pathlib import Path

    from . import rules
    from .utils import resolve_project_path

    any_failed = False

    if all_projects:
        from .storage import load_registry

        registry = load_registry()
        if not registry.projects:
            click.echo("No registered projects found.")
            return

        for entry in registry.projects:
            root = Path(entry.path)
            if not root.exists():
                click.echo(f"  {entry.name}: path not found (skipped)")
                continue
            click.echo(f"\n{entry.name}:")
            summary = rules.inject_pm_rules(root, target=target, dry_run=dry_run)
            _print_inject_summary(summary)
            any_failed = any_failed or any(r.status == "failed" for r in summary.results)
    else:
        try:
            root = resolve_project_path()
        except Exception as e:
            click.echo(f"Error: {e}")
            raise click.exceptions.Exit(1) from e
        summary = rules.inject_pm_rules(root, target=target, dry_run=dry_run)
        _print_inject_summary(summary)
        any_failed = any(r.status == "failed" for r in summary.results)

    if any_failed:
        raise click.exceptions.Exit(1)


if __name__ == "__main__":
    cli()
