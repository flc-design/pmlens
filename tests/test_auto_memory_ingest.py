"""PMSERV-156 / ADR-045: physical ingest of auto-memory into the global index.

The v1 overlay (ADR-040) only reaches the CURRENT project, and `pm_recall`'s
cross_project branch returns before any overlay runs — so auto-memory
knowledge was structurally invisible to cross-project search. These tests pin
the ingest that closes that gap, plus the guardrails ADR-045 attaches to it.
"""

from __future__ import annotations

import sqlite3

import pytest

from pmlens import auto_memory
from pmlens.memory import AUTO_MEMORY_SOURCE, MemoryStore
from pmlens.models import Memory


def write_note(home, project_path, name: str, body: str, note_type: str = "reference") -> None:
    """Create a Claude Code auto-memory note for `project_path` under `home`."""
    d = home / ".claude" / "projects" / auto_memory.encode_project_dirname(project_path) / "memory"
    d.mkdir(parents=True, exist_ok=True)
    (d / name).write_text(
        f"---\nname: {name.removesuffix('.md')}\n"
        f"description: test note\nmetadata:\n  type: {note_type}\n---\n\n{body}\n",
        encoding="utf-8",
    )


@pytest.fixture
def env(tmp_path, monkeypatch):
    """Two projects, an isolated HOME, and a store wired to a global index."""
    home = tmp_path / "home"
    (home / ".claude" / "projects").mkdir(parents=True)
    monkeypatch.setenv("HOME", str(home))
    proj_a, proj_b = tmp_path / "proj-a", tmp_path / "proj-b"
    for p in (proj_a, proj_b):
        (p / ".pm").mkdir(parents=True)
    store = MemoryStore(proj_a / ".pm" / "memory.db", global_db_path=home / ".pm" / "memory.db")
    yield home, proj_a, proj_b, store
    store.close()


def collect(project_path, home, scope="project"):
    entries, scanned, _diag = auto_memory.collect_ingest_entries(
        str(project_path), scope=scope, home=home
    )
    return entries, scanned


class TestCollect:
    def test_project_scope_sees_only_this_repo(self, env):
        home, a, b, _ = env
        write_note(home, a, "a1.md", "ALPHATOKEN lives here")
        write_note(home, b, "b1.md", "BETATOKEN lives here")
        entries, dirs = collect(a, home)
        assert [e["source_file"] for e in entries] == ["a1.md"]
        assert len(dirs) == 1

    def test_all_scope_sweeps_every_store(self, env):
        home, a, b, _ = env
        write_note(home, a, "a1.md", "ALPHATOKEN")
        write_note(home, b, "b1.md", "BETATOKEN")
        entries, dirs = collect(a, home, scope="all")
        assert sorted(e["source_file"] for e in entries) == ["a1.md", "b1.md"]
        assert len(dirs) == 2

    def test_content_is_not_truncated_for_ingest(self, env):
        """The overlay excerpts at 500 chars; indexing that excerpt would make
        anything said later in a note permanently unsearchable."""
        home, a, _b, _ = env
        tail = "NEEDLETOKEN"
        write_note(home, a, "long.md", ("x" * 2000) + " " + tail)
        entries, _ = collect(a, home)
        assert tail in entries[0]["content"]
        assert "content_truncated" not in entries[0]

    def test_memory_md_index_is_never_ingested(self, env):
        """MEMORY.md is the reverse bridge's own output — ingesting it would
        close the loop ADR-040 keeps structurally open."""
        home, a, _b, _ = env
        write_note(home, a, "real.md", "REALTOKEN")
        d = home / ".claude" / "projects" / auto_memory.encode_project_dirname(a) / "memory"
        (d / "MEMORY.md").write_text("- [Real](real.md) — pointer\n", encoding="utf-8")
        entries, _ = collect(a, home)
        assert [e["source_file"] for e in entries] == ["real.md"]

    def test_rejects_an_unknown_scope(self, env):
        home, a, _b, _ = env
        with pytest.raises(ValueError):
            auto_memory.collect_ingest_entries(str(a), scope="everything", home=home)


