"""CLI entry point for PM Lens."""

from __future__ import annotations

import click

from . import __version__
from .rules import RULE_TARGET_CHOICES as _RULE_TARGET_CHOICES
from .utils import TARGET_CHOICES


@click.group()
@click.version_option(version=__version__, prog_name="pmlens")
def cli():
    """PM Lens — Claude Code Project Management."""


# Derived from the host registry (PMSERV-165) rather than restated. Nothing
# used to compare the two, so the CLI could silently offer a different set of
# hosts than the Python API accepted; tests/test_utils.py now pins the parity.
_TARGET_CHOICES = list(TARGET_CHOICES)


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
        if summary.detection_source == "existing":
            click.echo("- no PM Lens section in CLAUDE.md or AGENTS.md (nothing to update)")
        else:
            click.echo("✗ No hosts processed.")
        return

    # Surface a fallback warning ahead of the per-host lines so the user
    # sees it before scrolling past success indicators.
    if summary.detection_source == "fallback":
        others = ", ".join(h for h in TARGET_CHOICES if h not in ("auto", "all", "claude-code"))
        click.echo(
            "⚠ No host detected via filesystem / marker / env. "
            f"Defaulted to claude-code only — pass --target=<{others}> "
            "if running under one of those hosts."
        )

    for r in summary.results:
        prefix = "✗" if r.status == "failed" else "⚠" if r.refused_downgrade else "✓"
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
    """Register PM Lens as an MCP server in the chosen host(s)."""
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
    """Remove PM Lens from MCP host(s)."""
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

    from .discovery import scan_projects
    from .models import RegistryEntry
    from .storage import (
        GLOBAL_PM_DIR,
        _save_registry,
        _yaml_transaction,
        load_registry,
    )

    scan = scan_projects(Path(scan_path))
    found = scan.projects

    if scan.depth_capped:
        n = len(scan.depth_capped)
        click.echo(
            f"⚠ Depth cap ({scan.max_depth} levels) skipped {n} director"
            f"{'y' if n == 1 else 'ies'}; projects deeper than the cap may be "
            f"undetected. Re-run with a scan_path closer to them, or pm_init there."
        )

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
            _save_registry(registry)

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


@cli.command("prompt-pack")
@click.option("--tag", "filter_tag", default=None, help="Only tasks carrying this tag.")
@click.option("--phase", "filter_phase", default=None, help="Only tasks in this phase id.")
@click.option(
    "--priority", "filter_priority", default=None, help="Only tasks at this priority (P0-P3)."
)
@click.option(
    "--task-id",
    "task_ids",
    multiple=True,
    help="Explicit task id (repeatable); overrides filters and includes done tasks.",
)
@click.option(
    "--format",
    "fmt",
    type=click.Choice(["md", "html"]),
    default="md",
    show_default=True,
    help="Output format.",
)
@click.option(
    "--group-by",
    "group_by",
    type=click.Choice(["none", "phase", "track"]),
    default="none",
    show_default=True,
    help="HTML diagram lane grouping.",
)
@click.option(
    "--out",
    "out_path",
    default=None,
    help="Output path (default: .pm/exports/prompt-pack-<label>.<ext>).",
)
@click.option(
    "--project", "project_path", default=None, help="Project dir (default: auto-detect from cwd)."
)
def prompt_pack_cmd(
    filter_tag: str | None,
    filter_phase: str | None,
    filter_priority: str | None,
    task_ids: tuple[str, ...],
    fmt: str,
    group_by: str,
    out_path: str | None,
    project_path: str | None,
):
    """Generate a self-contained implementation-session prompt pack.

    Turns backlog tasks into ready-to-paste session prompts ("1 task = 1
    session"); use --format html for a diagram + copy buttons. Read-only over
    the project's data — the only write is the export file.
    """
    from .prompt_pack import run_prompt_pack
    from .utils import resolve_project_path

    try:
        root = resolve_project_path(project_path)
    except Exception as e:
        click.echo(f"Error: {e}", err=True)
        raise SystemExit(1) from e

    result = run_prompt_pack(
        root / ".pm",
        filter_tag=filter_tag,
        filter_phase=filter_phase,
        filter_priority=filter_priority,
        task_ids=list(task_ids) or None,
        format=fmt,
        group_by=group_by,
        out_path=out_path,
    )

    if result["status"] == "error":
        click.echo(f"Error: {result['message']}", err=True)
        raise SystemExit(1)
    for w in result.get("warnings", []):
        click.echo(f"⚠ {w}", err=True)
    if result["task_count"] == 0:
        click.echo("No tasks matched — nothing generated.")
        return
    click.echo(f"✓ {result['task_count']} task(s) → {result['out_path']} ({result['format']})")


@cli.command()
def migrate():
    """pm-agent からの移行。旧 MCP 登録を解除し pm-server として再登録。"""
    from .installer import migrate_from_pm_agent

    migrate_from_pm_agent()


def _run_migrate_to_pmlens(dry_run: bool) -> None:
    """Shared body for the pm-server -> pmlens migration CLI commands."""
    from . import installer

    summary = installer.migrate_to_pmlens(dry_run=dry_run)
    _print_install_summary(summary)
    if any(r.status == "failed" for r in summary.results):
        raise click.exceptions.Exit(1)


