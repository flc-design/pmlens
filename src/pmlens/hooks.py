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
import tempfile
from pathlib import Path

from .models import PmServerError

# Hook command that Claude Code will call. New installs emit the ``pmlens``
# binary; the legacy ``pm-server hook`` form is still RECOGNIZED on detection
# and removal via ``_PM_HOOK_COMMAND_RE`` below (dual-recognition, PMSERV-137 /
# ADR-034): detection/removal must match BOTH identities so an upgrade-in-place
# (or the migrate updater) never strands a half-renamed hook.
_HOOK_COMMAND_PREFIX = "pmlens hook"


def _settings_path() -> Path:
    """Return the global Claude Code settings path."""
    return Path.home() / ".claude" / "settings.json"


# Why a settings file could not be used. Fixed texts: a parser's own message
# repeats text from the file, and these end up in tool responses.
_PROBLEM_IO = "it could not be read"
_PROBLEM_JSON = "it is not valid JSON"
_PROBLEM_TOP_LEVEL = "its top level is not a JSON object"
_PROBLEM_HOOKS_SHAPE = (
    "its `hooks` entry is not an object, or the `PostToolUse` entry in it is not a list"
)


def _settings_unreadable_warning(path: Path, problem: str) -> dict:
    """The ``warnings[]`` entry for a settings file pmlens will not write to."""
    return {
        "level": "warning",
        "code": "hooks_settings_unreadable",
        "message": (
            f"pmlens could not use {path} as Claude Code settings: {problem}. "
            "It cannot tell whether its PostToolUse hook (the reminder after a "
            "git commit) is installed there."
        ),
        "remediation": (
            "Fix the JSON in that file by hand; a trailing comma or a missing "
            "bracket is the usual cause. pmlens did not change the file and will "
            "not write to it until it parses as a JSON object. After that, "
            "`pmlens install-hooks` installs the hook."
        ),
    }


class SettingsUnreadableError(PmServerError):
    """The settings file exists, but pmlens cannot read it as Claude Code settings.

    Raised instead of treating the file as empty. The callers write the file
    back, and "empty plus the pmlens hook" used to replace a user's whole
    configuration (deny rules, env, their own hooks) when the file merely had
    a trailing comma.

    ``str()`` of the error is the message and remediation of
    :attr:`warning`, built from the path and a fixed reason only.
    """

    def __init__(self, path: Path, problem: str) -> None:
        self.path = path
        self.problem = problem
        self.warning = _settings_unreadable_warning(path, problem)
        super().__init__(f"{self.warning['message']} {self.warning['remediation']}")


# Why a settings file was not written. Fixed texts, for the same reason.
_PROBLEM_READ_ONLY = "the file is read-only"
_PROBLEM_DANGLING_LINK = "it is a link to a location that does not exist"
_PROBLEM_DIRECTORY = "its directory is not writable, so it cannot be replaced safely"
_PROBLEM_WRITE_FAILED = "writing it failed"
_PROBLEM_NOT_UTF8 = "it holds text that cannot be written back as UTF-8"

_REMEDY_NOT_WRITABLE = (
    "Make that file and its directory writable (for a link: create the "
    "directory it points into), then run the command again; `pmlens "
    "install-hooks` installs the hook. pm_status tries the hook install "
    "again on each call and reports this until it succeeds."
)
# Permissions are not the cause here, so the advice above would mislead.
_REMEDY_NOT_UTF8 = (
    "A string in that file holds half of a character pair, written as an "
    "escape in the range \\ud800 to \\udfff without its other half (an emoji "
    "cut in the middle is the usual cause). pmlens did not change the file. "
    "Fix or remove that escape by hand, then run the command again; `pmlens "
    "install-hooks` installs the hook. pm_status tries the hook install "
    "again on each call and reports this until it succeeds."
)


def _settings_not_writable_warning(path: Path, problem: str) -> dict:
    """The ``warnings[]`` entry for a settings file pmlens could not write."""
    remediation = _REMEDY_NOT_UTF8 if problem == _PROBLEM_NOT_UTF8 else _REMEDY_NOT_WRITABLE
    return {
        "level": "warning",
        "code": "hooks_settings_not_writable",
        "message": (
            f"pmlens could not write Claude Code settings at {path}: {problem}. "
            "It did not complete the change it was making there (its PostToolUse "
            "hook, the reminder after a git commit, or a permission migration)."
        ),
        "remediation": remediation,
    }


