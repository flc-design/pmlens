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
    return auto_memory.collect_ingest_entries(str(project_path), scope=scope, home=home)


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
        assert hits[0]["source_file"].endswith("a1.md")

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
        for col in ("source", "source_file", "content_hash"):
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
        assert "auto_memory_ingested_all_projects" in codes
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
