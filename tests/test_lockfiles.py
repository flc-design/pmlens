"""Lock synchronization and CI wiring regressions (PMSERV-186).

These tests exercise file preservation, error exits and the explicit upgrade
policy without depending on PyPI releases. The CI guard itself uses real uv
against the committed project, and the locked job installs its hashed export.
"""

from __future__ import annotations

import importlib.util
import subprocess
from pathlib import Path

import pytest
import yaml

REPO_ROOT = Path(__file__).resolve().parents[1]
EXPORTED = "sample==1.0 \\\n    --hash=sha256:" + "a" * 64 + "\n"


@pytest.fixture
def guard():
    spec = importlib.util.spec_from_file_location("lockfiles", REPO_ROOT / "scripts/lockfiles.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def project(tmp_path: Path) -> Path:
    root = tmp_path / "repo"
    root.mkdir()
    (root / "pyproject.toml").write_text('[project]\nname = "sample-project"\n')
    (root / "uv.lock").write_text("existing resolution\n")
    return root


@pytest.fixture
def uv_calls(guard, monkeypatch) -> list[list[str]]:
    calls = []

    def run(uv, args, root):
        calls.append(args)
        if args == ["--version"]:
            return f"uv {guard.UV_VERSION} (test build)\n"
        if args[0] == "export":
            return EXPORTED
        assert args[0] == "lock"
        return ""

    monkeypatch.setattr(guard, "_run", run)
    return calls


def _snapshot(root: Path) -> dict[str, bytes]:
    return {p.name: p.read_bytes() for p in root.iterdir()}


def test_matching_check_preserves_all_files(guard, project, uv_calls) -> None:
    (project / "requirements.lock").write_text(guard.HEADER + EXPORTED)
    before = _snapshot(project)
    assert guard.main(["check"], root=project) == 0
    assert _snapshot(project) == before
    # A checker must not silently turn into an updater or depend on PyPI/cache.
    assert not any(args[0] == "lock" for args in uv_calls)
    export = next(args for args in uv_calls if args[0] == "export")
    assert {"--locked", "--offline", "--no-cache"} <= set(export)
    assert "--frozen" not in export  # would bypass the project metadata check


@pytest.mark.parametrize("defect", ["version", "hash", "missing-hash", "missing-file"])
def test_check_rejects_stale_or_unhashed_export(
    guard, project, uv_calls, capsys, defect: str
) -> None:
    content = guard.HEADER + EXPORTED
    if defect == "version":
        content = content.replace("sample==1.0", "sample==0.9")
    elif defect == "hash":
        content = content.replace("a" * 64, "b" * 64)
    elif defect == "missing-hash":
        content = guard.HEADER + "sample==1.0\n"
    if defect != "missing-file":
        (project / "requirements.lock").write_text(content)
    before = _snapshot(project)
    assert guard.main(["check"], root=project) == 1
    assert "lockfiles.py sync" in capsys.readouterr().err
    assert _snapshot(project) == before


def test_sync_preserves_compatible_pins_and_writes_complete_export(
    guard, project, uv_calls
) -> None:
    (project / "requirements.lock").write_text("stale export\n")
    assert guard.main(["sync"], root=project) == 0
    assert (project / "requirements.lock").read_text() == guard.HEADER + EXPORTED
    lock = next(args for args in uv_calls if args[0] == "lock")
    assert "--upgrade" not in lock and "--upgrade-package" not in lock
    export = next(args for args in uv_calls if args[0] == "export")
    assert {"--all-extras", "--all-groups", "--no-emit-project"} <= set(export)
    assert "--no-hashes" not in export


@pytest.mark.parametrize("packages", [[], ["fastmcp==4.0.3", "pip"]])
def test_refresh_explicitly_discards_old_version_preferences(
    guard, project, uv_calls, packages: list[str]
) -> None:
    args = ["refresh"]
    for package in packages:
        args.extend(["--package", package])
    assert guard.main(args, root=project) == 0
    lock = next(args for args in uv_calls if args[0] == "lock")
    if packages:
        assert "--upgrade" not in lock
        upgraded = [lock[i + 1] for i, value in enumerate(lock) if value == "--upgrade-package"]
        assert upgraded == packages
    else:
        assert "--upgrade" in lock  # PMSERV-185: omitting this silently preserved stale pins.
    assert (project / "requirements.lock").read_text() == guard.HEADER + EXPORTED


@pytest.mark.parametrize("command", ["check", "sync", "refresh"])
def test_failed_export_never_overwrites_requirements(
    guard, project, uv_calls, monkeypatch, capsys, command: str
) -> None:
    (project / "requirements.lock").write_text("previous good export\n")

    def failed_export(*args):
        raise subprocess.CalledProcessError(2, ["uv", "export"], stderr="lock is stale")

    monkeypatch.setattr(guard, "_export", failed_export)
    assert guard.main([command], root=project) == 1
    assert "lock is stale" in capsys.readouterr().err
    assert (project / "requirements.lock").read_text() == "previous good export\n"


def test_wrong_uv_version_fails_before_changing_files(guard, project, monkeypatch, capsys) -> None:
    monkeypatch.setattr(guard, "_run", lambda *args: "uv 0.0.1\n")
    before = _snapshot(project)
    assert guard.main(["refresh"], root=project) == 1
    assert f"Use uv=={guard.UV_VERSION}" in capsys.readouterr().err
    assert _snapshot(project) == before


def test_missing_uv_fails_with_an_actionable_exit(guard, project, monkeypatch, capsys) -> None:
    def missing(*args):
        raise FileNotFoundError("uv was not found")

    monkeypatch.setattr(guard, "_run", missing)
    assert guard.main(["check"], root=project) == 1
    assert "uv was not found" in capsys.readouterr().err


def _jobs(filename: str) -> dict:
    return yaml.safe_load((REPO_ROOT / ".github/workflows" / filename).read_text())["jobs"]


def test_real_checker_is_mandatory_in_ci_and_before_release(guard) -> None:
    ci, release = _jobs("ci.yml"), _jobs("release.yml")
    for job in (ci["lockfile-freshness"], release["verify"]):
        assert "if" not in job and not job.get("continue-on-error", False)
        steps = job["steps"]
        checks = [s for s in steps if s.get("run") == "python scripts/lockfiles.py check"]
        assert len(checks) == 1
        assert "if" not in checks[0] and not checks[0].get("continue-on-error", False)
        installs = [s for s in steps if s.get("run") == f"pip install uv=={guard.UV_VERSION}"]
        assert len(installs) == 1
        assert "if" not in installs[0] and not installs[0].get("continue-on-error", False)
        assert steps.index(installs[0]) < steps.index(checks[0])
    assert ci["test-locked"]["needs"] == "lockfile-freshness"
    assert release["build"]["needs"] == "verify"
    assert f"uv=={guard.UV_VERSION}" in (REPO_ROOT / ".devcontainer/Dockerfile").read_text()


def test_hash_install_is_followed_by_constraint_check() -> None:
    job = _jobs("ci.yml")["test-locked"]
    commands = [
        s.get("run") for s in job["steps"] if "if" not in s and not s.get("continue-on-error")
    ]
    assert (
        commands.index("pip install --require-hashes -r requirements.lock")
        < commands.index("pip install -e . --no-deps")
        < commands.index("pip check")
        < commands.index("pytest -q")
    )