class TestIngest:
    def test_ingested_notes_become_cross_project_searchable(self, env):
        home, a, _b, store = env
        write_note(home, a, "a1.md", "ZEBRAFISHALPHA runbook")
        assert store.search_global_ex("ZEBRAFISHALPHA")[0] == []  # the PMSERV-156 blind spot
        entries, dirs = collect(a, home)
        result = store.ingest_auto_memory(entries, dirs)
        assert result["ingested"] == 1
        hits, _strategy = store.search_global_ex("ZEBRAFISHALPHA")
        assert len(hits) == 1
        assert hits[0]["source"] == AUTO_MEMORY_SOURCE
        assert hits[0]["source_path"].endswith("a1.md")

    def test_dry_run_reports_without_writing(self, env):
        home, a, _b, store = env
        write_note(home, a, "a1.md", "DRYTOKEN")
        entries, dirs = collect(a, home)
        assert store.ingest_auto_memory(entries, dirs, dry_run=True)["ingested"] == 1
        assert store.search_global_ex("DRYTOKEN")[0] == []

    def test_reingest_is_idempotent_by_content_hash(self, env):
        home, a, _b, store = env
        write_note(home, a, "a1.md", "STABLETOKEN")
        entries, dirs = collect(a, home)
        store.ingest_auto_memory(entries, dirs)
        again = store.ingest_auto_memory(*collect(a, home))
        assert (again["ingested"], again["unchanged"]) == (0, 1)
        assert len(store.search_global_ex("STABLETOKEN")[0]) == 1

    def test_edited_note_replaces_the_row_and_the_fts_entry(self, env):
        """Re-ingest must DELETE+INSERT: the global FTS5 table is
        external-content with only after-insert / after-delete triggers, so an
        UPDATE would leave the old text searchable and the new text missing —
        silently, with no error anywhere."""
        home, a, _b, store = env
        write_note(home, a, "a1.md", "OLDTOKEN here")
        store.ingest_auto_memory(*collect(a, home))
        write_note(home, a, "a1.md", "NEWTOKEN here")
        result = store.ingest_auto_memory(*collect(a, home))
        assert result["ingested"] == 1
        assert len(store.search_global_ex("NEWTOKEN")[0]) == 1
        assert store.search_global_ex("OLDTOKEN")[0] == [], "stale FTS row survived the re-ingest"

    def test_deleted_note_is_pruned(self, env):
        home, a, _b, store = env
        write_note(home, a, "gone.md", "GONETOKEN")
        store.ingest_auto_memory(*collect(a, home))
        d = home / ".claude" / "projects" / auto_memory.encode_project_dirname(a) / "memory"
        (d / "gone.md").unlink()
        result = store.ingest_auto_memory(*collect(a, home))
        assert result["pruned"] == 1
        assert store.search_global_ex("GONETOKEN")[0] == []

    def test_project_scoped_ingest_never_prunes_another_project(self, env):
        """Pruning is scoped to the directories actually scanned. Without that,
        a project-scoped run would see every other project's rows as 'files no
        longer present' and delete them."""
        home, a, b, store = env
        write_note(home, a, "a1.md", "ALPHATOKEN")
        write_note(home, b, "b1.md", "BETATOKEN")
        store.ingest_auto_memory(*collect(a, home, scope="all"))
        assert len(store.search_global_ex("BETATOKEN")[0]) == 1
        store.ingest_auto_memory(*collect(a, home))  # project scope only
        assert len(store.search_global_ex("BETATOKEN")[0]) == 1

    def test_ledger_rows_are_untouched_by_ingest_and_purge(self, env):
        """PMSERV-111: the project ledger stays the source of truth. Ingest
        adds derived rows beside it and purge removes only those."""
        home, a, _b, store = env
        store.save(Memory(session_id="s1", content="LEDGERTOKEN from pm_remember", project="a"))
        write_note(home, a, "a1.md", "AUTOTOKEN")
        store.ingest_auto_memory(*collect(a, home))
        assert len(store.search_global_ex("LEDGERTOKEN")[0]) == 1
        purged = store.purge_auto_memory(None)
        assert purged["purged"] == 1
        assert store.search_global_ex("AUTOTOKEN")[0] == []
        assert len(store.search_global_ex("LEDGERTOKEN")[0]) == 1
        # The project's own ledger table is never written by ingest.
        assert store.get_stats()["total_memories"] == 1

    def test_purge_can_be_limited_to_one_project(self, env):
        home, a, b, store = env
        write_note(home, a, "a1.md", "ALPHATOKEN")
        write_note(home, b, "b1.md", "BETATOKEN")
        store.ingest_auto_memory(*collect(a, home, scope="all"))
        _entries, dirs_a = collect(a, home)
        assert store.purge_auto_memory(dirs_a)["purged"] == 1
        assert store.search_global_ex("ALPHATOKEN")[0] == []
        assert len(store.search_global_ex("BETATOKEN")[0]) == 1


