"""Integration tests for concurrent YAML writes (PMSERV-048 / ADR-011).

These tests spawn real OS-level subprocesses to validate that file locking
prevents lost updates and that atomic writes prevent partial-write corruption
under SIGKILL.

Why subprocess and not threading: filelock advisory locks are reentrant within
the same Python process, so threading wouldn't exercise the lock semantics we
care about — only multi-process contention does.
"""

from __future__ import annotations

import multiprocessing as mp
import os
import signal
import time
from pathlib import Path

import pytest

from pmlens import storage
from pmlens.models import Task
from pmlens.storage import (
    _LOCK_TIMEOUT_ENV,
    DEFAULT_LOCK_TIMEOUT_S,
    _resolve_lock_timeout,
    _yaml_transaction,
    add_task,
    load_tasks,
)

# ─── Top-level worker functions (must be module-level for pickling) ──


def _worker_add_tasks(pm_path_str: str, prefix: str, count: int) -> None:
    """Add ``count`` tasks named ``{prefix}-NNN`` via the public mutator.

    Each call goes through ``add_task`` which acquires the tasks.yaml lock.
    """
    pm_path = Path(pm_path_str)
    for i in range(count):
        add_task(
            pm_path,
            Task(id=f"{prefix}-{i:03d}", title=f"task {prefix}-{i}", phase="phase-1"),
        )


def _worker_holds_lock_then_writes(pm_path_str: str, hold_seconds: float, ready_path: str) -> None:
    """Acquire the tasks lock, signal ready, hold for ``hold_seconds``."""
    from pmlens.storage import _yaml_transaction

    pm_path = Path(pm_path_str)
    with _yaml_transaction(pm_path, "tasks.yaml"):
        # Touch ready file so parent knows we're holding the lock
        Path(ready_path).touch()
        time.sleep(hold_seconds)


# ─── Tests ───────────────────────────────────────────


@pytest.fixture
def cm_pm_path(tmp_path):
    """Initialise a fresh .pm/ project dir for concurrent tests."""
    pm_path = tmp_path / ".pm"
    pm_path.mkdir()
    return pm_path


class TestConcurrentMutations:
    def test_two_processes_adding_tasks_no_lost_update(self, cm_pm_path, monkeypatch):
        """100 add_task calls split across 2 processes should yield 100 tasks."""
        # On heavily-loaded CI runners (observed on Python 3.12, PMSERV-108/109)
        # the default 5s lock-acquire timeout can be exceeded purely from
        # scheduler jitter while a peer process holds the tasks.yaml lock — a
        # false failure, since locking is working correctly. Give the spawned
        # workers a generous timeout via the env knob. ``spawn`` inherits the
        # parent's os.environ, so the children pick this up.
        monkeypatch.setenv("PM_LOCK_TIMEOUT_S", "30")
        ctx = mp.get_context("spawn")
        p1 = ctx.Process(target=_worker_add_tasks, args=(str(cm_pm_path), "A", 50))
        p2 = ctx.Process(target=_worker_add_tasks, args=(str(cm_pm_path), "B", 50))

        p1.start()
        p2.start()
        p1.join(timeout=30)
        p2.join(timeout=30)

        assert p1.exitcode == 0, "process A failed"
        assert p2.exitcode == 0, "process B failed"

        tasks = load_tasks(cm_pm_path)
        ids = {t.id for t in tasks}
        # Without locking, ~50% of writes would be lost. With locking, all 100 survive.
        assert len(tasks) == 100, f"expected 100 tasks, got {len(tasks)}"
        assert ids == {f"A-{i:03d}" for i in range(50)} | {f"B-{i:03d}" for i in range(50)}

    def test_lock_blocks_concurrent_writer(self, cm_pm_path):
        """Second process should fail with PmServerError when first holds lock."""
        from pmlens.models import PmServerError
        from pmlens.storage import _yaml_transaction

        ctx = mp.get_context("spawn")
        ready_file = cm_pm_path / "ready.txt"
        holder = ctx.Process(
            target=_worker_holds_lock_then_writes,
            args=(str(cm_pm_path), 2.0, str(ready_file)),
        )
        holder.start()

        try:
            # Wait until holder has actually acquired the lock
            for _ in range(50):
                if ready_file.exists():
                    break
                time.sleep(0.05)
            assert ready_file.exists(), "holder process did not signal ready"

            # Now try to acquire from this process with short timeout — must fail
            with pytest.raises(PmServerError, match="timeout"):
                with _yaml_transaction(cm_pm_path, "tasks.yaml", timeout=0.3):
                    pass
        finally:
            holder.join(timeout=10)
            assert holder.exitcode == 0


