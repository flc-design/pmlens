"""Claude Code hooks for PM Lens lifecycle enforcement.

Provides PostToolUse hook that injects PM reminders after git commits,
and functions to install/uninstall hooks in Claude Code settings.
"""

from __future__ import annotations

import json
import os
import re
import shlex
import shutil
import sys
from pathlib import Path

# Hook command that Claude Code will call. New installs emit the ``pmlens``
# binary; the legacy ``pm-server hook`` form is still RECOGNIZED on detection
# and removal via ``_PM_HOOK_COMMAND_RE`` below (dual-recognition, PMSERV-137 /
# ADR-034): detection/removal must match BOTH identities so an upgrade-in-place
# (or the migrate updater) never strands a half-renamed hook.
_HOOK_COMMAND_PREFIX = "pmlens hook"


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
    """Return the full path to the PM Lens binary, or a bare name fallback.

    Prefers the new ``pmlens`` console script and falls back to the legacy
    ``pm-server`` binary while the rename is mid-flight (the wrapper still
    ships it), so a fresh hook install resolves on machines that only have the
    old binary on PATH (PMSERV-137)."""
    return shutil.which("pmlens") or shutil.which("pm-server") or "pmlens"


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


# Precise identification of PM Lens's OWN hook invocations (ADR-053 review
# finding). The executable's basename must be ``pmlens`` or ``pm-server`` —
# bare, or the last component of a path, optionally quoted, optionally after
# ``VAR=value`` prefixes — and the very next argument must be ``hook``. A user
# command that merely CONTAINS both strings somewhere (for example
# ``.../pm-server/scripts/hooks/notify.sh``) must never match, because the
# repair path in :func:`install_hooks` removes whatever matches here.
_PM_HOOK_COMMAND_RE = re.compile(r"""(?:^|[\s/])(?:pmlens|pm-server)["']?\s+hook(?:\s|$)""")

# ``VAR=value`` words a shell strips before resolving the executable.
_ENV_ASSIGNMENT_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*=")


def _is_pm_hook_command(cmd: str) -> bool:
    """Check if a single hook command is a PM Lens hook invocation.

    Recognises both identities (legacy ``pm-server hook …`` and current
    ``pmlens hook …``), see :data:`_PM_HOOK_COMMAND_RE` for the exact rule.
    """
    return bool(_PM_HOOK_COMMAND_RE.search(cmd))


def _is_pm_hook(hook_group: dict) -> bool:
    """Check if a hook group contains at least one PM Lens hook command."""
    return any(_is_pm_hook_command(hook.get("command", "")) for hook in hook_group.get("hooks", []))


def _hook_binary_exists(command: str) -> bool:
    """Return True iff the executable named by ``command`` can be found (ADR-053).

    Only the executable word is inspected, the way a shell would find it:
    leading ``VAR=value`` assignments are skipped, ``$VAR``/``~`` are expanded,
    then a path (anything containing ``/``) must exist on disk and a bare name
    must resolve on PATH. This never RUNS the command — ``pm_status`` calls it
    on the read-only path (ADR-028), and existence is all we need to tell a
    live entry from one left behind by an uninstalled distribution (the
    2026-09-17 audit found a hook pointing at a deleted ``.mcpb`` venv).

    The verdict is deliberately fail-safe: a variable this process cannot
    expand makes the entry count as *present*, because a wrong "stale" answer
    would let :func:`install_hooks` rewrite a working entry.
    """
    try:
        words = shlex.split(command)
    except ValueError:
        words = command.split()
    while words and _ENV_ASSIGNMENT_RE.match(words[0]):
        words.pop(0)
    if not words:
        return False
    head = os.path.expandvars(words[0])
    if "$" in head:
        return True
    if "/" in head or head.startswith("~"):
        return Path(head).expanduser().exists()
    return shutil.which(head) is not None


def _pm_hook_commands(post_tool_use: list[dict]) -> list[str]:
    """Return every PM Lens hook command registered under PostToolUse, in order.

    Duplicates are preserved on purpose so callers can detect double
    registration (the same command appearing twice runs twice per event).
    """
    return [
        hook.get("command", "")
        for group in post_tool_use
        for hook in group.get("hooks", [])
        if _is_pm_hook_command(hook.get("command", ""))
    ]


def _strip_pm_hooks(post_tool_use: list[dict]) -> tuple[list[dict], int]:
    """Drop PM Lens hook commands from ``post_tool_use``, keeping everything else.

    Works at the COMMAND level, not the group level: a group that mixes a PM
    Lens hook with a user's own hook keeps the user's hook. Groups left empty
    are removed.

    Returns:
        ``(remaining_groups, removed_count)``.
    """
    remaining: list[dict] = []
    removed = 0
    for group in post_tool_use:
        kept = []
        for hook in group.get("hooks", []):
            if _is_pm_hook_command(hook.get("command", "")):
                removed += 1
            else:
                kept.append(hook)
        if kept:
            new_group = dict(group)
            new_group["hooks"] = kept
            remaining.append(new_group)
    return remaining, removed