class TestMigration:
    def test_ingest_migrates_a_pre_pmserv156_global_index(self, env):
        """Existing global indexes have no provenance columns. Ingest must add
        them without disturbing the rows already there."""
        home, a, _b, store = env
        store.save(Memory(session_id="s1", content="EXISTINGTOKEN", project="a"))
        gdb = home / ".pm" / "memory.db"
        conn = sqlite3.connect(gdb)
        for col in ("source", "source_path", "content_hash"):
            conn.execute(f"ALTER TABLE memory_index DROP COLUMN {col}")
        conn.commit()
        cols = {r[1] for r in conn.execute("PRAGMA table_info(memory_index)")}
        conn.close()
        assert "source" not in cols  # genuinely un-migrated

        # A read on the un-migrated DB must not raise (ingest is what migrates).
        assert len(store.search_global_ex("EXISTINGTOKEN")[0]) == 1

        write_note(home, a, "a1.md", "NEWLYINDEXED")
        store.ingest_auto_memory(*collect(a, home))
        hits, _ = store.search_global_ex("EXISTINGTOKEN")
        assert hits[0]["source"] == "pm", "pre-existing rows must default to the ledger source"
        assert len(store.search_global_ex("NEWLYINDEXED")[0]) == 1


class TestServerTool:
    def _setup(self, tmp_path, monkeypatch, home):
        from pmlens.models import Project
        from pmlens.storage import _save_project

        pm_path = tmp_path / ".pm"
        pm_path.mkdir(parents=True, exist_ok=True)
        (pm_path / "daily").mkdir(exist_ok=True)
        _save_project(pm_path, Project(name="proj", display_name="proj"))
        monkeypatch.setenv("HOME", str(home))
        monkeypatch.chdir(tmp_path)

    def test_tool_defaults_to_project_scope_and_dry_run(self, tmp_path, monkeypatch):
        home = tmp_path / "home"
        (home / ".claude" / "projects").mkdir(parents=True)
        self._setup(tmp_path / "proj", monkeypatch, home)
        from pmlens.server import pm_memory_ingest

        write_note(home, tmp_path / "proj", "n.md", "TOOLTOKEN")
        result = pm_memory_ingest()
        assert result["scope"] == "project"
        assert result["dry_run"] is True
        assert result["notes_found"] == 1
        assert "warnings" not in result

    def test_all_scope_warns_about_the_blast_radius(self, tmp_path, monkeypatch):
        home = tmp_path / "home"
        (home / ".claude" / "projects").mkdir(parents=True)
        self._setup(tmp_path / "proj", monkeypatch, home)
        from pmlens.server import pm_memory_ingest

        write_note(home, tmp_path / "proj", "n.md", "TOOLTOKEN")
        write_note(home, tmp_path / "other", "o.md", "OTHERTOKEN")
        result = pm_memory_ingest(scope="all")
        codes = [w["code"] for w in result.get("warnings", [])]
        assert "auto_memory_ingest_blocked_foreign" in codes
        assert result["would_block"] is True
        assert result["notes_found"] == 2

    def test_unknown_scope_is_rejected(self, tmp_path, monkeypatch):
        home = tmp_path / "home"
        (home / ".claude" / "projects").mkdir(parents=True)
        self._setup(tmp_path / "proj", monkeypatch, home)
        from pmlens.server import pm_memory_ingest

        assert pm_memory_ingest(scope="everything")["status"] == "error"

    def test_ingest_tool_is_hidden_from_the_lens_viewer(self):
        """Ingest writes, so it must never register under PM_LENS=1 (RO
        invariant, PMSERV-144)."""
        from pmlens.server import RO_ALLOWLIST

        assert "pm_memory_ingest" not in RO_ALLOWLIST


