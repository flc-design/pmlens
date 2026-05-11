"""End-to-end CLI smoke tests (PMSERV-056).

These tests invoke `python -m pm_server <subcmd>` as a subprocess and
assert structural UX properties of the rendered output. They are the
PMSERV-039 regression guard: independent unit and CLI tests passed
while the real CLI emitted a duplicated `[dry-run]` tag in the wild
because the responsibility was layered in two places.

Marked with `@pytest.mark.smoke` so they can be selected or skipped:
    pytest -m smoke         # only smoke
    pytest -m "not smoke"   # skip smoke

The marker is registered in pyproject.toml. Default `pytest` includes
these. All subprocess invocations use `--dry-run` so no filesystem
side effects can occur on the host running the tests.
"""

from __future__ import annotations

import subprocess
import sys

import pytest

pytestmark = pytest.mark.smoke


def _run_cli(*args: str, timeout: float = 30.0) -> subprocess.CompletedProcess[str]:
    """Invoke `python -m pm_server <args>` and capture stdout/stderr."""
    return subprocess.run(
        [sys.executable, "-m", "pm_server", *args],
        capture_output=True,
        text=True,
        timeout=timeout,
    )


def _assert_no_duplicate_dry_run_tags(output: str) -> None:
    """No output line may contain more than one `[dry-run]` tag.

    PMSERV-039 regression: the bug produced lines like
    `✓ [dry-run] claude-code: [dry-run] would register PM Server...`
    where the tag appeared twice because both the renderer and the
    message constructor added it. The fix made the CLI renderer
    (__main__.py::_print_install_summary, _print_inject_summary) the
    single source of truth — message-layer never embeds the tag.
    """
    for line in output.splitlines():
        assert line.count("[dry-run]") <= 1, f"Duplicate [dry-run] tag in output line: {line!r}"


class TestInstallDryRunSmoke:
    """`pm-server install --dry-run` UX structural checks."""

    def test_install_dry_run_target_claude_code(self):
        result = _run_cli("install", "--target", "claude-code", "--dry-run")
        assert result.returncode in (0, 1), (
            f"unexpected exit {result.returncode}, stderr={result.stderr!r}"
        )
        _assert_no_duplicate_dry_run_tags(result.stdout)
        assert "[dry-run]" in result.stdout, "expected --dry-run tag in output"
        assert "claude-code" in result.stdout

    def test_install_dry_run_target_codex(self):
        result = _run_cli("install", "--target", "codex", "--dry-run")
        assert result.returncode in (0, 1), (
            f"unexpected exit {result.returncode}, stderr={result.stderr!r}"
        )
        _assert_no_duplicate_dry_run_tags(result.stdout)
        assert "[dry-run]" in result.stdout
        assert "codex" in result.stdout

    def test_install_dry_run_target_all(self):
        """The exact reproduction case from PMSERV-039.

        With `--target all`, both hosts appear as separate output
        lines. The original bug doubled the tag on every line.
        """
        result = _run_cli("install", "--target", "all", "--dry-run")
        assert result.returncode in (0, 1), (
            f"unexpected exit {result.returncode}, stderr={result.stderr!r}"
        )
        _assert_no_duplicate_dry_run_tags(result.stdout)
        assert "[dry-run]" in result.stdout
        assert "claude-code" in result.stdout
        assert "codex" in result.stdout


class TestUninstallDryRunSmoke:
    """`pm-server uninstall --dry-run` UX structural checks."""

    def test_uninstall_dry_run_target_all(self):
        result = _run_cli("uninstall", "--target", "all", "--dry-run")
        assert result.returncode in (0, 1), (
            f"unexpected exit {result.returncode}, stderr={result.stderr!r}"
        )
        _assert_no_duplicate_dry_run_tags(result.stdout)
        assert "[dry-run]" in result.stdout


class TestCliSanity:
    """Basic CLI startup sanity. Catches gross regressions like import errors."""

    def test_help_exits_zero(self):
        result = _run_cli("--help")
        assert result.returncode == 0, f"stderr={result.stderr!r}"
        assert "install" in result.stdout
        assert "uninstall" in result.stdout

    def test_install_help_advertises_options(self):
        result = _run_cli("install", "--help")
        assert result.returncode == 0, f"stderr={result.stderr!r}"
        assert "--dry-run" in result.stdout
        assert "--target" in result.stdout
