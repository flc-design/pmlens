"""x_drafts.db secret-at-rest gitignore guard (PMSERV-120, ADR-024).

x_drafts.db holds raw_content (the unscrubbed concentrate). It is covered by
the blanket .pm/ ignore, but pm-server explicitly ignores it too so that users
who choose to COMMIT .pm/ (the portability path ADR-023 anticipates) still keep
the concentrate out of git. This test fails if that defensive entry is dropped.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).resolve().parent.parent


def test_x_drafts_db_is_gitignored() -> None:
    if not (_REPO_ROOT / ".git").exists():
        pytest.skip("not a git checkout")
    result = subprocess.run(
        ["git", "check-ignore", ".pm/x_drafts.db"],
        cwd=_REPO_ROOT,
        capture_output=True,
        text=True,
        timeout=10,
    )
    # returncode 0 → the path is ignored.
    assert result.returncode == 0, "x_drafts.db is not gitignored — raw_content could be committed"
    assert ".pm/x_drafts.db" in result.stdout


def test_x_drafts_db_explicit_entry_present() -> None:
    """Belt-and-suspenders: the explicit entry must survive even if .pm/ is
    later un-ignored (the ADR-023 commit-.pm portability case)."""
    gitignore = (_REPO_ROOT / ".gitignore").read_text(encoding="utf-8")
    assert ".pm/x_drafts.db" in gitignore