# ─── Adversarial-review regressions (PMSERV-156 hardening) ───────────────


class TestForeignGate:
    """The safety boundary is what was COLLECTED, not the scope parameter:
    an auto_memory_path override kept scope="project" while ingesting an
    arbitrary directory, and the scope-keyed warning never fired."""

    def _setup(self, tmp_path, monkeypatch):
        from pmlens.models import Project
        from pmlens.storage import _save_project

        home = tmp_path / "home"
        (home / ".claude" / "projects").mkdir(parents=True)
        proj = tmp_path / "proj"
        pm_path = proj / ".pm"
        pm_path.mkdir(parents=True)
        (pm_path / "daily").mkdir()
        _save_project(pm_path, Project(name="proj", display_name="proj"))
        monkeypatch.setenv("HOME", str(home))
        monkeypatch.chdir(proj)
        return home, proj

    def test_scope_all_real_run_is_blocked_without_force(self, tmp_path, monkeypatch):
        home, proj = self._setup(tmp_path, monkeypatch)
        from pmlens.server import pm_memory_ingest

        write_note(home, proj, "mine.md", "MINETOKEN")
        write_note(home, tmp_path / "other", "theirs.md", "THEIRSTOKEN")
        result = pm_memory_ingest(scope="all", dry_run=False)
        assert result["blocked"] is True
        assert result["ingested"] == 0
        assert result["foreign_projects"]
        codes = [w["code"] for w in result["warnings"]]
        assert codes == ["auto_memory_ingest_blocked_foreign"]
        # All-or-nothing: even this project's own note was not written.
        assert not (home / ".pm" / "memory.db").exists()

    def test_force_executes_and_keeps_the_post_hoc_warning(self, tmp_path, monkeypatch):
        home, proj = self._setup(tmp_path, monkeypatch)
        from pmlens.server import pm_memory_ingest

        write_note(home, tmp_path / "other", "theirs.md", "THEIRSTOKEN")
        result = pm_memory_ingest(scope="all", dry_run=False, force=True)
        assert result.get("blocked") is not True
        assert result["ingested"] == 1
        codes = [w["code"] for w in result["warnings"]]
        assert "auto_memory_ingested_foreign" in codes
        assert "auto_memory_ingest_blocked_foreign" not in codes

    def test_auto_memory_path_override_is_gated(self, tmp_path, monkeypatch):
        """The confirmed bypass: scope="project" + auto_memory_path pointing
        at an arbitrary directory ingested it with no warning at all."""
        home, proj = self._setup(tmp_path, monkeypatch)
        from pmlens.server import pm_memory_ingest

        outside = tmp_path / "private-notes"
        outside.mkdir()
        (outside / "secret.md").write_text("---\nname: s\n---\n\nSECRETTOKEN\n", encoding="utf-8")
        result = pm_memory_ingest(scope="project", dry_run=False, auto_memory_path=str(outside))
        assert result["blocked"] is True
        assert result["ingested"] == 0
        assert not (home / ".pm" / "memory.db").exists()

    def test_own_project_content_is_never_gated(self, tmp_path, monkeypatch):
        """scope="all" with only this project's own store present collects
        nothing foreign — fact-based gating must not fire on scope alone."""
        home, proj = self._setup(tmp_path, monkeypatch)
        from pmlens.server import pm_memory_ingest

        write_note(home, proj, "mine.md", "MINETOKEN")
        result = pm_memory_ingest(scope="all", dry_run=False)
        assert result.get("blocked") is not True
        assert result["ingested"] == 1
        assert "warnings" not in result

    def test_dry_run_predicts_the_block_without_writing(self, tmp_path, monkeypatch):
        home, proj = self._setup(tmp_path, monkeypatch)
        from pmlens.server import pm_memory_ingest

        write_note(home, tmp_path / "other", "theirs.md", "THEIRSTOKEN")
        result = pm_memory_ingest(scope="all")  # dry_run default
        assert result["would_block"] is True
        assert not (home / ".pm" / "memory.db").exists()
        forced = pm_memory_ingest(scope="all", force=True)
        assert forced.get("would_block") is not True
        assert [w["code"] for w in forced["warnings"]] == ["auto_memory_ingested_foreign"]