@cli.command("migrate-from-pm-server")
@click.option(
    "--dry-run",
    is_flag=True,
    default=False,
    help="Preview the migration across all hosts without writing.",
)
def migrate_from_pm_server_cmd(dry_run: bool):
    """Migrate an existing pm-server install to the PM Lens (pmlens) identity.

    Re-registers the MCP server under the new ``pmlens`` key (Claude Code +
    Codex), deep-copies the Codex ``[mcp_servers.pm-server]`` table preserving
    user sub-tables, and additively rewrites ``mcp__pm-server__*`` permissions
    to ``mcp__pmlens__*`` in settings.json. The legacy ``migrate`` command
    (pm-agent -> pm-server) is unaffected.
    """
    _run_migrate_to_pmlens(dry_run)


@cli.command("upgrade")
@click.option(
    "--dry-run",
    is_flag=True,
    default=False,
    help="Preview the migration across all hosts without writing.",
)
def upgrade_cmd(dry_run: bool):
    """Alias for ``migrate-from-pm-server``."""
    _run_migrate_to_pmlens(dry_run)


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
    """Manage Claude Code hooks for PM Lens."""


@hook.command("post-tool-use")
def hook_post_tool_use():
    """Handle PostToolUse events (called by Claude Code)."""
    from .hooks import handle_post_tool_use

    handle_post_tool_use()


@cli.command("install-hooks")
def install_hooks_cmd():
    """Install PM Lens hooks into Claude Code settings."""
    from .hooks import install_hooks

    msg = install_hooks()
    prefix = "✓" if "installed" in msg or "skipped" in msg else "✗"
    click.echo(f"{prefix} {msg}")


@cli.command("uninstall-hooks")
def uninstall_hooks_cmd():
    """Remove PM Lens hooks from Claude Code settings."""
    from .hooks import uninstall_hooks

    msg = uninstall_hooks()
    prefix = "✓" if "removed" in msg or "skipped" in msg else "✗"
    click.echo(f"{prefix} {msg}")


@cli.command("update-claudemd")
@click.option(
    "--all",
    "all_projects",
    is_flag=True,
    help="Retired; use `pmlens update-rules --all` (plan) and `--apply`.",
)
def update_claudemd_cmd(all_projects: bool):
    """Update PM Lens rules in CLAUDE.md.

    Updates the current project only. ``--all`` is retired: use
    ``pmlens update-rules --all``, which shows a plan before writing.

    .. deprecated:: 0.6.0
        Backward-compat alias. Prefer ``pm-server update-rules`` which
        supports AGENTS.md (Codex CLI) in addition to CLAUDE.md.
        Output format is byte-stable with v0.4.x for this command.
    """
    from .claudemd import update_claudemd

    if all_projects:
        # Retired (ADR-055): it rewrote — or created — CLAUDE.md in every
        # registered repository at once, with no plan to review first.
        click.echo(
            "update-claudemd --all is retired: it wrote every registered repository "
            "without a plan. Run `pmlens update-rules --all` to see the plan, then "
            "`pmlens update-rules --all --apply` to write it."
        )
        raise click.exceptions.Exit(1)
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
    type=click.Choice(list(_RULE_TARGET_CHOICES)),
    default=None,
    show_default="auto; existing with --all",
    help=(
        "Which host's rule file to update. 'auto' detects via "
        "filesystem/marker/env; 'all' forces every known host; 'existing' "
        "only rewrites files that already carry the PM Lens section."
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
    help=(
        "Plan the update for every registered project. Nothing is written "
        "unless --apply is also given."
    ),
)
@click.option(
    "--apply",
    "apply_changes",
    is_flag=True,
    default=False,
    help="With --all, write the planned changes.",
)
@click.option(
    "--force",
    is_flag=True,
    default=False,
    help="Also rewrite sections that are newer than this pmlens (a downgrade).",
)
def update_rules_cmd(
    target: str | None,
    dry_run: bool,
    all_projects: bool,
    apply_changes: bool,
    force: bool,
):
    """Inject PM Lens rules into CLAUDE.md and/or AGENTS.md.

    Compared to ``update-claudemd``: also handles AGENTS.md for Codex
    CLI (ADR-008). Default ``target=auto`` detects which hosts are
    installed on this machine and updates only those rule files.

    ``--all`` rewrites files in every registered repository, many of which
    commit their rule files, so it defaults to ``--target existing`` (never
    creating a rule file) and only prints the plan until ``--apply`` is
    given (ADR-055).
    """
    from pathlib import Path

    from . import rules
    from .utils import resolve_project_path

    if apply_changes and not all_projects:
        raise click.UsageError(
            "--apply only applies to --all; without --all, update-rules writes "
            "directly (use --dry-run to preview)."
        )
    if apply_changes and dry_run:
        raise click.UsageError("--dry-run and --apply contradict each other; pass one.")

    any_failed = False

    if all_projects:
        from .storage import load_registry

        target = target or "existing"
        plan_only = dry_run or not apply_changes
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
            summary = rules.inject_pm_rules(root, target=target, dry_run=plan_only, force=force)
            _print_inject_summary(summary)
            any_failed = any_failed or any(r.status == "failed" for r in summary.results)
        if plan_only and not dry_run:
            click.echo(
                "\nPlan only — nothing was written. Re-run with --apply to write these changes."
            )
    else:
        target = target or "auto"
        try:
            root = resolve_project_path()
        except Exception as e:
            click.echo(f"Error: {e}")
            raise click.exceptions.Exit(1) from e
        summary = rules.inject_pm_rules(root, target=target, dry_run=dry_run, force=force)
        _print_inject_summary(summary)
        any_failed = any(r.status == "failed" for r in summary.results)

    if any_failed:
        raise click.exceptions.Exit(1)


if __name__ == "__main__":
    cli()
