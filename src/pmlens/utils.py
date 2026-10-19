"""Shared utilities for PM Lens."""

from __future__ import annotations

import errno
import hashlib
import os
import shutil
import stat
import tempfile
from collections.abc import Sequence
from datetime import datetime
from pathlib import Path

from .hosts import HOSTS
from .models import Phase, ProjectNotFoundError, Task, TaskStatus

# Computed at module import time — represents the bytes Python actually
# loaded into memory. Used by ``get_utils_fingerprint`` to detect stale
# module cache after editable-install source edits (PMSERV-060).
_UTILS_FINGERPRINT: str = hashlib.sha1(Path(__file__).read_bytes()).hexdigest()[:8]


def get_utils_fingerprint() -> dict:
    """Return fingerprint info for stale-module-cache detection (PMSERV-060).

    ``loaded`` is the sha1[:8] computed when this module was imported — i.e.
    what the running Python process is actually executing. ``current`` is
    re-read from disk on each call. A mismatch means the source file was
    edited after this MCP process started; restart the server to pick up
    the new code. See ``BUG_REPORT_2026-05-01_pm_status_stale_cache.md``
    history (now removed) for the originating incident.
    """
    try:
        current = hashlib.sha1(Path(__file__).read_bytes()).hexdigest()[:8]
    except OSError:
        current = "unreadable"
    return {
        "loaded": _UTILS_FINGERPRINT,
        "current": current,
        "stale": _UTILS_FINGERPRINT != current and current != "unreadable",
        "path": str(Path(__file__).resolve()),
    }


def _is_project_pm_dir(pm_dir: Path) -> bool:
    """Check if a .pm/ directory belongs to an actual project.

    A project .pm/ always contains project.yaml (created by pm_init).
    The global ~/.pm/ only has registry.yaml and memory.db, so it is
    excluded by this check.
    """
    return pm_dir.is_dir() and (pm_dir / "project.yaml").exists()


class FileRefusedError(Exception):
    """A file that exists but is not read or written; the message is fixed text.

    Raised for a symlink (the file or a directory on the way), something that
    is not a regular file or directory, a file larger than the limit, or a
    name that is not a plain relative path.
    """


_NOFOLLOW = getattr(os, "O_NOFOLLOW", 0)
_DIR_FLAGS = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | _NOFOLLOW | getattr(os, "O_CLOEXEC", 0)


def _plain_parts(name: str | Sequence[str]) -> list[str]:
    parts = [name] if isinstance(name, str) else list(name)
    for part in parts:
        if (
            not part
            or part in {".", ".."}
            or "/" in part
            or os.sep in part
            or (os.altsep and os.altsep in part)
            or "\x00" in part
        ):
            raise FileRefusedError("the name is not a plain relative path")
    if not parts:
        raise FileRefusedError("the name is not a plain relative path")
    return parts


def _no_dir_fd() -> bool:
    return not (os.open in os.supports_dir_fd and hasattr(os, "O_DIRECTORY") and _NOFOLLOW)


def _open_dir_nofollow(path: Path | str, dir_fd: int | None = None) -> int | None:
    """Open a directory without following a symlink at its last component.

    Returns None when it does not exist; raises FileRefusedError when it is a link
    or not a directory.
    """
    try:
        return os.open(path, _DIR_FLAGS, dir_fd=dir_fd)
    except FileNotFoundError:
        return None
    except OSError as exc:
        if exc.errno in {errno.ELOOP, errno.ENOTDIR, errno.EMLINK}:
            raise FileRefusedError("a symlink or not a directory") from None
        reason = os.strerror(exc.errno) if exc.errno else type(exc).__name__
        raise FileRefusedError(f"the directory could not be opened ({reason})") from None