class TestDryRunPurity:
    def test_dry_run_does_not_create_the_global_db(self, env):
        """The old dry_run created the DB, flipped it to WAL and ALTERed the
        schema before ever checking the flag (adversarial review, two lenses
        independently)."""
        home, a, _b, store = env
        write_note(home, a, "a1.md", "PUREDRYTOKEN")
        result = store.ingest_auto_memory(*collect(a, home), dry_run=True)
        assert result["ingested"] == 1
        assert not (home / ".pm" / "memory.db").exists()

    def test_dry_run_does_not_migrate_an_old_schema(self, env):
        home, a, _b, store = env
        store.save(Memory(session_id="s1", content="OLDROW", project="a"))
        gdb = home / ".pm" / "memory.db"
        conn = sqlite3.connect(gdb)
        for col in ("source", "source_path", "content_hash"):
            conn.execute(f"ALTER TABLE memory_index DROP COLUMN {col}")
        conn.commit()
        conn.close()
        write_note(home, a, "a1.md", "MIGRATIONTOKEN")
        result = store.ingest_auto_memory(*collect(a, home), dry_run=True)
        assert result["ingested"] == 1  # counted as new — no provenance rows exist
        cols = {r[1] for r in sqlite3.connect(gdb).execute("PRAGMA table_info(memory_index)")}
        assert "source" not in cols, "dry_run migrated the schema"


class TestScanFailureSafety:
    def test_unreadable_dir_is_skipped_not_pruned(self, env):
        """pathlib's glob() swallows PermissionError and returns [] — the old
        collector then listed the directory as scanned, and every previously
        indexed row under it was pruned as stale (confirmed: 2 rows -> 0,
        no error). os.listdir raises honestly; the dir must leave scanning
        as UNREADABLE, not as empty."""
        import os as _os

        home, a, _b, store = env
        write_note(home, a, "keep.md", "KEEPTOKEN")
        store.ingest_auto_memory(*collect(a, home))
        assert len(store.search_global_ex("KEEPTOKEN")[0]) == 1
        d = home / ".claude" / "projects" / auto_memory.encode_project_dirname(a) / "memory"
        mode = d.stat().st_mode
        _os.chmod(d, 0o000)
        try:
            entries, scanned, diag = auto_memory.collect_ingest_entries(str(a), home=home)
            assert entries == []
            assert scanned == []
            assert diag["unreadable_dirs"]
            result = store.ingest_auto_memory(entries, scanned)
            assert result["pruned"] == 0
        finally:
            _os.chmod(d, mode)
        assert len(store.search_global_ex("KEEPTOKEN")[0]) == 1

    def test_one_pathological_file_does_not_abort_the_sweep(self, env, monkeypatch):
        home, a, _b, _store = env
        write_note(home, a, "good.md", "GOODTOKEN")
        write_note(home, a, "bad.md", "BADTOKEN")

        real_parse = auto_memory.parse_auto_memory_file

        def exploding(path, **kwargs):
            if path.name == "bad.md":
                raise RecursionError("billion laughs")
            return real_parse(path, **kwargs)

        monkeypatch.setattr(auto_memory, "parse_auto_memory_file", exploding)
        entries, _scanned, diag = auto_memory.collect_ingest_entries(str(a), home=home)
        assert [e["source_file"] for e in entries] == ["good.md"]
        assert len(diag["skipped_files"]) == 1


