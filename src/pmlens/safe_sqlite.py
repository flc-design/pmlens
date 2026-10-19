"""Open a project's SQLite store without following a symlink (PMSERV-277).

A cloned repository can ship ``.pm/memory.db`` or ``.pm/drafts.db`` as a
symlink, and SQLite follows it: a dangling link made pmlens create the target
anywhere the user can write and put memories, or a draft's unredacted
``raw_content``, in it. Python's ``sqlite3`` cannot pass
``SQLITE_OPEN_NOFOLLOW`` (which would also refuse /var -> /private/var
ancestors), so the store is checked by descriptor first and the connection
then confirms which file it opened.

The ``.pm`` directory and its ancestors are trusted: a symlinked ``.pm`` keeps
working, as the rest of pmlens does.
"""

from __future__ import annotations

import os
import sqlite3
import stat
from pathlib import Path
from urllib.parse import quote

from .utils import FileRefusedError, _no_dir_fd, open_trusted_dir

_SIDECARS = ("-wal", "-shm", "-journal")
_NOFOLLOW = getattr(os, "O_NOFOLLOW", 0)
_CLOEXEC = getattr(os, "O_CLOEXEC", 0)
# SQLITE_DEFAULT_FILE_PERMISSIONS: the mode sqlite3.connect created stores
# with before they were pre-created here; -wal / -shm take the store's mode.
_DB_MODE = 0o644


def _lstat_at(name: str, dir_fd: int | None, directory: Path) -> os.stat_result | None:
    try:
        if dir_fd is None:
            return os.lstat(directory / name)
        return os.stat(name, dir_fd=dir_fd, follow_symlinks=False)
    except FileNotFoundError:
        return None


def _refuse_unless_regular(info: os.stat_result, what: str) -> None:
    if stat.S_ISLNK(info.st_mode):
        raise FileRefusedError(f"{what} is a symlink")
    if not stat.S_ISREG(info.st_mode):
        raise FileRefusedError(f"{what} is not a regular file")


def _file_uri(target: Path) -> str:
    """A ``file:`` URI with an empty authority for an absolute path.

    ``Path.as_uri`` puts a UNC host (a mapped network drive resolves to
    ``\\\\server\\share``) in the authority, which SQLite rejects.
    """
    path = target.as_posix()
    if not path.startswith("/"):
        path = "/" + path  # C:/x -> /C:/x
    return "file://" + quote(path, safe="/:")


def refusal_message(path: Path, exc: FileRefusedError) -> str:
    """A content-free message for a store ``connect_store`` refused."""
    reason = str(exc)
    if "symlink" in reason or "not a regular file" in reason:
        return (
            f"refusing to open .pm/{path.name}: {reason}; pmlens does not open symlinks "
            "inside .pm (to move or share the data, make .pm itself the symlink)"
        )
    return f"could not open .pm/{path.name}: {reason}"


def connect_store(
    path: Path, *, readonly: bool = False, create: bool = True
) -> tuple[sqlite3.Connection, bool] | None:
    """Open ``path`` with sqlite3, refusing a symlink at the file or its sidecars.

    Args:
        path: The database file (its directory is trusted).
        readonly: Open with ``mode=ro&immutable=1`` (Lens): never creates
            anything, sidecars included.
        create: Create the file when it is missing (ignored when read-only).

    Returns:
        The connection and whether this call created the file, or None when
        the file is missing and nothing may be created.

    Raises:
        FileRefusedError: The file, or a ``-wal`` / ``-shm`` / ``-journal``
            next to it, is a symlink or not a regular file, or SQLite opened a
            different file than the one checked.
        sqlite3.Error: SQLite could not open it.
    """
    directory = path.parent.resolve()
    name = path.name
    target = directory / name
    created = False
    dir_fd = None if _no_dir_fd() else open_trusted_dir(directory)
    try:
        info = _lstat_at(name, dir_fd, directory)
        if info is None:
            if readonly or not create:
                return None
            flags = os.O_RDWR | os.O_CREAT | os.O_EXCL | _NOFOLLOW | _CLOEXEC
            try:
                if dir_fd is None:  # pragma: no cover - Windows
                    fd = os.open(target, flags, _DB_MODE)
                else:
                    fd = os.open(name, flags, _DB_MODE, dir_fd=dir_fd)
                os.close(fd)
                created = True
            except FileExistsError:
                info = _lstat_at(name, dir_fd, directory)
                if info is None:
                    raise FileRefusedError(
                        "the database file changed while it was being opened"
                    ) from None
        if info is not None:
            _refuse_unless_regular(info, "the database file")
        for suffix in _SIDECARS:
            sidecar = _lstat_at(name + suffix, dir_fd, directory)
            if sidecar is not None:
                _refuse_unless_regular(sidecar, f"its {suffix} file")
    finally:
        if dir_fd is not None:
            os.close(dir_fd)
    mode = "?mode=ro&immutable=1" if readonly else "?mode=rw"
    conn = sqlite3.connect(_file_uri(target) + mode, uri=True, check_same_thread=False)
    try:
        # mode=rw never creates, so a link swapped in for a missing target fails
        # to open; a link to an existing file is caught here, before any write.
        row = conn.execute("PRAGMA database_list").fetchone()
        opened = row[2] if row else ""
        if opened and os.path.normcase(opened) != os.path.normcase(str(target)):
            raise FileRefusedError("the database file changed while it was being opened")
    except BaseException:
        conn.close()
        raise
    return conn, created