def read_file_in_dir(directory: Path, name: str | Sequence[str], max_bytes: int) -> bytes | None:
    """Read a file under ``directory`` once, following no symlink on the way.

    For files a repository supplies (workflow templates, prompt-pack templates):
    a symlink there could point at any file the server can read, and a check on
    the path followed by a separate open can be raced by swapping the path in
    between (PMSERV-270, PMSERV-271). ``directory``, each subdirectory in
    ``name`` and the file are opened in turn relative to the previous one with
    ``O_NOFOLLOW``, and the file is checked on its descriptor. Containment is
    by path: a hard link needs write access to the project and is not refused.
    Where ``os.open`` takes no ``dir_fd`` (Windows), symlinks are refused by a
    check before the open instead, which is not atomic.

    Args:
        directory: The directory the file must be under.
        name: The file name, or its path below ``directory`` as parts.
        max_bytes: The largest file accepted.

    Returns:
        The file's bytes, or None when it (or a directory on the way) does not
        exist.

    Raises:
        FileRefusedError: It exists but is a symlink (or a directory on the way is),
            is not a regular file, is larger than ``max_bytes``, or ``name`` is
            not a plain relative path.
    """
    parts = _plain_parts(name)
    flags = os.O_RDONLY | _NOFOLLOW | getattr(os, "O_NONBLOCK", 0) | getattr(os, "O_CLOEXEC", 0)
    if _no_dir_fd():
        path = directory
        for part in parts:
            path = path / part
            if path.is_symlink():
                raise FileRefusedError("a symlink")
        if directory.is_symlink():
            raise FileRefusedError("a symlink")
        try:
            fd = os.open(path, flags)
        except FileNotFoundError:
            return None
        except OSError:
            raise FileRefusedError("the file could not be opened") from None
    else:
        dir_fd = _open_dir_nofollow(directory)
        if dir_fd is None:
            return None
        try:
            for part in parts[:-1]:
                next_fd = _open_dir_nofollow(part, dir_fd=dir_fd)
                os.close(dir_fd)
                dir_fd = next_fd
                if dir_fd is None:
                    return None
            try:
                fd = os.open(parts[-1], flags, dir_fd=dir_fd)
            except FileNotFoundError:
                return None
            except OSError as exc:
                if exc.errno in {errno.ELOOP, errno.EMLINK}:
                    raise FileRefusedError("a symlink") from None
                raise FileRefusedError("the file could not be opened") from None
        finally:
            if dir_fd is not None:
                os.close(dir_fd)
    chunks: list[bytes] = []
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode):
            raise FileRefusedError("not a regular file")
        if info.st_size > max_bytes:
            raise FileRefusedError(f"larger than {max_bytes} bytes")
        remaining = max_bytes + 1
        while remaining > 0:
            chunk = os.read(fd, remaining)
            if not chunk:
                break
            chunks.append(chunk)
            remaining -= len(chunk)
    except OSError:
        raise FileRefusedError("the file could not be read") from None
    finally:
        os.close(fd)
    data = b"".join(chunks)
    if len(data) > max_bytes:
        raise FileRefusedError(f"larger than {max_bytes} bytes")
    return data


