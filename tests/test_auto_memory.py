"""Tests for the auto-memory bridge (ADR-040 / PMSERV-112 v1).

Covers the read-time overlay (parser + locator + ``build_auto_memory_overlay``
+ ``pm_recall(include_auto_memory=True)``) and the reverse bridge
(``sync_memory_md_pointer`` + ``pm_remember(bridge_to_memory_md=True)``).

Invariants under regression lock (memory:262 / memory:265):
  * MEMORY.md (a derived index) is never ingested → no overlay↔bridge loop.
  * The overlay is READ-ONLY: it never writes ~/.claude, even under PM_LENS=1.
  * The reverse bridge writes ONLY under PM_LENS=0 (defense-in-depth guard) and
    is idempotent (re-run leaves MEMORY.md byte-identical).
  * An FS-diff check ALONE cannot prove a writer is hidden under Lens, so the
    registration-gate membership is asserted explicitly (pm_remember absent /
    pm_recall present in REGISTERED_TOOLS), with a PM_LENS=0 positive control.
"""

from __future__ import annotations

import importlib
from pathlib import Path

import pytest

import pmlens.auto_memory as am
import pmlens.server as srv

# ─── Helpers ─────────────────────────────────────────


def _make_project(tmp_path: Path, name: str = "myrepo") -> Path:
    """A minimal registered project (.pm/project.yaml) so resolve_project_path works."""
    proj = tmp_path / name
    proj.mkdir()
    (proj / ".pm").mkdir()
    (proj / ".pm" / "project.yaml").write_text(
        f"# pm\nname: {name}\ndisplay_name: {name}\nversion: 0.1.0\nstatus: development\n",
        encoding="utf-8",
    )
    return proj


def _cc_memory_dir(home: Path, project_root: Path) -> Path:
    """The ~/.claude/projects/<enc>/memory dir CC would use for project_root."""
    enc = am.encode_project_dirname(project_root.resolve())
    d = home / ".claude" / "projects" / enc / "memory"
    d.mkdir(parents=True)
    return d


NESTED_META = (
    "---\n"
    "name: feedback-x\n"
    "description: nested metadata shape\n"
    "metadata:\n"
    "  node_type: memory\n"
    "  type: feedback\n"
    "  originSessionId: sess-nested-1\n"
    "---\n\n"
    "Body of feedback X about the build command.\n"
)

TOP_LEVEL = (
    "---\n"
    "name: PyPI plan\n"
    "description: top-level type shape\n"
    "type: project\n"
    "originSessionId: sess-top-1\n"
    "---\n"
    "Top-level type shape body about publishing.\n"
)


def _snapshot(root: Path) -> dict[str, tuple[int, int]]:
    """Map every path under ``root`` to (size, mtime_ns); {} if root absent.

    Mirrors test_lens_invariant._fs_snapshot: a newly created file/dir shows up
    as an added key, so creation is detected even without an mtime bump.
    """
    if not root.exists():
        return {}
    out: dict[str, tuple[int, int]] = {}
    for p in sorted(root.rglob("*")):
        try:
            st = p.stat()
        except OSError:  # pragma: no cover
            continue
        out[str(p.relative_to(root))] = (st.st_size if p.is_file() else -1, st.st_mtime_ns)
    return out


# ─── Parser ──────────────────────────────────────────


def test_parse_nested_metadata_shape(tmp_path: Path) -> None:
    f = tmp_path / "feedback_x.md"
    f.write_text(NESTED_META, encoding="utf-8")
    entry = am.parse_auto_memory_file(f)
    assert entry is not None
    assert entry["type"] == "feedback"  # read from metadata.type
    assert entry["session_id"] == "sess-nested-1"  # metadata.originSessionId
    assert entry["name"] == "feedback-x"
    assert entry["description"] == "nested metadata shape"
    assert entry["source"] == "auto_memory"
    assert entry["source_file"] == "feedback_x.md"
    assert "build command" in entry["content"]