class TestSymlinkedRoot:
    def test_symlinked_project_root_stays_idempotent(self, tmp_path, monkeypatch):
        """Claude Code encodes the path as IT sees it (possibly through a
        symlink); the store compared resolved scan dirs against unresolved
        stored paths, so every re-ingest duplicated every note and pruning
        never fired (adversarial review, confirmed via /var vs /private/var)."""
        home = tmp_path / "home"
        (home / ".claude" / "projects").mkdir(parents=True)
        monkeypatch.setenv("HOME", str(home))
        real = tmp_path / "real-proj"
        (real / ".pm").mkdir(parents=True)
        link = tmp_path / "link-proj"
        link.symlink_to(real, target_is_directory=True)

        # CC created the store under the SYMLINK spelling of the root.
        d = home / ".claude" / "projects" / auto_memory.encode_project_dirname(link) / "memory"
        d.mkdir(parents=True)
        (d / "n.md").write_text("---\nname: n\n---\n\nSYMLINKTOKEN\n", encoding="utf-8")

        store = MemoryStore(real / ".pm" / "memory.db", global_db_path=home / ".pm" / "memory.db")
        try:

            def run():
                entries, scanned, _diag = auto_memory.collect_ingest_entries(str(link), home=home)
                return store.ingest_auto_memory(entries, scanned)

            first = run()
            assert first["ingested"] == 1, "locator missed the symlink-spelled store"
            second = run()
            assert (second["ingested"], second["unchanged"]) == (0, 1)
            assert len(store.search_global_ex("SYMLINKTOKEN")[0]) == 1
            (d / "n.md").unlink()
            third = run()
            assert third["pruned"] == 1
            assert store.search_global_ex("SYMLINKTOKEN")[0] == []
        finally:
            store.close()


class TestPurgeContract:
    def test_purge_reports_projects_and_supports_dry_run(self, env):
        home, a, b, store = env
        write_note(home, a, "a1.md", "ALPHATOKEN")
        write_note(home, b, "b1.md", "BETATOKEN")
        store.ingest_auto_memory(*collect(a, home, scope="all"))
        preview = store.purge_auto_memory(None, dry_run=True)
        assert preview["would_purge"] == 2
        assert len(preview["projects"]) == 2
        assert len(store.search_global_ex("ALPHATOKEN")[0]) == 1  # nothing deleted
        real = store.purge_auto_memory(None)
        assert real["purged"] == 2

    def test_purge_by_project_root_survives_a_vanished_dir(self, env):
        """The source directory can disappear (encoding drift, deleted repo);
        rows still carry project_path, so a project-scoped purge must keep
        working instead of returning purged:0 as success (two lenses
        independently)."""
        import shutil as _shutil

        home, a, _b, store = env
        write_note(home, a, "a1.md", "VANISHTOKEN")
        store.ingest_auto_memory(*collect(a, home))
        d = home / ".claude" / "projects" / auto_memory.encode_project_dirname(a) / "memory"
        _shutil.rmtree(d.parent)
        entries, scanned = collect(a, home)
        assert scanned == []  # the dir is gone — dir-based targeting finds nothing
        result = store.purge_auto_memory(scanned, project_root=a)
        assert result["purged"] == 1
        assert store.search_global_ex("VANISHTOKEN")[0] == []

    def test_purge_tool_reports_scope_all_removal(self, tmp_path, monkeypatch):
        from pmlens.models import Project
        from pmlens.storage import _save_project

        home = tmp_path / "home"
        (home / ".claude" / "projects").mkdir(parents=True)
        proj = tmp_path / "proj"
        (proj / ".pm" / "daily").mkdir(parents=True)
        _save_project(proj / ".pm", Project(name="proj", display_name="proj"))
        monkeypatch.setenv("HOME", str(home))
        monkeypatch.chdir(proj)
        from pmlens.server import pm_memory_ingest

        write_note(home, proj, "mine.md", "MINETOKEN")
        write_note(home, tmp_path / "other", "theirs.md", "THEIRSTOKEN")
        pm_memory_ingest(scope="all", dry_run=False, force=True)
        result = pm_memory_ingest(scope="all", purge=True, dry_run=False)
        assert result["purged"] == 2
        assert len(result["projects"]) == 2
        codes = [w["code"] for w in result.get("warnings", [])]
        assert "auto_memory_purged_all_projects" in codes


