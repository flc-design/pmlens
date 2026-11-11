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
from pmlens.storage import PM_GITIGNORE_ENTRIES, _git_could_stage_pm, ensure_pm_gitignore

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
    """Run git in ``root`` with nothing outside the repository deciding the answer.

    No global or system configuration, no global ignore file (git reads
    ``$XDG_CONFIG_HOME/git/ignore`` even with the global configuration switched
    off, so it is named here), and none of the ``GIT_*`` variables a hook
    passes on, such as its index.
    """
    env = {name: value for name, value in os.environ.items() if not name.startswith("GIT_")}
    env |= {"GIT_CONFIG_GLOBAL": os.devnull, "GIT_CONFIG_NOSYSTEM": "1"}
    return subprocess.run(
        ["git", "-c", f"core.excludesFile={os.devnull}", *args],
        cwd=root,
        env=env,
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
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


def _needs_a_place_outside_git(tmp_path: Path) -> None:
    """Skip when pytest's own temporary directory is inside a git work tree (``--basetemp``)."""
    if any(os.path.lexists(directory / ".git") for directory in (tmp_path, *tmp_path.parents)):
        pytest.skip("tmp_path is inside a git work tree")


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
    _needs_a_place_outside_git(tmp_path)
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


@pytest.mark.parametrize(
    "line", [".pm", ".pm/", "/.pm", "/.pm/", ".pm/*", "**/.pm/", ".pm/  ", ".pm/\r"]
)
def test_no_notice_when_the_project_ignores_all_of_pm(tmp_path: Path, line: str) -> None:
    # Trailing spaces and a CRLF line ending are not part of the pattern for git.
    root = _old_project_with_memory(tmp_path)
    (root / ".gitignore").write_bytes(f"# build output\ndist/\n{line}\n".encode())

    assert "pm_gitignore_missing" not in _codes(srv.pm_status(project_path=str(root)))


@pytest.mark.parametrize(
    "text",
    [
        "dist/\n",  # nothing about .pm
        "# .pm/\n",  # a comment
        ".pm/memory.db\n",  # one file is not all of .pm
        ".pmx/\n",  # another name
        ".pm/\n!.pm/tasks.yaml\n",  # re-included afterwards
        ".pm/\n!*\n",  # a re-include need not name .pm
        ".pm/*\n!*.db\n",  # the databases themselves, re-included
        "  .pm/\n",  # leading spaces are part of the pattern
        ".pm/\t\n",  # only trailing spaces are trimmed, not a tab
        "dist/\r.pm/\rbuild/\n",  # a lone CR ends no line: one pattern
        "x\x0c.pm/\n",  # nor does a form feed
    ],
    ids=[
        "unrelated",
        "comment",
        "one-file",
        "other-name",
        "re-included",
        "re-included-by-any-pattern",
        "databases-re-included",
        "leading-spaces",
        "trailing-tab",
        "cr-only",
        "form-feed",
    ],
)
def test_the_notice_stays_when_part_of_pm_can_be_staged(tmp_path: Path, text: str) -> None:
    root = _old_project_with_memory(tmp_path)
    (root / ".gitignore").write_bytes(text.encode())

    assert "pm_gitignore_missing" in _codes(srv.pm_status(project_path=str(root)))


@pytest.mark.parametrize("in_git", [True, False], ids=["in-git", "outside-git"])
def test_a_pm_that_is_a_link_keeps_the_notice(tmp_path: Path, in_git: bool) -> None:
    # Its files live where the link points, which may be another repository:
    # neither this project's .git nor its .gitignore says anything about them.
    root = _init(tmp_path, "old")
    (root / ".pm" / ".gitignore").unlink()
    srv.pm_remember(content="a memory", project_path=str(root))
    notes = tmp_path / "notes"
    notes.mkdir()
    (root / ".pm").rename(notes / "old")
    (root / ".pm").symlink_to(notes / "old")
    if in_git:
        _in_git(root)
    (root / ".gitignore").write_text(".pm\n", encoding="utf-8")

    assert _git_could_stage_pm(root / ".pm") is True
    assert "pm_gitignore_missing" in _codes(srv.pm_status(project_path=str(root)))


@pytest.mark.parametrize("as_file", [False, True], ids=["directory", "gitfile"])
@pytest.mark.parametrize("root_in_git", [True, False], ids=["root-ignores-pm", "root-outside-git"])
def test_a_pm_that_is_a_repository_of_its_own_keeps_the_notice(
    tmp_path: Path, as_file: bool, root_in_git: bool
) -> None:
    # Ignored by the public repository and versioned privately: `git add` run
    # inside .pm stages the databases, and the project's .gitignore has no say.
    root = _init(tmp_path, "old")
    (root / ".pm" / ".gitignore").unlink()
    srv.pm_remember(content="a memory", project_path=str(root))
    if root_in_git:
        _in_git(root)
        (root / ".gitignore").write_text(".pm/\n", encoding="utf-8")
    if as_file:
        (root / ".pm" / ".git").write_text("gitdir: elsewhere\n", encoding="utf-8")
    else:
        (root / ".pm" / ".git").mkdir()

    assert _git_could_stage_pm(root / ".pm") is True
    assert "pm_gitignore_missing" in _codes(srv.pm_status(project_path=str(root)))


@pytest.mark.parametrize("variable", ["GIT_DIR", "GIT_WORK_TREE"])
def test_a_git_location_from_the_environment_keeps_the_notice(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, variable: str
) -> None:
    # No .git entry near the project, yet git works on it with these set.
    _needs_a_place_outside_git(tmp_path)
    root = _init(tmp_path, "old")
    (root / ".pm" / ".gitignore").unlink()
    srv.pm_remember(content="a memory", project_path=str(root))
    assert "pm_gitignore_missing" not in _codes(srv.pm_status(project_path=str(root)))

    monkeypatch.setenv(variable, str(tmp_path / "elsewhere"))

    assert "pm_gitignore_missing" in _codes(srv.pm_status(project_path=str(root)))


@pytest.mark.parametrize("variable", ["GIT_DIR", "GIT_WORK_TREE"])
def test_a_git_location_from_the_environment_outweighs_the_projects_gitignore(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, variable: str
) -> None:
    # The work tree those variables name may be .pm itself, and then the
    # project's .gitignore is not the file git reads.
    root = _old_project_with_memory(tmp_path)
    (root / ".gitignore").write_text(".pm/\n", encoding="utf-8")
    assert "pm_gitignore_missing" not in _codes(srv.pm_status(project_path=str(root)))

    monkeypatch.setenv(variable, str(tmp_path / "elsewhere"))

    assert "pm_gitignore_missing" in _codes(srv.pm_status(project_path=str(root)))


# What git reads from a project's .gitignore, against what the notice assumes.
# Each text is written as the whole file.
_GITIGNORE_TEXTS = [
    "",
    "dist/\n",
    ".pm\n",
    ".pm/\n",
    "/.pm\n",
    "/.pm/\n",
    ".pm/*\n",
    "/.pm/*\n",
    ".pm/**\n",
    "/.pm/**\n",
    "**/.pm\n",
    "**/.pm/\n",
    ".pm/",  # no final newline
    ".pm/  \n",
    ".pm/\r\n",
    "dist/\r\n.pm/\r\nbuild/\r\n",
    ".pm/ # not a comment\n",
    ".pm/\\ \n",  # an escaped trailing space
    "  .pm/  \n",
    " .pm/\n",
    "\t.pm/\n",
    ".pm/\t\n",
    ".pm/\u3000\n",
    ".pm/\u00a0\n",
    ".pm/\x0c\n",
    "dist/\r.pm/\rbuild/\n",
    "x\x0b.pm\n",
    "# s\x0c.pm/\n",
    "x\u2028.pm/\n",
    "x\u0085.pm/\n",
    "x\x1c.pm/\n",
    ".pm/\n!*\n",
    ".pm/\n!.*\n",
    ".pm\n!/.p*\n",
    ".pm/\n!/*\n",
    ".pm/\n!*/\n",
    "**/.pm\n!**\n",
    ".pm\n!.[p]m\n",
    ".pm\n!.??\n",
    ".pm/*\n!*.db\n",
    ".pm/**\n!**/*.db\n",
    ".pm/*\n!drafts.db\n",
    ".pm/*\n!memory.db\n",
    ".pm/*\n!/.p*/drafts.db\n",
    "/.pm/*\n!*\n",
    "!*\n.pm/\n",
    ".pm\n!*\n.pm\n",
    ".pm/\n!.pm/tasks.yaml\n",
    "\ufeff.pm/\n",
    ".PM/\n",
    ".pm/memory.db\n",
    "*.db\n",
]


def _pm_paths_git_would_stage(tmp_path: Path, text: str) -> tuple[Path, list[str]]:
    """A repository with ``text`` as its .gitignore and two databases in .pm.

    Returns its root and the paths under .pm that ``git add --all`` would stage.
    """
    root = tmp_path / "repo"
    (root / ".pm").mkdir(parents=True)
    init = _git(root, "init", "-q")
    assert init.returncode == 0, init.stderr
    for name in ("drafts.db", "memory.db"):
        (root / ".pm" / name).write_bytes(b"sqlite")
    (root / ".gitignore").write_bytes(text.encode())
    listed = _git(root, "add", "--all", "--dry-run")
    assert listed.returncode == 0, listed.stderr
    staged = [
        path
        for path in re.findall(r"^add '(.+)'$", listed.stdout, flags=re.MULTILINE)
        if path.startswith(".pm/")
    ]
    return root, sorted(staged)


@pytest.mark.skipif(shutil.which("git") is None, reason="git is not installed")
@pytest.mark.parametrize("text", _GITIGNORE_TEXTS, ids=[repr(t) for t in _GITIGNORE_TEXTS])
def test_the_notice_is_never_withheld_where_git_would_stage_a_database(
    tmp_path: Path, text: str
) -> None:
    """The unsafe direction only: "git cannot stage it" must be true whenever it is claimed.

    The other direction (a notice git would not have needed) is allowed: what
    this function cannot tell counts as "could be staged".
    """
    root, staged = _pm_paths_git_would_stage(tmp_path, text)

    if not _git_could_stage_pm(root / ".pm"):
        assert not staged, f"the notice is withheld, but git would stage {staged}"


@pytest.mark.skipif(shutil.which("git") is None, reason="git is not installed")
@pytest.mark.parametrize("text", ["", "dist/\n", "  .pm/\n", ".pm/\n!*\n"])
def test_the_git_comparison_really_sees_git_stage_the_databases(tmp_path: Path, text: str) -> None:
    # If something outside the repository ignored *.db, git would stage nothing
    # for any text and the comparison above would pass whatever the function says.
    _, staged = _pm_paths_git_would_stage(tmp_path, text)

    assert staged == [".pm/drafts.db", ".pm/memory.db"]


def test_the_git_comparison_covers_both_answers(tmp_path: Path) -> None:
    # Or the test above could pass by the function always answering "could be staged".
    answers = set()
    for index, text in enumerate(_GITIGNORE_TEXTS):
        root = tmp_path / f"repo{index}"
        (root / ".pm").mkdir(parents=True)
        (root / ".git").mkdir()
        (root / ".gitignore").write_bytes(text.encode())
        answers.add(_git_could_stage_pm(root / ".pm"))
    assert answers == {True, False}


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