class SettingsNotWritableError(PmServerError):
    """pmlens will not, or could not, write the settings file.

    Raised by :func:`_save_settings` before anything is written when the file
    is read-only or is a link to a location that does not exist: replacing a
    file only needs a writable directory, so without this check a status read
    would rewrite a file its owner had write-protected. Also raised, equally
    before anything is written, when the settings cannot be encoded as UTF-8.

    ``str()`` of the error is the message and remediation of
    :attr:`warning`, built from the path and a fixed reason only.
    """

    def __init__(self, path: Path, problem: str) -> None:
        self.path = path
        self.problem = problem
        self.warning = _settings_not_writable_warning(path, problem)
        super().__init__(f"{self.warning['message']} {self.warning['remediation']}")


def hooks_settings_write_failed_warning() -> dict:
    """The ``warnings[]`` entry for a settings write that ended in an ``OSError``.

    For the callers that must not pass the error on (``pm_status`` is a status
    read; the CLI prints one line instead of a traceback). The operating
    system's own message is left out: it is not a fixed text.
    """
    return _settings_not_writable_warning(_settings_path(), _PROBLEM_WRITE_FAILED)


def _load_settings(path: Path) -> dict:
    """Load Claude Code settings; an empty dict only when the file does not exist.

    Raises:
        SettingsUnreadableError: The file exists but cannot be read, is not
            valid JSON, or does not hold a JSON object at the top level.
    """
    try:
        # utf-8-sig: a byte-order mark is not a reason to refuse the file.
        raw = path.read_text(encoding="utf-8-sig")
    except FileNotFoundError:
        # Also a dangling symlink: there is nothing a write could destroy.
        return {}
    except (OSError, UnicodeDecodeError):
        raise SettingsUnreadableError(path, _PROBLEM_IO) from None
    try:
        settings = json.loads(raw)
    except (ValueError, RecursionError):
        raise SettingsUnreadableError(path, _PROBLEM_JSON) from None
    if not isinstance(settings, dict):
        raise SettingsUnreadableError(path, _PROBLEM_TOP_LEVEL)
    return settings


def _post_tool_use_groups(path: Path, settings: dict) -> list:
    """The ``hooks.PostToolUse`` list of ``settings``; an empty list when absent.

    Raises:
        SettingsUnreadableError: ``hooks`` or ``hooks.PostToolUse`` is present
            with another type. pmlens did not write that value and does not
            know what it means, so it will not replace it.
    """
    if "hooks" not in settings:
        return []
    hooks = settings["hooks"]
    if not isinstance(hooks, dict):
        raise SettingsUnreadableError(path, _PROBLEM_HOOKS_SHAPE)
    if "PostToolUse" not in hooks:
        return []
    groups = hooks["PostToolUse"]
    if not isinstance(groups, list):
        raise SettingsUnreadableError(path, _PROBLEM_HOOKS_SHAPE)
    return groups


def _save_settings(path: Path, settings: dict) -> None:
    """Write settings back atomically, keeping every key and the file's mode.

    Claude Code reads and writes this file too, so a crash or a concurrent
    reader must never see half of it: the text goes to a temporary file in the
    same directory, is flushed to disk, and then replaces the target. The
    target is the resolved path, so a ``settings.json`` that is a symlink into
    a dotfiles checkout stays a symlink and the file it points at is the one
    replaced.

    Not ``utils._atomic_write_text``: that helper is how pmlens writes its own
    ledgers, and the static check of the read-only tool surface counts every
    caller of it as a ledger writer. This file is not a ledger, and the one
    read tool that reaches this function (``pm_status``) does so only outside
    Lens, which that check cannot see.

    The file is never written in place. Where no temporary file can be created
    next to it (a writable file in a directory that is not), the write is
    refused: an in-place write that fails partway leaves the user's settings
    truncated, and the callers include a status read.

    Raises:
        SettingsNotWritableError: The file is read-only, its directory is not
            writable, it is a link whose destination directory does not
            exist, or the settings hold text that is not valid UTF-8. Nothing
            is written.
        OSError: The write itself failed. The old file is still in place.
    """
    target = path.resolve()
    # Only the directory of the settings path itself (~/.claude) is created.
    # Where a link points is the user's: a dangling link into a dotfiles
    # checkout that is not there yet must not make pmlens create that tree.
    path.parent.mkdir(parents=True, exist_ok=True)
    if not target.parent.is_dir():
        raise SettingsNotWritableError(path, _PROBLEM_DANGLING_LINK)
    # os.replace only needs a writable directory, so the file's own write
    # protection has to be checked here or it would be bypassed.
    if target.exists() and not os.access(target, os.W_OK):
        raise SettingsNotWritableError(path, _PROBLEM_READ_ONLY)
    text = json.dumps(settings, indent=2, ensure_ascii=False) + "\n"
    # Encoded before any file is opened. JSON may hold half of a surrogate
    # pair as an escape (a writer that cut a string in the middle of an emoji
    # leaves one); it loads, but it is not UTF-8.
    try:
        data = text.encode("utf-8")
    except UnicodeEncodeError:
        # Not chained: the codec's message quotes the file's text.
        raise SettingsNotWritableError(path, _PROBLEM_NOT_UTF8) from None
    try:
        fd, tmp_name = tempfile.mkstemp(dir=str(target.parent), suffix=".tmp")
    except PermissionError:
        raise SettingsNotWritableError(path, _PROBLEM_DIRECTORY) from None
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        if target.exists():
            shutil.copymode(target, tmp_name)
        else:
            # mkstemp creates 0600; a new settings file gets the mode a plain
            # open() would have given it, as before.
            current_umask = os.umask(0)
            os.umask(current_umask)
            os.chmod(tmp_name, 0o666 & ~current_umask)
        os.replace(tmp_name, target)
    except BaseException:
        Path(tmp_name).unlink(missing_ok=True)
        raise


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


