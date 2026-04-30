"""Claude Code hooks for PM Server lifecycle enforcement.

Provides PostToolUse hook that injects PM reminders after git commits,
and functions to install/uninstall hooks in Claude Code settings.
"""

from __future__ import annotations

import json
import shutil
import sys
from pathlib import Path

# Hook command that Claude Code will call
_HOOK_COMMAND_PREFIX = "pm-server hook"

# Markers to identify pm-server hooks in settings.json
_PM_HOOK_MARKER = "pm-server"


def _settings_path() -> Path:
    """Return the global Claude Code settings path."""
    return Path.home() / ".claude" / "settings.json"


def _load_settings(path: Path) -> dict:
    """Load Claude Code settings, returning empty dict if missing."""
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return {}


def _save_settings(path: Path, settings: dict) -> None:
    """Write settings back, preserving all existing keys."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(settings, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )


def _pm_server_command() -> str:
    """Return the full path to pm-server, or bare name as fallback."""
    return shutil.which("pm-server") or "pm-server"


def _build_hook_config() -> dict:
    """Build the hook configuration for pm-server."""
    cmd = _pm_server_command()
    return {
        "hooks": {
            "PostToolUse": [
                {
                    "matcher": "Bash",
                    "hooks": [
                        {
                            "type": "command",
                            "command": f"{cmd} hook post-tool-use",
                        },
                    ],
                },
            ],
        },
    }


def _is_pm_hook(hook_group: dict) -> bool:
    """Check if a hook group belongs to pm-server."""
    for hook in hook_group.get("hooks", []):
        cmd = hook.get("command", "")
        if _PM_HOOK_MARKER in cmd and "hook" in cmd:
            return True
    return False


# ─── Hook status ──────────────────────────────────


def get_hooks_status() -> dict:
    """Check if pm-server hooks are installed.

    Returns:
        dict with keys: installed (bool), path (str)
    """
    path = _settings_path()
    settings = _load_settings(path)
    hooks = settings.get("hooks", {})
    post_tool_use = hooks.get("PostToolUse", [])

    installed = any(_is_pm_hook(group) for group in post_tool_use)
    return {"installed": installed, "path": str(path)}


# ─── Hook installation ────────────────────────────


def install_hooks() -> str:
    """Install pm-server hooks into Claude Code settings.

    Safely merges into existing hooks without overwriting user hooks.

    Returns:
        Status message.
    """
    path = _settings_path()
    settings = _load_settings(path)

    # Ensure hooks structure exists
    if "hooks" not in settings:
        settings["hooks"] = {}
    if "PostToolUse" not in settings["hooks"]:
        settings["hooks"]["PostToolUse"] = []

    post_tool_use = settings["hooks"]["PostToolUse"]

    # Check if already installed
    if any(_is_pm_hook(group) for group in post_tool_use):
        return "pm-server hooks already installed (skipped)"

    # Append pm-server hook group
    config = _build_hook_config()
    post_tool_use.extend(config["hooks"]["PostToolUse"])

    _save_settings(path, settings)
    return "pm-server hooks installed in Claude Code settings"


def uninstall_hooks() -> str:
    """Remove pm-server hooks from Claude Code settings.

    Returns:
        Status message.
    """
    path = _settings_path()
    settings = _load_settings(path)

    hooks = settings.get("hooks", {})
    post_tool_use = hooks.get("PostToolUse", [])

    if not post_tool_use:
        return "no pm-server hooks found (skipped)"

    # Filter out pm-server hooks, keep everything else
    remaining = [group for group in post_tool_use if not _is_pm_hook(group)]

    if len(remaining) == len(post_tool_use):
        return "no pm-server hooks found (skipped)"

    settings["hooks"]["PostToolUse"] = remaining
    # Clean up empty structures
    if not remaining:
        del settings["hooks"]["PostToolUse"]
    if not settings["hooks"]:
        del settings["hooks"]

    _save_settings(path, settings)
    return "pm-server hooks removed from Claude Code settings"


# ─── Hook handler ─────────────────────────────────


def handle_post_tool_use() -> None:
    """Handle PostToolUse hook events from Claude Code.

    Reads JSON from stdin, checks if the command was a git commit,
    and outputs additionalContext with PM reminders if applicable.
    Exit 0 with no output for non-matching commands.
    """
    try:
        data = json.load(sys.stdin)
    except (json.JSONDecodeError, EOFError):
        return

    tool_input = data.get("tool_input", {})
    command = tool_input.get("command", "")
    cwd = data.get("cwd", "")

    # Only act on git commit commands
    if "git commit" not in command:
        return

    # Only act on projects with PM Server
    pm_path = Path(cwd) / ".pm"
    if not pm_path.exists():
        return

    reminder = _build_commit_reminder(pm_path)
    if reminder:
        json.dump({"additionalContext": reminder}, sys.stdout)


def _build_commit_reminder(pm_path: Path) -> str:
    """Build a contextual PM reminder after git commit."""
    from .models import TaskStatus
    from .storage import load_tasks

    tasks = load_tasks(pm_path)
    active = [t for t in tasks if t.status == TaskStatus.IN_PROGRESS]

    lines = ["[PM Server] Git commit completed. Please execute:"]

    if active:
        task_ids = ", ".join(t.id for t in active)
        lines.append(f"1. pm_update_task — mark completed tasks as done (active: {task_ids})")
    else:
        lines.append("1. pm_update_task — update task status if needed")

    lines.append("2. pm_log — record what was accomplished")
    lines.append("3. pm_next — check recommended next tasks")

    return "\n".join(lines)