def test_parse_top_level_shape(tmp_path: Path) -> None:
    f = tmp_path / "project_y.md"
    f.write_text(TOP_LEVEL, encoding="utf-8")
    entry = am.parse_auto_memory_file(f)
    assert entry is not None
    assert entry["type"] == "project"  # top-level type
    assert entry["session_id"] == "sess-top-1"  # top-level originSessionId


def test_parse_skips_memory_index(tmp_path: Path) -> None:
    f = tmp_path / am.MEMORY_INDEX_FILENAME
    f.write_text("- [x](y.md) — index\n", encoding="utf-8")
    assert am.parse_auto_memory_file(f) is None


def test_parse_malformed_frontmatter_degrades(tmp_path: Path) -> None:
    # Unterminated / invalid YAML mapping must not raise; body preserved.
    f = tmp_path / "bad.md"
    f.write_text("---\n: : : not: valid: yaml\n---\nreal body here\n", encoding="utf-8")
    entry = am.parse_auto_memory_file(f)
    assert entry is not None
    assert entry["type"] is None  # no usable frontmatter
    assert "real body here" in entry["content"]


def test_parse_no_frontmatter_uses_whole_body(tmp_path: Path) -> None:
    f = tmp_path / "plain.md"
    f.write_text("just some prose, no fences\n", encoding="utf-8")
    entry = am.parse_auto_memory_file(f)
    assert entry is not None
    assert entry["type"] is None
    assert entry["name"] == "plain"  # falls back to file stem
    assert "just some prose" in entry["content"]


# ─── Locator ─────────────────────────────────────────


def test_locate_explicit_override_memory_dir(tmp_path: Path) -> None:
    d = tmp_path / "memory"
    d.mkdir()
    (d / "a.md").write_text("x", encoding="utf-8")
    found = am.locate_auto_memory_dirs(auto_memory_path=str(d))
    assert found == [d.resolve()]


def test_locate_explicit_override_parent_with_memory_child(tmp_path: Path) -> None:
    parent = tmp_path / "proj"
    (parent / "memory").mkdir(parents=True)
    found = am.locate_auto_memory_dirs(auto_memory_path=str(parent))
    assert found == [(parent / "memory").resolve()]


def test_locate_enumerate_and_match(tmp_path: Path) -> None:
    home = tmp_path / "home"
    proj = _make_project(tmp_path)
    mem = _cc_memory_dir(home, proj)
    found = am.locate_auto_memory_dirs(str(proj), home=home)
    assert found == [mem]


def test_locate_matches_legacy_slash_only_encoding(tmp_path: Path) -> None:
    # A dir created by an older CC that replaced only "/" (keeping "_") must
    # still resolve via the drift-tolerant candidate set.
    home = tmp_path / "home"
    proj = _make_project(tmp_path, name="under_score")
    legacy_name = str(proj.resolve()).replace("/", "-")  # keeps the underscore
    mem = home / ".claude" / "projects" / legacy_name / "memory"
    mem.mkdir(parents=True)
    found = am.locate_auto_memory_dirs(str(proj), home=home)
    assert mem in found


def test_locate_unresolvable_project_returns_empty(tmp_path: Path) -> None:
    # No .pm/ anywhere and no override → [] (never raises).
    assert am.locate_auto_memory_dirs(str(tmp_path / "nope"), home=tmp_path / "home") == []


# ─── Overlay ─────────────────────────────────────────


def _overlay_project(tmp_path: Path) -> tuple[Path, Path, Path]:
    home = tmp_path / "home"
    proj = _make_project(tmp_path)
    mem = _cc_memory_dir(home, proj)
    (mem / am.MEMORY_INDEX_FILENAME).write_text("- [x](y.md) — index\n", encoding="utf-8")
    (mem / "feedback_x.md").write_text(NESTED_META, encoding="utf-8")
    (mem / "project_y.md").write_text(TOP_LEVEL, encoding="utf-8")
    return home, proj, mem


