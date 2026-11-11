"""``.pm/.gitignore``: a user's repository must not stage pmlens's databases.

The README offers ``.pm/`` as something to track in git, but ``pm_init`` wrote
no ignore rules, so ``git add .`` staged ``memory.db`` with its sidecars and,
once the content pipeline had been used, ``drafts.db``, which holds the text
before redaction. pmlens's own repository ignores ``.pm/`` wholesale, which is
why tests/test_drafts_gitignore.py (it checks only this checkout) never saw it.
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
from pathlib import Path

import pytest

import pmlens.server as srv
from pmlens.storage import PM_GITIGNORE_ENTRIES, ensure_pm_gitignore

# Assembled at runtime so no secret-shaped literal is in the source (fake data).
_FAKE_KEY = "sk-test-" + "EXAMPLE" + "0" * 12

_IGNORED = [
    "drafts.db",
    "drafts.db-wal",
    "drafts.db-shm",
    "drafts.db-journal",
    "x_drafts.db",
    "x_drafts.db-wal",
    "x_drafts.db-shm",
    "memory.db",
    "memory.db-wal",
    "memory.db-shm",
    "some_future.db-wal",
    "some_future.db-shm",
    ".locks/tasks.yaml.lock",
]
_TRACKABLE = ["project.yaml", "tasks.yaml", "decisions.yaml", "daily/2026-11-11.yaml", ".gitignore"]


def _init(tmp_path: Path, name: str = "proj") -> Path:
    root = tmp_path / name
    root.mkdir()
    result = srv.pm_init(project_path=str(root), project_name=name)
    assert result["status"] == "initialized"
    return root


def _entries(pm_path: Path) -> list[str]:
    return sorted(entry.name for entry in pm_path.iterdir())


def _codes(result: dict) -> list[str]:
    return [warning["code"] for warning in result.get("warnings", [])]


def _git(root: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", *args], cwd=root, capture_output=True, text=True, timeout=30, check=False
    )


# ─── pm_init seeds the file ───────────────────────────────────────────────


def test_pm_init_seeds_gitignore_for_a_new_project(tmp_path: Path) -> None:
    root = tmp_path / "fresh"
    root.mkdir()

    result = srv.pm_init(project_path=str(root), project_name="fresh")

    assert result["gitignore"] == "created .pm/.gitignore"
    text = (root / ".pm" / ".gitignore").read_text(encoding="utf-8")
    rules = [line for line in text.splitlines() if line and not line.startswith("#")]
    assert rules == list(PM_GITIGNORE_ENTRIES)
    assert set(rules) >= {"drafts.db*", "x_drafts.db*", "memory.db*", "*.db-wal", "*.db-shm"}
    assert ".locks/" in rules
    comment = " ".join(line.lstrip("# ") for line in text.splitlines() if line.startswith("#"))
    assert "before redaction" in comment
    assert "Remove the next line to track memory.db" in comment


def test_an_existing_gitignore_is_never_overwritten(tmp_path: Path) -> None:
    root = tmp_path / "mine"
    (root / ".pm").mkdir(parents=True)
    own = "# my own rules\n*.bak\n"
    (root / ".pm" / ".gitignore").write_text(own, encoding="utf-8")

    first = srv.pm_init(project_path=str(root), project_name="mine")
    again = srv.pm_init(project_path=str(root))

    assert (root / ".pm" / ".gitignore").read_text(encoding="utf-8") == own
    assert first["gitignore"] == ".pm/.gitignore already exists (not changed)"
    assert again["gitignore"] == ".pm/.gitignore already exists (not changed)"


def test_pm_init_again_adds_the_file_to_an_existing_project(tmp_path: Path) -> None:
    root = _init(tmp_path)
    gitignore = root / ".pm" / ".gitignore"
    gitignore.unlink()  # a project created before pmlens seeded the file
    srv.pm_remember(content="kept across the second init", project_path=str(root))

    result = srv.pm_init(project_path=str(root))

    assert result["gitignore"] == "created .pm/.gitignore"
    assert gitignore.is_file()
    assert "pm_gitignore_missing" not in _codes(srv.pm_status(project_path=str(root)))


@pytest.mark.skipif(
    hasattr(os, "geteuid") and os.geteuid() == 0, reason="permission bits do not bind root"
)
def test_pm_init_again_still_returns_when_the_file_cannot_be_created(tmp_path: Path) -> None:
    """A second init on a ``.pm`` that is not writable returned before the file existed."""
    root = _init(tmp_path)
    pm_path = root / ".pm"
    (pm_path / ".gitignore").unlink()
    before = _entries(pm_path)
    pm_path.chmod(0o555)
    try:
        result = srv.pm_init(project_path=str(root))
        after = _entries(pm_path)
    finally:
        pm_path.chmod(0o755)

    assert result["status"] == "initialized"
    assert result["gitignore"] == "could not create .pm/.gitignore"
    assert after == before
    assert ".gitignore" not in after
    [warning] = [w for w in result["warnings"] if w["code"] == "pm_gitignore_not_created"]
    assert set(warning) == {"level", "code", "message", "remediation"}
    assert warning["level"] == "warning"
    assert "draft database" in warning["message"]
    assert warning["remediation"].splitlines()[1:] == list(PM_GITIGNORE_ENTRIES)


def test_a_symlink_at_the_name_is_not_written_through(tmp_path: Path) -> None:
    root = tmp_path / "linked"
    (root / ".pm").mkdir(parents=True)
    outside = tmp_path / "global_excludes"
    (root / ".pm" / ".gitignore").symlink_to(outside)  # dangling

    result = srv.pm_init(project_path=str(root), project_name="linked")

    assert not outside.exists()
    assert (root / ".pm" / ".gitignore").is_symlink()
    assert result["gitignore"] == ".pm/.gitignore already exists (not changed)"
    [linked] = [w for w in result["warnings"] if w["code"] == "symlinks_in_pm_refused"]
    assert ".gitignore" in linked["message"]


def test_ensure_pm_gitignore_reports_whether_it_created_the_file(tmp_path: Path) -> None:
    pm_path = tmp_path / ".pm"
    pm_path.mkdir()
    assert ensure_pm_gitignore(pm_path) is True
    created = (pm_path / ".gitignore").read_bytes()
    assert ensure_pm_gitignore(pm_path) is False
    assert (pm_path / ".gitignore").read_bytes() == created
    assert _entries(pm_path) == [".gitignore"]


# ─── git really leaves the databases out ──────────────────────────────────


@pytest.mark.skipif(shutil.which("git") is None, reason="git is not installed")
def test_git_stages_no_database_from_a_project_pm_init_created(tmp_path: Path) -> None:
    root = tmp_path / "repo"
    root.mkdir()
    assert _git(root, "init", "-q").returncode == 0
    srv.pm_init(project_path=str(root), project_name="repo")
    srv.pm_remember(content="a memory", project_path=str(root))
    staged = srv.pm_draft_content(
        signal_type="lesson",
        source_refs=["memory:1"],
        raw_content=f"the key was {_FAKE_KEY}",
        hook="a hook",
        project_path=str(root),
    )
    assert staged.get("status") != "error", staged
    pm_path = root / ".pm"
    # The unredacted text really is in the database git must not pick up.
    assert _FAKE_KEY.encode() in b"".join(
        path.read_bytes() for path in pm_path.glob("drafts.db*") if path.is_file()
    )
    assert (pm_path / "memory.db").is_file()

    listed = _git(root, "add", "--dry-run", ".")
    assert listed.returncode == 0, listed.stderr
    would_add = re.findall(r"^add '(.+)'$", listed.stdout, flags=re.MULTILINE)

    assert ".pm/project.yaml" in would_add
    assert ".pm/.gitignore" in would_add
    leaked = [path for path in would_add if ".db" in path or "/.locks/" in path]
    assert not leaked, f"git would stage {leaked}"


@pytest.mark.skipif(shutil.which("git") is None, reason="git is not installed")
def test_git_check_ignore_agrees_name_by_name(tmp_path: Path) -> None:
    root = tmp_path / "repo"
    root.mkdir()
    assert _git(root, "init", "-q").returncode == 0
    srv.pm_init(project_path=str(root), project_name="repo")

    for name in _IGNORED:
        assert _git(root, "check-ignore", "-q", f".pm/{name}").returncode == 0, name
    for name in _TRACKABLE:
        assert _git(root, "check-ignore", "-q", f".pm/{name}").returncode == 1, name


# ─── pm_status tells an existing project that has no .pm/.gitignore ───────


def _in_git(root: Path) -> Path:
    """Mark ``root`` as a git work tree the way the notice looks for one: a ``.git`` entry."""
    (root / ".git").mkdir()
    return root


def _old_project_with_memory(tmp_path: Path) -> Path:
    root = _in_git(_init(tmp_path, "old"))
    (root / ".pm" / ".gitignore").unlink()
    srv.pm_remember(content="a memory", project_path=str(root))
    return root


@pytest.mark.parametrize("lens", [False, True], ids=["full", "lens"])
def test_pm_status_reports_a_missing_gitignore_next_to_a_database(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, lens: bool
) -> None:
    root = _old_project_with_memory(tmp_path)
    monkeypatch.setattr(srv, "PM_LENS_ENABLED", lens)
    before = _entries(root / ".pm")

    status = srv.pm_status(project_path=str(root))

    [notice] = [w for w in status["warnings"] if w["code"] == "pm_gitignore_missing"]
    assert notice["level"] == "info"
    assert "ignores .pm entirely can disregard" in notice["message"]
    for line in PM_GITIGNORE_ENTRIES:
        assert f"\n{line}" in notice["remediation"]
    # Shown in Lens as well, which cannot write: no tool is named.
    assert not re.search(r"\bpm_[a-z_]+", notice["message"] + notice["remediation"])
    # A status read creates nothing, the missing file included.
    assert _entries(root / ".pm") == before
    assert ".gitignore" not in before


@pytest.mark.parametrize("database", ["drafts.db", "x_drafts.db"])
def test_a_draft_database_alone_is_enough_for_the_notice(tmp_path: Path, database: str) -> None:
    root = _in_git(_init(tmp_path))
    (root / ".pm" / ".gitignore").unlink()
    assert "pm_gitignore_missing" not in _codes(srv.pm_status(project_path=str(root)))

    (root / ".pm" / database).write_bytes(b"")

    assert "pm_gitignore_missing" in _codes(srv.pm_status(project_path=str(root)))


def test_no_notice_without_a_database_or_with_any_gitignore(tmp_path: Path) -> None:
    root = _in_git(_init(tmp_path))
    gitignore = root / ".pm" / ".gitignore"
    # Seeded by pm_init, and nothing but YAML in .pm so far.
    assert "pm_gitignore_missing" not in _codes(srv.pm_status(project_path=str(root)))

    gitignore.unlink()
    assert "pm_gitignore_missing" not in _codes(srv.pm_status(project_path=str(root)))

    srv.pm_remember(content="a memory", project_path=str(root))
    assert "pm_gitignore_missing" in _codes(srv.pm_status(project_path=str(root)))

    # The user's own file counts, whatever is in it: pmlens does not judge it.
    gitignore.write_text("# I track everything on purpose\n", encoding="utf-8")
    assert "pm_gitignore_missing" not in _codes(srv.pm_status(project_path=str(root)))


# The notice repeats on every status read, so it is shown only where git could
# stage the databases. git is never run: a ``.git`` entry and the project's own
# .gitignore are all that is looked at.


def test_no_notice_outside_a_git_work_tree(tmp_path: Path) -> None:
    root = _init(tmp_path, "old")
    (root / ".pm" / ".gitignore").unlink()
    srv.pm_remember(content="a memory", project_path=str(root))

    assert "pm_gitignore_missing" not in _codes(srv.pm_status(project_path=str(root)))


@pytest.mark.parametrize("as_file", [False, True], ids=["directory", "gitfile"])
def test_a_git_entry_above_the_project_counts(tmp_path: Path, as_file: bool) -> None:
    # A monorepo keeps .git above the project; a worktree or submodule has a file.
    (tmp_path / "repo").mkdir()
    if as_file:
        (tmp_path / "repo" / ".git").write_text("gitdir: elsewhere\n", encoding="utf-8")
    else:
        (tmp_path / "repo" / ".git").mkdir()
    (tmp_path / "repo" / "apps").mkdir()
    root = _init(tmp_path / "repo" / "apps", "old")
    (root / ".pm" / ".gitignore").unlink()
    srv.pm_remember(content="a memory", project_path=str(root))

    assert "pm_gitignore_missing" in _codes(srv.pm_status(project_path=str(root)))


@pytest.mark.parametrize("line", [".pm", ".pm/", "/.pm", "/.pm/", ".pm/*", "**/.pm/", "  .pm/  "])
def test_no_notice_when_the_project_ignores_all_of_pm(tmp_path: Path, line: str) -> None:
    root = _old_project_with_memory(tmp_path)
    (root / ".gitignore").write_text(f"# build output\ndist/\n{line}\n", encoding="utf-8")

    assert "pm_gitignore_missing" not in _codes(srv.pm_status(project_path=str(root)))


@pytest.mark.parametrize(
    "text",
    [
        "dist/\n",  # nothing about .pm
        "# .pm/\n",  # a comment
        ".pm/memory.db\n",  # one file is not all of .pm
        ".pmx/\n",  # another name
        ".pm/\n!.pm/tasks.yaml\n",  # re-included afterwards
    ],
    ids=["unrelated", "comment", "one-file", "other-name", "re-included"],
)
def test_the_notice_stays_when_part_of_pm_can_be_staged(tmp_path: Path, text: str) -> None:
    root = _old_project_with_memory(tmp_path)
    (root / ".gitignore").write_text(text, encoding="utf-8")

    assert "pm_gitignore_missing" in _codes(srv.pm_status(project_path=str(root)))


def test_a_gitignore_that_cannot_be_read_keeps_the_notice(tmp_path: Path) -> None:
    root = _old_project_with_memory(tmp_path)
    (tmp_path / "elsewhere").write_text(".pm/\n", encoding="utf-8")

    # A link is not followed, so what it says about .pm is not known.
    (root / ".gitignore").symlink_to(tmp_path / "elsewhere")
    assert "pm_gitignore_missing" in _codes(srv.pm_status(project_path=str(root)))

    (root / ".gitignore").unlink()
    (root / ".gitignore").write_bytes(b".pm/\n" + b"#" * (300 * 1024))
    assert "pm_gitignore_missing" in _codes(srv.pm_status(project_path=str(root)))

    (root / ".gitignore").write_bytes(b".pm/\n\xff\xfe")
    assert "pm_gitignore_missing" in _codes(srv.pm_status(project_path=str(root)))


@pytest.mark.skipif(not hasattr(os, "mkfifo"), reason="needs a FIFO")
def test_a_fifo_named_gitignore_does_not_block_the_status_read(tmp_path: Path) -> None:
    import threading

    root = _old_project_with_memory(tmp_path)
    os.mkfifo(root / ".gitignore")
    codes: list[str] = []
    reader = threading.Thread(
        target=lambda: codes.extend(_codes(srv.pm_status(project_path=str(root)))), daemon=True
    )
    reader.start()
    reader.join(timeout=10)

    assert not reader.is_alive()
    assert "pm_gitignore_missing" in codes


# ─── A listing call does not create a draft database ──────────────────────


@pytest.mark.parametrize("tool", ["pm_drafts_pending", "pm_x_drafts_pending"])
def test_listing_drafts_creates_no_database(tmp_path: Path, tool: str) -> None:
    root = _init(tmp_path)
    before = _entries(root / ".pm")

    listed = getattr(srv, tool)(project_path=str(root))
    paged = getattr(srv, tool)(filter_status="all", limit=10, offset=5, project_path=str(root))

    assert listed == {"status": "ok", "items": [], "total": 0, "has_more": False, "next_offset": 0}
    assert paged == {"status": "ok", "items": [], "total": 0, "has_more": False, "next_offset": 5}
    assert _entries(root / ".pm") == before
    assert not list((root / ".pm").glob("*drafts.db*"))


def test_the_first_draft_still_creates_the_database_and_is_listed(tmp_path: Path) -> None:
    root = _init(tmp_path)
    srv.pm_drafts_pending(project_path=str(root))

    staged = srv.pm_draft_content(
        signal_type="lesson",
        source_refs=["memory:1"],
        raw_content="raw",
        hook="a hook",
        project_path=str(root),
    )

    assert staged.get("status") != "error", staged
    assert (root / ".pm" / "drafts.db").is_file()
    assert srv.pm_drafts_pending(filter_status="all", project_path=str(root))["total"] == 1
    assert srv.pm_x_drafts_pending(filter_status="all", project_path=str(root))["total"] == 1