def stale_pm_hook_warning(status: dict) -> dict | None:
    """Build the ``warnings[]`` entry for unhealthy PM Lens hook entries (ADR-053).

    Returns ``None`` when ``status`` (from :func:`get_hooks_status`) reports
    no stale or duplicate entries.
    """
    stale = status.get("stale", [])
    duplicates = status.get("duplicates", [])
    if not stale and not duplicates:
        return None
    problems = []
    if stale:
        problems.append(
            "command(s) whose executable no longer exists: " + "; ".join(f"`{c}`" for c in stale)
        )
    if duplicates:
        problems.append(
            "command(s) registered more than once (they run twice per event): "
            + "; ".join(f"`{c}`" for c in duplicates)
        )
    return {
        "code": "stale_pm_hook_command",
        "message": (
            f"PM Lens PostToolUse hook entries in {status.get('path')} need repair — "
            + " and ".join(problems)
            + ". Claude Code reports a hook error on every matching tool call until fixed."
        ),
        "remediation": (
            "Run `pmlens install-hooks` — it replaces stale and duplicate PM Lens "
            "entries with one fresh entry and leaves every other hook untouched. "
            "pm_status only reports; it never edits hooks that already exist."
        ),
        "stale": list(stale),
        "duplicates": list(duplicates),
    }


# ─── Hook status ──────────────────────────────────


def get_hooks_status() -> dict:
    """Check if pm-server hooks are installed, and whether they are healthy.

    Returns:
        dict with keys:
            installed (bool): at least one PM Lens hook command is registered
                (legacy ``pm-server`` or current ``pmlens`` identity).
            path (str): the settings file inspected.
            commands (list[str]): every PM Lens hook command, in file order.
            stale (list[str]): commands whose executable cannot be found
                (ADR-053) — e.g. left behind by an uninstalled distribution.
            duplicates (list[str]): commands registered more than once.
            healthy (bool): installed with no stale or duplicate entries.

        ``installed``/``path`` are the v0.4.x-compatible keys; the rest are
        additive (ADR-053) and read-only to compute.
    """
    path = _settings_path()
    settings = _load_settings(path)
    hooks = settings.get("hooks", {})
    post_tool_use = hooks.get("PostToolUse", [])

    commands = _pm_hook_commands(post_tool_use)
    stale = [cmd for cmd in commands if not _hook_binary_exists(cmd)]
    duplicates = sorted({cmd for cmd in commands if commands.count(cmd) > 1})
    installed = bool(commands)
    return {
        "installed": installed,
        "path": str(path),
        "commands": commands,
        "stale": stale,
        "duplicates": duplicates,
        "healthy": installed and not stale and not duplicates,
    }


# ─── Hook installation ────────────────────────────