def test_overlay_default_lists_entries_excludes_index(tmp_path: Path) -> None:
    home, proj, _ = _overlay_project(tmp_path)
    ov = am.build_auto_memory_overlay(str(proj), None, 5, home=home)
    files = sorted(e["source_file"] for e in ov["auto_memory_entries"])
    assert files == ["feedback_x.md", "project_y.md"]
    assert am.MEMORY_INDEX_FILENAME not in files
    assert ov["auto_memory_summary"]["total_available"] == 2
    assert ov["auto_memory_summary"]["scanned_dirs"] == 1
    assert ov["auto_memory_summary"]["scope"] == "project"


def test_overlay_query_substring_filter(tmp_path: Path) -> None:
    home, proj, _ = _overlay_project(tmp_path)
    ov = am.build_auto_memory_overlay(str(proj), "build command", 5, home=home)
    entries = ov["auto_memory_entries"]
    assert [e["source_file"] for e in entries] == ["feedback_x.md"]
    assert all(e["match_source"] == "auto_memory_like" for e in entries)


def test_overlay_no_dir_is_empty_never_raises(tmp_path: Path) -> None:
    proj = _make_project(tmp_path)  # project exists but no ~/.claude memory dir
    ov = am.build_auto_memory_overlay(str(proj), None, 5, home=tmp_path / "empty_home")
    assert ov["auto_memory_entries"] == []
    assert ov["auto_memory_summary"]["scanned_dirs"] == 0
    assert ov["auto_memory_summary"]["total_available"] == 0


def test_overlay_never_creates_files(tmp_path: Path) -> None:
    proj = _make_project(tmp_path)
    home = tmp_path / "home"
    before = _snapshot(home)
    am.build_auto_memory_overlay(str(proj), None, 5, home=home)
    assert _snapshot(home) == before  # no ~/.claude created by a read


def test_overlay_respects_limit_cap(tmp_path: Path) -> None:
    home = tmp_path / "home"
    proj = _make_project(tmp_path)
    mem = _cc_memory_dir(home, proj)
    for i in range(15):
        (mem / f"m{i:02d}.md").write_text(f"---\ntype: project\n---\nbody {i}\n", encoding="utf-8")
    ov = am.build_auto_memory_overlay(str(proj), None, 100, home=home)
    assert len(ov["auto_memory_entries"]) == am._AUTO_MEMORY_MAX_ENTRIES  # hard cap 10
    assert ov["auto_memory_summary"]["total_available"] == 15


# ─── pm_recall integration ───────────────────────────


