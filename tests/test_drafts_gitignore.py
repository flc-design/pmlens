"""Draft database secret-at-rest gitignore guard (PMSERV-120, ADR-024).

drafts.db and legacy x_drafts.db hold raw_content (the unscrubbed concentrate),
including in their WAL/SHM sidecars. They are covered by
the blanket .pm/ ignore, but pm-server explicitly ignores it too so that users
who choose to COMMIT .pm/ (the portability path ADR-023 anticipates) still keep
the concentrate out of git. This test fails if that defensive entry is dropped.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).resolve().parent.parent


@pytest.mark.parametrize("filename", ["drafts.db", "x_drafts.db"])
@pytest.mark.parametrize("suffix", ["", "-wal", "-shm"])
def test_draft_db_is_gitignored(filename: str, suffix: str) -> None:
    if not (_REPO_ROOT / ".git").exists():
        pytest.skip("not a git checkout")
    path = f".pm/{filename}{suffix}"
    result = subprocess.run(
        ["git", "check-ignore", path],
        cwd=_REPO_ROOT,
        capture_output=True,
        text=True,
        timeout=10,
    )
    # returncode 0 → the path is ignored.
    assert result.returncode == 0, f"{path} is not gitignored — raw_content could be committed"
    assert path in result.stdout


@pytest.mark.parametrize("filename", ["drafts.db", "x_drafts.db"])
@pytest.mark.parametrize("suffix", ["", "-wal", "-shm"])
def test_draft_db_explicit_entry_present(filename: str, suffix: str) -> None:
    """Belt-and-suspenders: the explicit entry must survive even if .pm/ is
    later un-ignored (the ADR-023 commit-.pm portability case)."""
    gitignore = (_REPO_ROOT / ".gitignore").read_text(encoding="utf-8")
    assert f".pm/{filename}{suffix}" in gitignore.splitlines()