class TestIndexRowShape:
    def test_created_at_matches_ledger_format_and_is_not_future(self, env):
        """Ledger rows use SQLite datetime('now') — UTC, space-separated. The
        local isoformat mtime ('T'-separated) sorted AFTER every ledger value
        in the LIKE fallback's ORDER BY created_at (confirmed: 'T' > ' ')."""
        home, a, _b, store = env
        store.save(Memory(session_id="s1", content="LEDGERROW", project="a"))
        write_note(home, a, "ts.md", "AUTOROW")
        store.ingest_auto_memory(*collect(a, home))
        conn = sqlite3.connect(home / ".pm" / "memory.db")
        auto_ts = conn.execute(
            "SELECT created_at FROM memory_index WHERE source='auto_memory'"
        ).fetchone()[0]
        now_utc = conn.execute("SELECT datetime('now') AS n").fetchone()[0]
        conn.close()
        assert "T" not in auto_ts
        assert auto_ts <= now_utc

    def test_typeless_note_gets_unknown_not_the_source_literal(self, env):
        """type='auto_memory' invented a fake category colliding with the
        source column; the overlay honestly returns None for such notes."""
        home, a, _b, store = env
        d = home / ".claude" / "projects" / auto_memory.encode_project_dirname(a) / "memory"
        d.mkdir(parents=True, exist_ok=True)
        (d / "untyped.md").write_text("---\nname: u\n---\n\nUNTYPEDTOKEN\n", encoding="utf-8")
        store.ingest_auto_memory(*collect(a, home))
        hit = store.search_global_ex("UNTYPEDTOKEN")[0][0]
        assert hit["type"] == "unknown"
        assert hit["source"] == AUTO_MEMORY_SOURCE

    def test_drift_duplicate_dirs_dedup_to_one_row(self, env):
        """One repo can own several encoded dirs (encoding drift); the overlay
        dedups by basename but ingest did not, so one logical note became N
        searchable rows."""
        home, a, _b, store = env
        raw = str(a)
        current = home / ".claude" / "projects" / auto_memory.encode_project_dirname(raw)
        legacy = home / ".claude" / "projects" / raw.replace("/", "-")
        for base in (current, legacy):
            (base / "memory").mkdir(parents=True, exist_ok=True)
            (base / "memory" / "same.md").write_text(
                "---\nname: s\n---\n\nDRIFTTOKEN\n", encoding="utf-8"
            )
        # Register the project so both encodings resolve to one identity.
        (home / ".pm").mkdir(exist_ok=True)
        (home / ".pm" / "registry.yaml").write_text(
            f"projects:\n- path: {raw}\n  name: proj-a\n  registered: '2026-01-01'\n",
            encoding="utf-8",
        )
        entries, _scanned, _diag = auto_memory.collect_ingest_entries(
            str(a), scope="all", home=home
        )
        drift_entries = [e for e in entries if "DRIFTTOKEN" in e["content"]]
        assert len(drift_entries) == 1


class TestLensGlobalRead:
    def test_lens_searches_ingested_rows_but_cannot_ingest(self, env):
        """CHANGELOG/design.md promise the Lens viewer can SEARCH ingested
        rows; the old wiring nulled global_db_path on every readonly store,
        so Lens cross-project search always returned [] (confirmed). Writes
        must still be refused by the store itself (defense-in-depth under
        RO_ALLOWLIST)."""
        home, a, _b, store = env
        write_note(home, a, "a1.md", "LENSREADTOKEN")
        store.ingest_auto_memory(*collect(a, home))
        # The WRITER legitimately created WAL sidecars; its connection is
        # closed (auto-checkpoint), so drop them to observe what the RO read
        # itself touches.
        for suffix in ("-wal", "-shm"):
            sidecar = home / ".pm" / f"memory.db{suffix}"
            if sidecar.exists():
                sidecar.unlink()

        ro = MemoryStore(
            a / ".pm" / "memory.db",
            global_db_path=home / ".pm" / "memory.db",
            readonly=True,
        )
        try:
            hits, _ = ro.search_global_ex("LENSREADTOKEN")
            assert len(hits) == 1
            assert hits[0]["source"] == AUTO_MEMORY_SOURCE
            refused = ro.ingest_auto_memory(*collect(a, home))
            assert "error" in refused
            purged = ro.purge_auto_memory(None)
            assert "error" in purged
        finally:
            ro.close()
        # The read created no sidecars in ~/.pm (RO invariant, ADR-028).
        assert not (home / ".pm" / "memory.db-wal").exists()
        assert not (home / ".pm" / "memory.db-shm").exists()