def _prime_recall(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    home, proj, _ = _overlay_project(tmp_path)
    monkeypatch.setenv("HOME", str(home))
    return proj


def test_pm_recall_include_auto_memory_default_path(tmp_path, monkeypatch) -> None:
    proj = _prime_recall(tmp_path, monkeypatch)
    r = srv.pm_recall(project_path=str(proj), include_auto_memory=True)
    assert "auto_memory_entries" in r and "auto_memory_summary" in r
    assert {e["source_file"] for e in r["auto_memory_entries"]} == {
        "feedback_x.md",
        "project_y.md",
    }


def test_pm_recall_include_auto_memory_query_path(tmp_path, monkeypatch) -> None:
    proj = _prime_recall(tmp_path, monkeypatch)
    r = srv.pm_recall(query="publishing", project_path=str(proj), include_auto_memory=True)
    assert [e["source_file"] for e in r["auto_memory_entries"]] == ["project_y.md"]


def test_pm_recall_flag_off_keys_absent(tmp_path, monkeypatch) -> None:
    proj = _prime_recall(tmp_path, monkeypatch)
    r = srv.pm_recall(project_path=str(proj))
    assert "auto_memory_entries" not in r
    assert "auto_memory_summary" not in r


def test_pm_recall_task_id_path_unaffected(tmp_path, monkeypatch) -> None:
    proj = _prime_recall(tmp_path, monkeypatch)
    # task_id branch must not carry the overlay (parity with include_outbox).
    r = srv.pm_recall(task_id="X-1", project_path=str(proj), include_auto_memory=True)
    assert "auto_memory_entries" not in r


def test_pm_recall_both_overlays_coexist(tmp_path, monkeypatch) -> None:
    proj = _prime_recall(tmp_path, monkeypatch)
    r = srv.pm_recall(project_path=str(proj), include_outbox=True, include_auto_memory=True)
    assert "auto_memory_entries" in r
    assert "outbox_entries" in r  # both independent overlays present


def test_pm_recall_explicit_auto_memory_path(tmp_path, monkeypatch) -> None:
    # Override wins even when the ~/.claude encoding would not match.
    home, proj, mem = _overlay_project(tmp_path)
    monkeypatch.setenv("HOME", str(tmp_path / "unrelated_home"))
    r = srv.pm_recall(
        project_path=str(proj),
        include_auto_memory=True,
        auto_memory_path=str(mem),
    )
    assert {e["source_file"] for e in r["auto_memory_entries"]} == {
        "feedback_x.md",
        "project_y.md",
    }


# ─── Reverse bridge ──────────────────────────────────


def test_bridge_writes_and_is_idempotent(tmp_path) -> None:
    home = tmp_path / "home"
    proj = _make_project(tmp_path)
    _cc_memory_dir(home, proj)  # dir exists (no MEMORY.md yet)

    r1 = am.sync_memory_md_pointer(
        memory_id=1,
        mtype="insight",
        content="First locator insight",
        project_path=str(proj),
        home=home,
        created_at="2026-07-15",
    )
    assert r1["synced"] is True and r1["skipped"] is False
    mp = Path(r1["path"])
    after1 = mp.read_text(encoding="utf-8")
    assert am.BRIDGE_BEGIN in after1 and am.BRIDGE_END in after1
    assert "- PM #1 [insight]" in after1

    # Re-sync same memory_id → no-op, byte-identical.
    r2 = am.sync_memory_md_pointer(
        memory_id=1,
        mtype="insight",
        content="First locator insight",
        project_path=str(proj),
        home=home,
        created_at="2026-07-15",
    )
    assert r2["skipped"] is True
    assert mp.read_text(encoding="utf-8") == after1


def test_bridge_appends_distinct_memories(tmp_path) -> None:
    home = tmp_path / "home"
    proj = _make_project(tmp_path)
    _cc_memory_dir(home, proj)
    am.sync_memory_md_pointer(
        memory_id=1, mtype="insight", content="one", project_path=str(proj), home=home
    )
    r = am.sync_memory_md_pointer(
        memory_id=2, mtype="lesson", content="two", project_path=str(proj), home=home
    )
    text = Path(r["path"]).read_text(encoding="utf-8")
    assert text.count("- PM #") == 2
    assert "- PM #1 [insight]" in text and "- PM #2 [lesson]" in text


def test_bridge_preserves_existing_cc_content(tmp_path) -> None:
    home = tmp_path / "home"
    proj = _make_project(tmp_path)
    mem = _cc_memory_dir(home, proj)
    cc_line = "- [PyPI](project_pypi.md) — CC-managed index entry\n"
    (mem / am.MEMORY_INDEX_FILENAME).write_text(cc_line, encoding="utf-8")
    r = am.sync_memory_md_pointer(
        memory_id=5, mtype="insight", content="new", project_path=str(proj), home=home
    )
    text = Path(r["path"]).read_text(encoding="utf-8")
    assert cc_line.strip() in text  # CC content untouched
    assert am.BRIDGE_BEGIN in text  # bridge block appended after it


def test_bridge_self_heals_corrupted_block(tmp_path) -> None:
    home = tmp_path / "home"
    proj = _make_project(tmp_path)
    mem = _cc_memory_dir(home, proj)
    # begin marker present, no end marker (corrupted)
    (mem / am.MEMORY_INDEX_FILENAME).write_text(
        f"header\n{am.BRIDGE_BEGIN}\nstale junk without end\n", encoding="utf-8"
    )
    r = am.sync_memory_md_pointer(
        memory_id=9, mtype="lesson", content="heal", project_path=str(proj), home=home
    )
    text = Path(r["path"]).read_text(encoding="utf-8")
    assert text.count(am.BRIDGE_BEGIN) == 1
    assert text.count(am.BRIDGE_END) == 1
    assert "- PM #9 [lesson]" in text


def test_bridge_creates_dir_when_absent(tmp_path) -> None:
    # No ~/.claude/projects/<enc>/memory exists yet → bridge creates it.
    home = tmp_path / "home"
    proj = _make_project(tmp_path)
    r = am.sync_memory_md_pointer(
        memory_id=1, mtype="insight", content="bootstrap", project_path=str(proj), home=home
    )
    assert r["synced"] is True
    assert Path(r["path"]).exists()


# ─── pm_remember integration ─────────────────────────


def test_pm_remember_bridge_opt_in_writes(tmp_path, monkeypatch) -> None:
    home = tmp_path / "home"
    proj = _make_project(tmp_path)
    _cc_memory_dir(home, proj)
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setattr(srv, "PM_LENS_ENABLED", False)
    r = srv.pm_remember(
        "bridged insight", type="insight", project_path=str(proj), bridge_to_memory_md=True
    )
    assert "memory_md_synced" in r
    assert r["memory_md_synced"]["synced"] is True
    assert Path(r["memory_md_synced"]["path"]).exists()


def test_pm_remember_default_off_no_bridge(tmp_path, monkeypatch) -> None:
    home = tmp_path / "home"
    proj = _make_project(tmp_path)
    _cc_memory_dir(home, proj)
    monkeypatch.setenv("HOME", str(home))
    before = _snapshot(home)
    r = srv.pm_remember("unbridged", type="observation", project_path=str(proj))
    assert "memory_md_synced" not in r
    assert _snapshot(home) == before  # nothing written under ~/.claude


# ─── RO invariant: overlay is read-only even under PM_LENS=1 ──


def test_overlay_zero_fs_writes_under_lens(tmp_path, monkeypatch) -> None:
    """The include_auto_memory overlay READS ~/.claude but must never WRITE it,
    even under PM_LENS=1 where pm_recall is a registered (RO) tool. This is the
    behavioural half of the invariant the registration-membership assert cannot
    prove (memory:265)."""
    home, proj, _ = _overlay_project(tmp_path)
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setattr(srv, "PM_LENS_ENABLED", True)
    claude_root = home / ".claude"
    before = _snapshot(claude_root)
    r = srv.pm_recall(project_path=str(proj), include_auto_memory=True)
    # still returns data...
    assert len(r["auto_memory_entries"]) == 2
    # ...and wrote nothing under ~/.claude
    assert _snapshot(claude_root) == before


def test_reverse_bridge_suppressed_under_lens(tmp_path, monkeypatch) -> None:
    """Defense-in-depth: pm_remember's reverse bridge must not write ~/.claude
    when PM_LENS_ENABLED (the tool is already unregistered under Lens; this
    guards a direct in-process call)."""
    home = tmp_path / "home"
    proj = _make_project(tmp_path)
    _cc_memory_dir(home, proj)
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setattr(srv, "PM_LENS_ENABLED", True)
    claude_root = home / ".claude"
    before = _snapshot(claude_root)
    r = srv.pm_remember(
        "should not bridge", type="insight", project_path=str(proj), bridge_to_memory_md=True
    )
    assert "memory_md_synced" not in r  # guard suppressed the write
    assert _snapshot(claude_root) == before


# ─── Registration-gate membership (explicit, memory:265) ─────


def _reload_server(env_overrides: dict[str, str | None]) -> None:
    """Reload pmlens.server with PM_LENS applied (mirror of test_lens_mode)."""
    import os

    for key, value in env_overrides.items():
        if value is None:
            os.environ.pop(key, None)
        else:
            os.environ[key] = value
    srv._memory_stores.clear()
    srv.REGISTERED_TOOLS.clear()
    importlib.reload(srv)


@pytest.fixture
def lens_server(monkeypatch):
    monkeypatch.setenv("PM_LENS", "1")
    _reload_server({"PM_LENS": "1"})
    yield srv
    monkeypatch.delenv("PM_LENS", raising=False)
    _reload_server({"PM_LENS": None})


@pytest.fixture
def normal_server(monkeypatch):
    monkeypatch.delenv("PM_LENS", raising=False)
    _reload_server({"PM_LENS": None})
    yield srv


def test_pm_remember_hidden_under_lens(lens_server) -> None:
    # The reverse-bridge writer must stay unregistered under PM_LENS=1 — an
    # FS-diff sweep alone cannot catch a future mis-allowlisting (readonly store
    # swallows the write), so assert membership directly.
    assert "pm_remember" not in lens_server.REGISTERED_TOOLS


def test_pm_recall_registered_under_lens(lens_server) -> None:
    # The overlay-carrying reader stays available under Lens (it is read-only).
    assert "pm_recall" in lens_server.REGISTERED_TOOLS


def test_pm_remember_registered_in_code_mode(normal_server) -> None:
    # Positive control: without the flag, a name typo would make the hidden
    # assert vacuously pass — so pin that pm_remember IS registered at PM_LENS=0.
    assert "pm_remember" in normal_server.REGISTERED_TOOLS


# ─── Adversarial-review regressions (WF: pmserv-112-adversarial-review) ─────


def test_overlay_tolerates_non_utf8_file(tmp_path, monkeypatch) -> None:
    """[F1] A single non-UTF-8 .md file must NOT crash the overlay/pm_recall.

    UnicodeDecodeError ⊂ ValueError ⊄ OSError, so the read must decode-tolerate
    (errors='replace') and the scan loop must guard the parse call — a
    corrupted/binary note cannot take down a read-only tool."""
    home, proj, mem = _overlay_project(tmp_path)
    (mem / "corrupt.md").write_bytes(b"\xff\xfe binary garbage \x80\x81")
    monkeypatch.setenv("HOME", str(home))
    # Neither the pure overlay nor pm_recall may raise.
    ov = am.build_auto_memory_overlay(str(proj), None, 20, home=home)
    files = {e["source_file"] for e in ov["auto_memory_entries"]}
    assert {"feedback_x.md", "project_y.md"} <= files  # good files still surfaced
    r = srv.pm_recall(project_path=str(proj), include_auto_memory=True)
    assert "auto_memory_entries" in r  # degraded gracefully, no exception


def test_parse_handles_utf8_bom(tmp_path) -> None:
    """[F3] A leading UTF-8 BOM must not discard all frontmatter."""
    f = tmp_path / "bom.md"
    f.write_bytes(b"\xef\xbb\xbf" + NESTED_META.encode("utf-8"))
    entry = am.parse_auto_memory_file(f)
    assert entry is not None
    assert entry["type"] == "feedback"  # BOM stripped → frontmatter parsed
    assert entry["session_id"] == "sess-nested-1"


def test_bridge_excerpt_with_marker_token_does_not_corrupt(tmp_path) -> None:
    """[F2] A memory whose content contains BRIDGE_END must not smuggle a
    second marker into the block and break the splice on the next sync."""
    home = tmp_path / "home"
    proj = _make_project(tmp_path)
    _cc_memory_dir(home, proj)
    r1 = am.sync_memory_md_pointer(
        memory_id=1,
        mtype="insight",
        content=f"{am.BRIDGE_END} and trailing text",
        project_path=str(proj),
        home=home,
    )
    mp = Path(r1["path"])
    am.sync_memory_md_pointer(
        memory_id=2, mtype="lesson", content="second", project_path=str(proj), home=home
    )
    text = mp.read_text(encoding="utf-8")
    assert text.count(am.BRIDGE_BEGIN) == 1
    assert text.count(am.BRIDGE_END) == 1  # exactly one terminator, block intact
    assert "- PM #1 [insight]" in text and "- PM #2 [lesson]" in text


def test_locate_override_prefers_memory_child_over_stray_md(tmp_path) -> None:
    """[F4] Override at a project dir with a stray top-level *.md must still
    resolve to the memory/ child, not the parent."""
    parent = tmp_path / "proj"
    child = parent / "memory"
    child.mkdir(parents=True)
    (parent / "README.md").write_text("stray top-level md", encoding="utf-8")
    found = am.locate_auto_memory_dirs(auto_memory_path=str(parent))
    assert found == [child.resolve()]


def test_bridge_dedup_prefix_no_collision_10_vs_1(tmp_path) -> None:
    """[F5] The trailing space in the '- PM #<id> ' dedup prefix keeps #10 from
    being read as a duplicate of #1 (delete the space → this fails)."""
    home = tmp_path / "home"
    proj = _make_project(tmp_path)
    _cc_memory_dir(home, proj)
    am.sync_memory_md_pointer(
        memory_id=10, mtype="insight", content="ten", project_path=str(proj), home=home
    )
    r = am.sync_memory_md_pointer(
        memory_id=1, mtype="lesson", content="one", project_path=str(proj), home=home
    )
    text = Path(r["path"]).read_text(encoding="utf-8")
    assert "- PM #10 [insight]" in text
    assert "- PM #1 [lesson]" in text
    assert text.count("- PM #") == 2  # #1 was NOT swallowed as a prefix of #10


def test_pm_recall_type_filter_does_not_drop_overlay(tmp_path, monkeypatch) -> None:
    """[F6] type= filters ledger memories (MemoryType) but must not filter the
    overlay, whose raw origin types (feedback/project) are a different axis."""
    proj = _prime_recall(tmp_path, monkeypatch)
    r = srv.pm_recall(project_path=str(proj), type="insight", include_auto_memory=True)
    assert {e["source_file"] for e in r["auto_memory_entries"]} == {
        "feedback_x.md",
        "project_y.md",
    }


def test_overlay_dedups_same_basename_across_drift_dirs(tmp_path) -> None:
    """[F8] The same repo materialised under both the current and the legacy
    ~/.claude encoding must count a shared-basename file exactly once."""
    home = tmp_path / "home"
    proj = _make_project(tmp_path, name="under_score")  # underscore → distinct encodings
    root = proj.resolve()
    cur = home / ".claude" / "projects" / am.encode_project_dirname(root) / "memory"
    legacy = home / ".claude" / "projects" / str(root).replace("/", "-") / "memory"
    cur.mkdir(parents=True)
    legacy.mkdir(parents=True)
    (cur / "dup.md").write_text("---\ntype: project\n---\ncurrent body\n", encoding="utf-8")
    (legacy / "dup.md").write_text("---\ntype: project\n---\nlegacy body\n", encoding="utf-8")
    ov = am.build_auto_memory_overlay(str(proj), None, 20, home=home)
    dup = [e for e in ov["auto_memory_entries"] if e["source_file"] == "dup.md"]
    assert len(dup) == 1  # basename deduped across the two drift dirs
    assert ov["auto_memory_summary"]["scanned_dirs"] == 2


def test_pm_recall_cross_project_omits_overlay(tmp_path, monkeypatch) -> None:
    """[F9] The cross_project branch returns early and must not carry the
    overlay (parity with the task_id branch)."""
    proj = _prime_recall(tmp_path, monkeypatch)
    r = srv.pm_recall(
        query="anything", cross_project=True, project_path=str(proj), include_auto_memory=True
    )
    assert "auto_memory_entries" not in r
    assert "auto_memory_summary" not in r
