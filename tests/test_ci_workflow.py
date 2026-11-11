"""What the CI workflow has to keep testing (PMSERV-297).

pmlens is installed with pipx / uvx against whatever its dependencies released
last, on any Python that requires-python allows. These tests keep the three
things that make CI see that: a run that does not wait for a push, a matrix
that covers every Python the package claims, and a job at the declared floors.
"""

from __future__ import annotations

import re
import tomllib
from pathlib import Path
from typing import Any

import pytest
import yaml

REPO_ROOT = Path(__file__).resolve().parents[1]
WORKFLOWS = REPO_ROOT / ".github" / "workflows"
_PINNED_ACTION = re.compile(r"[\w.-]+/[\w./-]+@[0-9a-f]{40}")
_CLASSIFIER = "Programming Language :: Python :: "


def _workflow(filename: str) -> dict[Any, Any]:
    return yaml.safe_load((WORKFLOWS / filename).read_text(encoding="utf-8"))


def _triggers(workflow: dict[Any, Any]) -> dict[str, Any]:
    # YAML 1.1 reads the key `on` as the boolean true.
    return workflow[True] if True in workflow else workflow["on"]


def _project() -> dict[str, Any]:
    return tomllib.loads((REPO_ROOT / "pyproject.toml").read_text(encoding="utf-8"))["project"]


def _uv_version() -> str:
    source = (REPO_ROOT / "scripts" / "lockfiles.py").read_text(encoding="utf-8")
    found = re.search(r'^UV_VERSION = "([^"]+)"$', source, re.MULTILINE)
    assert found is not None
    return found.group(1)


def _minor(version: str) -> tuple[int, int]:
    major, minor = version.split(".")
    return int(major), int(minor)


def test_ci_also_runs_weekly_and_on_demand() -> None:
    triggers = _triggers(_workflow("ci.yml"))
    assert triggers["push"] == {"branches": ["main"]}
    assert triggers["pull_request"] == {"branches": ["main"]}
    assert "workflow_dispatch" in triggers
    schedule = triggers["schedule"]
    assert len(schedule) == 1
    minute, hour, day_of_month, month, day_of_week = schedule[0]["cron"].split()
    # Weekly: one fixed minute and hour on one day of the week.
    assert minute.isdigit() and hour.isdigit() and day_of_week.isdigit()
    assert (day_of_month, month) == ("*", "*")


def test_matrix_tests_every_python_the_package_claims() -> None:
    project = _project()
    matrix = _workflow("ci.yml")["jobs"]["test"]["strategy"]["matrix"]["python-version"]
    # Quoted in the workflow: an unquoted 3.10 would be read as the number 3.1.
    assert all(isinstance(version, str) for version in matrix)
    claimed = [
        c.removeprefix(_CLASSIFIER)
        for c in project["classifiers"]
        if re.fullmatch(re.escape(_CLASSIFIER) + r"\d+\.\d+", c)
    ]
    assert matrix == claimed
    assert "3.14" in matrix
    floor = project["requires-python"].removeprefix(">=")
    assert min(matrix, key=_minor) == floor


def test_lowest_bounds_job_installs_the_floors_on_the_oldest_python() -> None:
    job = _workflow("ci.yml")["jobs"]["test-lowest"]
    assert "if" not in job and not job.get("continue-on-error", False)
    steps = job["steps"]
    assert all("if" not in s and not s.get("continue-on-error", False) for s in steps)

    python = [s["with"]["python-version"] for s in steps if "python-version" in s.get("with", {})]
    assert python == [_project()["requires-python"].removeprefix(">=")]

    commands = [s["run"] for s in steps if "run" in s]
    install = "uv pip install --system --resolution lowest-direct -e ."
    # The dev extra has no lower bounds, so it is not part of the lowest
    # resolution (uv would build the first pytest ever published); the test
    # runner is installed on its own afterwards.
    assert not any("lowest" in c and "[" in c for c in commands)
    assert (
        commands.index(f"pip install uv=={_uv_version()}")
        < commands.index(install)
        < commands.index("uv pip install --system pytest")
        < commands.index("pytest -q")
    )


def test_jobs_that_existed_keep_their_names() -> None:
    # Branch protection refers to jobs by name; a new job is added, not renamed.
    jobs = _workflow("ci.yml")["jobs"]
    for name in ("test", "test-locked", "lockfile-freshness", "identity-guard"):
        assert name in jobs
        assert "name" not in jobs[name]


@pytest.mark.parametrize("filename", sorted(p.name for p in WORKFLOWS.glob("*.yml")))
def test_every_action_is_pinned_by_commit(filename: str) -> None:
    uses = [
        step["uses"]
        for job in _workflow(filename)["jobs"].values()
        for step in job.get("steps", [])
        if "uses" in step
    ]
    assert uses, "a workflow without any action would make this check vacuous"
    for action in uses:
        assert _PINNED_ACTION.fullmatch(action), action