def install_hooks() -> str:
    """Install pm-server hooks into Claude Code settings.

    Safely merges into existing hooks without overwriting user hooks. When PM
    Lens entries already exist but are unhealthy — the executable is gone, or
    the same command is registered twice — they are replaced by ONE fresh
    entry (ADR-053). Healthy existing entries are left alone. Only PM Lens's
    own commands are ever touched; every other hook is preserved.

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

    commands = _pm_hook_commands(post_tool_use)
    stale = [cmd for cmd in commands if not _hook_binary_exists(cmd)]
    has_duplicates = len(set(commands)) != len(commands)

    # Already installed and healthy — nothing to do.
    if commands and not stale and not has_duplicates:
        return "pm-server hooks already installed (skipped)"

    repaired = 0
    if commands:
        # Unhealthy: strip every PM Lens command (and only those), then fall
        # through to append one fresh entry.
        post_tool_use, repaired = _strip_pm_hooks(post_tool_use)
        settings["hooks"]["PostToolUse"] = post_tool_use

    # Append pm-server hook group
    config = _build_hook_config()
    post_tool_use.extend(config["hooks"]["PostToolUse"])

    _save_settings(path, settings)
    if repaired:
        return (
            "pm-server hooks repaired in Claude Code settings "
            f"(replaced {repaired} stale/duplicate entries with one fresh entry)"
        )
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

    # Filter out pm-server hook COMMANDS, keep everything else — including a
    # user's own hook that happens to share a group with ours (ADR-053).
    remaining, removed = _strip_pm_hooks(post_tool_use)

    if removed == 0:
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

    session_dir = Path(cwd or ".")
    commit_dir = git_commit_dir(command, session_dir)
    if commit_dir is None:
        return

    # Only act when the commit landed in a PM Lens project — that of the
    # repository the commit went to, which a `cd` or `git -C` can make
    # different from the session cwd.
    pm_path = _find_project_pm_dir(commit_dir)
    if pm_path is None:
        return

    # When pm_* tools called without project_path would resolve somewhere
    # else (PM_PROJECT_PATH, or a different cwd project), name the project.
    default_pm = _default_project_pm_dir(session_dir)
    other_project = None if default_pm == pm_path.resolve() else pm_path.resolve().parent
    reminder = _build_commit_reminder(pm_path, project_path=other_project)
    if reminder:
        json.dump(post_tool_use_context(reminder), sys.stdout)


_SHELL_SEPARATORS = re.compile(r"&&|\|\||[;|\n]")
_ENV_ASSIGNMENT = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*=")
# git global options that take their value as the NEXT token.
_GIT_OPTIONS_WITH_VALUE = frozenset({"-C", "-c", "--git-dir", "--work-tree", "--namespace"})
# git global options that print information instead of running a command.
_GIT_INFO_OPTIONS = frozenset(
    {"-h", "--help", "--version", "--exec-path", "--html-path", "--man-path", "--info-path"}
)
# `git commit` flags that show what would be committed without committing.
_NON_COMMIT_FLAGS = frozenset({"--dry-run", "--short", "--porcelain", "--long", "-h", "--help"})
_SEPARATOR_CHARS = frozenset(";&|()\n")


def _shell_commands(command: str) -> list[list[str]]:
    """Split a shell command line into simple commands (lists of words).

    Separators (``;``, ``&&``, ``||``, ``|``, ``&``, newlines, subshell
    parentheses) only count outside quotes, and a heredoc body is skipped, so
    ``echo "a; git commit"`` or a heredoc line reading ``git commit`` is text,
    not a command. Input that does not tokenise (unbalanced quotes) falls back
    to a plain split on separators.
    """
    lexer = shlex.shlex(command, posix=True, punctuation_chars=";&|()<>\n")
    lexer.whitespace = " \t\r"
    lexer.whitespace_split = True
    lexer.commenters = "#"
    try:
        tokens = list(lexer)
    except ValueError:
        return [segment.split() for segment in _SHELL_SEPARATORS.split(command)]

    commands: list[list[str]] = []
    current: list[str] = []
    pending_heredocs: list[str] = []
    expect_delimiter = False
    skipping: str | None = None
    at_line_start = False
    for token in tokens:
        if skipping is not None:
            if token == "\n":
                at_line_start = True
            elif at_line_start and token == skipping:
                skipping = pending_heredocs.pop(0) if pending_heredocs else None
                at_line_start = False
            else:
                at_line_start = False
            continue
        if expect_delimiter:
            pending_heredocs.append(token)
            expect_delimiter = False
            current.append(token)
            continue
        if token in ("<<", "<<-"):
            expect_delimiter = True
            current.append(token)
            continue
        if set(token) <= _SEPARATOR_CHARS:
            if current:
                commands.append(current)
                current = []
            if token == "\n" and pending_heredocs:
                skipping = pending_heredocs.pop(0)
                at_line_start = True
            continue
        current.append(token)
    if current:
        commands.append(current)
    return commands


def _commit_target(words: list[str], cwd: Path) -> Path | None:
    """If ``words`` is a ``git ... commit`` that creates a commit, return the
    directory it commits in (``-C`` applied to ``cwd``); otherwise ``None``."""
    i = 0
    while i < len(words) and _ENV_ASSIGNMENT.match(words[i]):
        i += 1
    if i >= len(words) or Path(words[i]).name != "git":
        return None
    i += 1
    target = cwd
    while i < len(words) and words[i].startswith("-"):
        option = words[i]
        if option in _GIT_INFO_OPTIONS:
            return None
        if option == "-C" and i + 1 < len(words):
            target = target / Path(words[i + 1]).expanduser()
        i += 2 if option in _GIT_OPTIONS_WITH_VALUE else 1
    if i >= len(words) or words[i] != "commit":
        return None
    if _NON_COMMIT_FLAGS & set(words[i + 1 :]):
        return None
    return target


def git_commit_dir(command: str, cwd: Path) -> Path | None:
    """Return the directory a ``git commit`` in ``command`` runs in, if any.

    Follows ``cd <dir>`` and ``git -C <dir>`` so a commit made in another
    repository is attributed to that repository, not to the session cwd.
    """
    current = cwd
    for words in _shell_commands(command):
        i = 0
        while i < len(words) and _ENV_ASSIGNMENT.match(words[i]):
            i += 1
        if i + 1 < len(words) and words[i] == "cd":
            current = current / Path(words[i + 1]).expanduser()
            continue
        target = _commit_target(words, current)
        if target is not None:
            return target
    return None


def is_git_commit(command: str) -> bool:
    """Return True when ``command`` runs ``git commit`` as a command.

    Once the reminder actually reached the model (PMSERV-196), a substring
    test turned every ``grep "git commit"``, ``echo ... git commit``, heredoc
    line or ``git commit --dry-run`` into a false "commit recorded" claim.
    """
    return git_commit_dir(command, Path(".")) is not None


def _find_project_pm_dir(start: Path) -> Path | None:
    """Return the ``.pm/`` of the project containing ``start``, if any.

    The nearest ancestor whose ``.pm/`` holds ``project.yaml`` (the global
    ``~/.pm`` does not) — the same walk-up ``utils.resolve_project_path``
    uses.
    """
    from .utils import _is_project_pm_dir

    try:
        start = start.resolve()
    except OSError:  # pragma: no cover — defensive FS guard
        return None
    for directory in (start, *start.parents):
        if _is_project_pm_dir(directory / ".pm"):
            return directory / ".pm"
    return None


def _default_project_pm_dir(cwd: Path) -> Path | None:
    """The ``.pm/`` a pm_* tool call without ``project_path`` would use:
    ``PM_PROJECT_PATH`` first, then the walk-up from ``cwd``."""
    from .utils import _is_project_pm_dir

    if env_path := os.environ.get("PM_PROJECT_PATH"):
        pm_dir = Path(env_path).expanduser() / ".pm"
        if _is_project_pm_dir(pm_dir):
            return pm_dir.resolve()
    return _find_project_pm_dir(cwd)


def post_tool_use_context(text: str) -> dict:
    """Wrap ``text`` in the envelope Claude Code reads from a PostToolUse hook.

    Claude Code only injects ``hookSpecificOutput.additionalContext`` (with a
    matching ``hookEventName``); a top-level ``additionalContext`` key is
    dropped as unrecognized, which silently kept every commit reminder out of
    the model's context until PMSERV-196.
    """
    return {"hookSpecificOutput": {"hookEventName": "PostToolUse", "additionalContext": text}}


# Task ids are auto-numbered PREFIX-NNN; anything else in tasks.yaml is
# repository-controlled text and must not be spliced into model context.
_TASK_ID = re.compile(r"^[A-Za-z][A-Za-z0-9_-]{0,39}$")
# Characters that could break out of the quoted path or start a new line.
_UNSAFE_PATH_CHARS = re.compile(r"[\x00-\x1f\x7f\"'`]")


def _build_commit_reminder(pm_path: Path, project_path: Path | None = None) -> str:
    """Build a contextual PM reminder after git commit.

    Returns an empty string (no reminder) when no task is in progress: there
    is nothing a commit could have completed, and an unconditional "please
    execute" list is exactly the kind of instruction current models follow
    too literally (ADR-054).

    PMSERV-086 / WF-026 FINDING-E: under PM_LENS=1 the host process exposes
    only the RO_ALLOWLIST tools, so suggesting ``pm_update_task`` / ``pm_log``
    would produce a confusing ``tool not found`` for the user. Switch to a
    read-only suggestion set when Lens is active.
    """
    from .models import TaskStatus
    from .storage import load_tasks

    lens_mode = os.environ.get("PM_LENS", "").lower() in {"1", "true", "yes", "on"}

    tasks = load_tasks(pm_path)
    active = [t for t in tasks if t.status == TaskStatus.IN_PROGRESS and _TASK_ID.match(t.id)]
    task_ids = ", ".join(t.id for t in active)

    if lens_mode:
        lines = ["[PM Lens] git commit recorded. (Read-only mode)"]
        if active:
            lines.append(f"In progress: {task_ids}.")
        lines.append("pm_next, pm_status and pm_tasks show the current progress.")
        lines.append(
            "Recording task updates needs a full-mode pmlens host "
            "(for example Claude Code or Codex CLI, without PM_LENS=1)."
        )
        return "\n".join(lines)

    if not active:
        return ""

    where = ""
    if project_path is not None:
        if _UNSAFE_PATH_CHARS.search(str(project_path)):
            # Naming the project is what keeps the update off the wrong one;
            # without a safe way to name it, say nothing.
            return ""
        where = f' These tasks belong to the project at "{project_path}"; pass it as project_path.'
    return (
        f"[PM Lens] git commit recorded. In progress: {task_ids}. "
        "If this commit completed one of them and it is not yet marked done, "
        "mark it done with pm_update_task and add a pm_log entry. "
        f"For an intermediate commit, leave task statuses unchanged.{where}"
    )