def write_file_in_dir(parent: Path, dirname: str, filename: str, data: bytes) -> None:
    """Write ``parent/dirname/filename`` without following a symlink.

    For pmlens's own output inside a project (``.pm/exports``): a repository
    can ship that directory or the file as a symlink, and writing through it
    would overwrite any file the user can write (PMSERV-270 review). The
    directory is created if missing, refused if it is a link, and the file is
    written to a temporary name and renamed over the final one, so a link
    there is replaced, never written through. ``parent`` itself is trusted.

    As with ``open(path, "w")``: new files and the directory get the umask's
    permissions, an existing file keeps its permission bits, and a read-only
    file is not replaced.

    Raises:
        FileRefusedError: ``dirname`` is a symlink or not a directory, or a name is
            not plain.
        OSError: The write failed (PermissionError for a read-only file).
    """
    _plain_parts([dirname, filename])
    if _no_dir_fd():
        directory = parent / dirname
        if directory.is_symlink():
            raise FileRefusedError("a symlink or not a directory")
        directory.mkdir(parents=True, exist_ok=True)
        _atomic_write_text(directory / filename, data.decode("utf-8"))
        return
    parent.mkdir(parents=True, exist_ok=True)
    parent_fd = os.open(parent, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
    try:
        dir_fd = _open_dir_nofollow(dirname, dir_fd=parent_fd)
        if dir_fd is None:
            try:
                os.mkdir(dirname, 0o777, dir_fd=parent_fd)
            except FileExistsError:
                pass
            dir_fd = _open_dir_nofollow(dirname, dir_fd=parent_fd)
            if dir_fd is None:
                raise FileRefusedError("the directory could not be created")
    finally:
        os.close(parent_fd)
    try:
        try:
            existing = os.stat(filename, dir_fd=dir_fd, follow_symlinks=False)
        except FileNotFoundError:
            existing = None
        keep_mode = None
        if existing is not None and stat.S_ISREG(existing.st_mode):
            if existing.st_uid == os.getuid() and not existing.st_mode & stat.S_IWUSR:
                raise PermissionError(errno.EACCES, "the existing file is read-only")
            keep_mode = stat.S_IMODE(existing.st_mode)
        temp = f".{filename}.{os.getpid()}.{os.urandom(4).hex()}.tmp"
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | _NOFOLLOW | getattr(os, "O_CLOEXEC", 0)
        fd = os.open(temp, flags, 0o666, dir_fd=dir_fd)
        try:
            with os.fdopen(fd, "wb") as fh:
                if keep_mode is not None:
                    os.fchmod(fh.fileno(), keep_mode)
                fh.write(data)
            os.replace(temp, filename, src_dir_fd=dir_fd, dst_dir_fd=dir_fd)
        except BaseException:
            try:
                os.unlink(temp, dir_fd=dir_fd)
            except OSError:
                pass
            raise
    finally:
        os.close(dir_fd)


def _pick_from_registry() -> Path | None:
    """Auto-pick the single registered project under PM_LENS=1.

    Active only when ``PM_LENS`` is truthy: in normal pm-server mode the
    cwd walk-up is the right semantic, while in Lens mode (Claude
    Desktop/Cowork) we want a passive viewer to land on the user's only
    registered project without requiring an explicit argument.

    Returns None when:
      - PM_LENS is not active
      - ``~/.pm/registry.yaml`` is missing or unreadable
      - the registry contains zero or two-or-more entries (ambiguous —
        let the caller decide rather than silently picking)
      - the resolved path no longer has a valid ``.pm/project.yaml``
    """
    if os.environ.get("PM_LENS", "").lower() not in {"1", "true", "yes", "on"}:
        return None
    # Local import: storage imports this module. Its loader caps alias
    # expansion and raises errors that quote nothing (PMSERV-266).
    from .models import PmServerError
    from .storage import _load_yaml

    registry_file = Path.home() / ".pm" / "registry.yaml"
    if not registry_file.exists():
        return None
    try:
        data = _load_yaml(registry_file) or {}
    except (OSError, PmServerError):
        return None
    if not isinstance(data, dict):
        return None
    projects = data.get("projects", [])
    if not isinstance(projects, list) or len(projects) != 1:
        return None
    entry = projects[0]
    if not isinstance(entry, dict) or "path" not in entry:
        return None
    try:
        p = Path(entry["path"]).resolve()
    except (OSError, ValueError):
        return None
    return p if _is_project_pm_dir(p / ".pm") else None


def resolve_project_path(project_path: str | None = None) -> Path:
    """Resolve the project root directory.

    Priority (PMSERV-082 / PMSERV-085 — MCP roots removed pending
    a future spec-compliant implementation; see WF-026 / KR-011):
      1. Explicit ``project_path`` argument
      2. ``PM_PROJECT_PATH`` environment variable
      3. Single-entry registry auto-pick (active only under ``PM_LENS=1``)
      4. Walk up from cwd looking for ``.pm/project.yaml``

    The cwd walk-up skips ``.pm/`` directories without ``project.yaml``
    (e.g. the global ``~/.pm/`` used for registry).
    """
    if project_path:
        p = Path(project_path).resolve()
        if not (p / ".pm").is_dir():
            raise ProjectNotFoundError(f"No .pm/ directory found at {p}. Run pm_init first.")
        return p

    if env_path := os.environ.get("PM_PROJECT_PATH"):
        p = Path(env_path).resolve()
        if (p / ".pm").is_dir():
            return p

    if picked := _pick_from_registry():
        return picked

    cwd = Path.cwd()
    for parent in [cwd, *cwd.parents]:
        if _is_project_pm_dir(parent / ".pm"):
            return parent

    raise ProjectNotFoundError(
        "No .pm/ directory found via project_path arg, PM_PROJECT_PATH, "
        "registry auto-pick, or cwd walk-up. "
        "Provide project_path or run pm_init first."
    )


def is_initialized_project(path_str: str) -> bool:
    """Check whether ``path_str`` is a pm_init'd project (PMSERV-147, AD-7).

    The predicate is keyed **only** on ``.pm/project.yaml`` existence at the
    resolved path — registry.yaml is deliberately never consulted here, since
    the registry is a lazily-updated cache and this predicate must stay a
    single source of truth shared by the outbox remember/log warnings, the
    pm_outbox_merge pre-flight guard, and the pm_outbox_pending
    unregistered_projects[] aggregation (ADR-039 T4).

    Any resolution error (``OSError``, ``ValueError``, or any other exception
    raised while expanding/resolving the path) is treated as "not
    initialized" rather than propagated — callers use this as a cheap
    boolean gate, not a validator that should ever raise.
    """
    try:
        resolved = Path(path_str).expanduser().resolve()
        return (resolved / ".pm" / "project.yaml").exists()
    except (OSError, ValueError, RuntimeError):
        return False


def generate_task_id(project_name: str, number: int) -> str:
    """Generate a task ID like PROJ-001 from project name and sequence number."""
    prefix = project_name.upper().replace("-", "").replace("_", "")[:6]
    return f"{prefix}-{number:03d}"


def generate_decision_id(number: int) -> str:
    """Generate an ADR ID like ADR-001."""
    return f"ADR-{number:03d}"


def generate_risk_id(number: int) -> str:
    """Generate a risk ID like RISK-001."""
    return f"RISK-{number:03d}"


def aggregate_task_status(tasks: list[Task]) -> dict[str, int]:
    """Count tasks by status. Returns {status_value: count}."""
    counts = {s.value: 0 for s in TaskStatus}
    for t in tasks:
        counts[t.status.value] += 1
    return counts


def calculate_phase_progress(tasks: list[Task], phase: Phase) -> dict:
    """Calculate progress for a single phase."""
    phase_tasks = [t for t in tasks if t.phase == phase.id]
    done = sum(1 for t in phase_tasks if t.status == TaskStatus.DONE)
    total = len(phase_tasks)
    return {
        "id": phase.id,
        "name": phase.name,
        "status": phase.status.value,
        "done": done,
        "total": total,
        "pct": round(done / total * 100) if total > 0 else 0,
        "target_date": phase.target_date.isoformat() if phase.target_date else None,
    }


# --- MCP host targets (shared by installer.py and rules.py) ---------------

# Derived from the single host registry (PMSERV-165) rather than restated.
# Tuple order is significant — orchestrators dispatch in this order and the CLI
# prints results in the same sequence — and comes from HOSTS's insertion order,
# which keeps "claude-code" first.
_KNOWN_HOSTS: tuple[str, ...] = tuple(HOSTS)
TARGET_CHOICES: tuple[str, ...] = ("auto", "all", *_KNOWN_HOSTS)


def _resolve_targets(target: str) -> list[str]:
    """Expand a target spec into a concrete list of host identifiers.

    ``"auto"`` and ``"all"`` are synonyms here (ADR-007 #1: target
    dispatch). The detection-aware ``"auto"`` semantics for rule-file
    injection are layered on top in ``rules.detect_hosts``.
    """
    if target in ("auto", "all"):
        return list(_KNOWN_HOSTS)
    if target in _KNOWN_HOSTS:
        return [target]
    raise ValueError(f"unknown target: {target!r}. Expected one of {TARGET_CHOICES}.")


def _host_config_path(host_id: str) -> Path:
    """Config file a file-registered host stores MCP servers in.

    Lazy on every call so a monkeypatched ``HOME`` is honoured by fixtures.

    Raises:
        ValueError: if the host is unknown or registers via its own CLI
            (``claude-code``), which has no config file pmlens edits.
    """
    spec = HOSTS.get(host_id)
    if spec is None:
        raise ValueError(f"unknown host: {host_id!r}. Expected one of {tuple(HOSTS)}.")
    if spec.config_file is None:
        raise ValueError(f"host {host_id!r} registers via {spec.config_label}, not a config file")
    return spec.config_file()


def _codex_config_path() -> Path:
    """Return the Codex CLI config path (lazy; honors monkeypatched HOME).

    Kept as a named helper: it predates the host registry and is imported
    directly by ``installer`` and ``rules``.
    """
    return _host_config_path("codex")


# --- File mutation helpers (shared by installer.py and rules.py) ----------


def _timestamped_backup(path: Path) -> Path:
    """Create a timestamped ``.bak.<ts>`` copy next to ``path``.

    Uses ``shutil.copy2`` to preserve mtime and permissions. Caller is
    responsible for ensuring ``path`` exists.
    """
    timestamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    backup_path = path.with_name(f"{path.name}.bak.{timestamp}")
    shutil.copy2(path, backup_path)
    return backup_path


def _atomic_write_text(path: Path, content: str, *, encoding: str = "utf-8") -> None:
    """Atomically replace ``path`` with ``content`` using mkstemp + rename.

    ``tempfile.mkstemp`` produces a randomised filename so concurrent
    writers cannot collide on a fixed ``.tmp`` suffix. The temp file is
    created in the same directory as ``path`` to keep ``os.replace`` on
    a single filesystem. On exception the temp file is removed.

    File-permission normalisation: ``mkstemp`` opens with mode 0600
    (security default for secrets), but rule files like ``CLAUDE.md`` /
    ``AGENTS.md`` are user content that should follow the conventional
    ``open(...,"w")`` permissions (``0o666 & ~umask``). When the target
    already exists we inherit its mode via ``shutil.copymode`` so users
    who locked down a file keep that lock.
    """
    tmp_fd, tmp_name = tempfile.mkstemp(dir=str(path.parent), suffix=".tmp")
    try:
        with os.fdopen(tmp_fd, "w", encoding=encoding) as f:
            f.write(content)
        if path.exists():
            shutil.copymode(path, tmp_name)
        else:
            current_umask = os.umask(0)
            os.umask(current_umask)
            os.chmod(tmp_name, 0o666 & ~current_umask)
        os.replace(tmp_name, path)
    except BaseException:
        Path(tmp_name).unlink(missing_ok=True)
        raise