def _group_hooks(group: object) -> list:
    """The hook entries of one PostToolUse group, or ``[]`` for an unknown shape.

    The file belongs to Claude Code and to the user. An entry pmlens does not
    recognise is never a pmlens hook, so it is skipped here and kept as it is
    on a write.
    """
    if not isinstance(group, dict):
        return []
    entries = group.get("hooks")
    return entries if isinstance(entries, list) else []


def _hook_command(hook: object) -> str:
    """The command of one hook entry, or ``""`` when it has none."""
    if not isinstance(hook, dict):
        return ""
    command = hook.get("command")
    return command if isinstance(command, str) else ""


def _is_pm_hook(hook_group: dict) -> bool:
    """Check if a hook group contains at least one PM Lens hook command."""
    return any(_is_pm_hook_command(_hook_command(hook)) for hook in _group_hooks(hook_group))


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
        _hook_command(hook)
        for group in post_tool_use
        for hook in _group_hooks(group)
        if _is_pm_hook_command(_hook_command(hook))
    ]


def _strip_pm_hooks(post_tool_use: list[dict]) -> tuple[list[dict], int]:
    """Drop PM Lens hook commands from ``post_tool_use``, keeping everything else.

    Works at the COMMAND level, not the group level: a group that mixes a PM
    Lens hook with a user's own hook keeps the user's hook. A group is removed
    only when removing PM Lens commands left it empty; every other group,
    including one whose shape pmlens does not recognise, is kept as it is.

    Returns:
        ``(remaining_groups, removed_count)``.
    """
    remaining: list[dict] = []
    removed = 0
    for group in post_tool_use:
        entries = _group_hooks(group)
        kept = [hook for hook in entries if not _is_pm_hook_command(_hook_command(hook))]
        if len(kept) == len(entries):
            remaining.append(group)
            continue
        removed += len(entries) - len(kept)
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


def hooks_settings_unreadable_warning(status: dict) -> dict | None:
    """Build the ``warnings[]`` entry for a settings file pmlens could not use.

    Returns ``None`` when ``status`` (from :func:`get_hooks_status`) read the
    file, or found none.
    """
    if status.get("readable", True):
        return None
    return _settings_unreadable_warning(
        Path(str(status.get("path", ""))), str(status.get("problem") or _PROBLEM_IO)
    )


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
            readable (bool): False when the settings file exists but could not
                be read as Claude Code settings. Nothing is known about the
                hooks then (``installed`` is False, the lists are empty), and
                ``problem`` (str) says why in a fixed text.

        ``installed``/``path`` are the v0.4.x-compatible keys; the rest are
        additive (ADR-053) and read-only to compute.
    """
    path = _settings_path()
    try:
        post_tool_use = _post_tool_use_groups(path, _load_settings(path))
    except SettingsUnreadableError as exc:
        return {
            "installed": False,
            "path": str(path),
            "commands": [],
            "stale": [],
            "duplicates": [],
            "healthy": False,
            "readable": False,
            "problem": exc.problem,
        }

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
        "readable": True,
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

    Raises:
        SettingsUnreadableError: The settings file exists but cannot be read
            as Claude Code settings. Nothing is written.
        SettingsNotWritableError: The settings file is read-only, or is a link
            to a location that does not exist. Nothing is written.
        OSError: Writing the file failed.
    """
    path = _settings_path()
    settings = _load_settings(path)
    post_tool_use = _post_tool_use_groups(path, settings)

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

    # Append pm-server hook group
    config = _build_hook_config()
    settings.setdefault("hooks", {})["PostToolUse"] = [
        *post_tool_use,
        *config["hooks"]["PostToolUse"],
    ]

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

    Raises:
        SettingsUnreadableError: The settings file exists but cannot be read
            as Claude Code settings. Nothing is written.
        SettingsNotWritableError: The settings file is read-only, or is a link
            to a location that does not exist. Nothing is written.
        OSError: Writing the file failed.
    """
    path = _settings_path()
    settings = _load_settings(path)
    post_tool_use = _post_tool_use_groups(path, settings)

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