class TestAtomicWriteUnderKill:
    def test_kill_during_write_leaves_original_intact(self, cm_pm_path):
        """SIGKILL during a write must not leave a half-written tasks.yaml."""
        # Seed a known-good state
        add_task(cm_pm_path, Task(id="ORIG-001", title="seed", phase="phase-1"))
        original_path = cm_pm_path / "tasks.yaml"
        original_bytes = original_path.read_bytes()

        # Spawn a child that holds the lock and sleeps — then SIGKILL it.
        # The child does NOT write, so we just verify the original is intact
        # after a forcibly-terminated lock holder. This proves that lock files
        # are released by the OS on process death (no permanent block).
        ctx = mp.get_context("spawn")
        ready_file = cm_pm_path / "kill_ready.txt"
        holder = ctx.Process(
            target=_worker_holds_lock_then_writes,
            args=(str(cm_pm_path), 30.0, str(ready_file)),
        )
        holder.start()

        try:
            for _ in range(100):
                if ready_file.exists():
                    break
                time.sleep(0.05)
            assert ready_file.exists(), "holder did not become ready in time"

            # SIGKILL — simulates OS crash / kill -9
            os.kill(holder.pid, signal.SIGKILL)
        finally:
            holder.join(timeout=10)

        # The yaml must be exactly the seed state — never corrupted
        assert original_path.read_bytes() == original_bytes

        # And we should be able to acquire the lock again (OS released it)
        add_task(cm_pm_path, Task(id="POST-001", title="after-kill", phase="phase-1"))
        tasks_after = load_tasks(cm_pm_path)
        assert {t.id for t in tasks_after} == {"ORIG-001", "POST-001"}


class TestLockTimeoutEnvKnob:
    """``PM_LOCK_TIMEOUT_S`` overrides the default lock-acquire timeout (PMSERV-109).

    These are deterministic (no subprocess, no wall-clock waits): the resolver is
    pure, and the wiring into ``_yaml_transaction`` is verified by spying on the
    timeout handed to ``FileLock``. This is the fix for the flaky 5s timeout on
    loaded CI — operators can raise the patience without changing the in-process
    fail-fast default.
    """

    def test_resolve_default_when_env_unset(self, monkeypatch):
        monkeypatch.delenv(_LOCK_TIMEOUT_ENV, raising=False)
        assert _resolve_lock_timeout() == DEFAULT_LOCK_TIMEOUT_S

    def test_resolve_reads_positive_override(self, monkeypatch):
        monkeypatch.setenv(_LOCK_TIMEOUT_ENV, "30")
        assert _resolve_lock_timeout() == 30.0

    def test_resolve_reads_fractional_override(self, monkeypatch):
        monkeypatch.setenv(_LOCK_TIMEOUT_ENV, "0.5")
        assert _resolve_lock_timeout() == 0.5

    @pytest.mark.parametrize("bad", ["abc", "", "0", "-1", "nan"])
    def test_resolve_falls_back_on_invalid(self, monkeypatch, bad):
        """Non-numeric, empty, zero, negative, or NaN → fail-fast default."""
        monkeypatch.setenv(_LOCK_TIMEOUT_ENV, bad)
        assert _resolve_lock_timeout() == DEFAULT_LOCK_TIMEOUT_S

    def _spy_filelock(self, monkeypatch):
        """Patch storage.FileLock with a spy that records the timeout it receives."""
        captured: dict[str, float] = {}
        real_filelock = storage.FileLock

        def spy(path, timeout):
            captured["timeout"] = timeout
            return real_filelock(path, timeout=timeout)

        monkeypatch.setattr(storage, "FileLock", spy)
        return captured

    def test_transaction_uses_env_timeout_by_default(self, monkeypatch, cm_pm_path):
        """With no explicit timeout, the env value flows through to FileLock."""
        monkeypatch.setenv(_LOCK_TIMEOUT_ENV, "12.5")
        captured = self._spy_filelock(monkeypatch)
        with _yaml_transaction(cm_pm_path, "tasks.yaml"):
            pass
        assert captured["timeout"] == 12.5

    def test_explicit_timeout_bypasses_env(self, monkeypatch, cm_pm_path):
        """An explicit timeout wins over the env (tests asserting contention rely on this)."""
        monkeypatch.setenv(_LOCK_TIMEOUT_ENV, "12.5")
        captured = self._spy_filelock(monkeypatch)
        with _yaml_transaction(cm_pm_path, "tasks.yaml", timeout=3.0):
            pass
        assert captured["timeout"] == 3.0
