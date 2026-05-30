"""Tests for pm_server.rules.

PMSERV-043 smoke tests cover the legacy CLAUDE.md surface and the
backward-compat shim. PMSERV-044 adds tests for the multi-host
detection layer (``detect_hosts``, ``_has_pm_marker``) and the
data-class result types.

Functional behavior of ``ensure_claudemd`` / ``update_claudemd`` /
``get_claudemd_status`` is covered by ``tests/test_claudemd.py``,
which is kept untouched as the strongest v0.4.x compatibility guarantee.
"""

from __future__ import annotations

from dataclasses import FrozenInstanceError

import pytest

from pm_server.rules import (
    BEGIN_MARKER,
    BEGIN_PATTERN,
    CLAUDEMD_TEMPLATE,
    END_MARKER,
    OTHER_SECTION_PATTERN,
    TARGET_FILES,
    TEMPLATE_VERSION,
    InjectResult,
    InjectStatus,
    InjectSummary,
    _has_pm_marker,
    detect_hosts,
    ensure_claudemd,
    get_claudemd_status,
    inject_pm_rules,
    update_claudemd,
)


class TestRulesModule:
    """pm_server.rules public surface (PMSERV-043)."""

    def test_template_version_is_int(self):
        assert isinstance(TEMPLATE_VERSION, int)
        assert TEMPLATE_VERSION >= 1

    def test_template_version_pinned_at_v9(self):
        # ADR-008 4th-tier guard: a bump must be intentional. v9 adds the
        # X content pipeline rule (.pm → build-in-public X drafts; on-signal
        # propose, redact-only safety, never auto-post) — PMSERV-119 / ADR-024.
        # v8 added the memory-layer routing rule (PMSERV-111 / ADR-023).
        assert TEMPLATE_VERSION == 9

    def test_template_contains_x_content_pipeline_section(self):
        # PMSERV-119: the on-signal trigger rule must be present in the
        # template, including the load-bearing "never auto-post" invariant.
        assert "X コンテンツパイプライン" in CLAUDEMD_TEMPLATE
        assert "content-pipeline" in CLAUDEMD_TEMPLATE
        assert "pm_redact_draft" in CLAUDEMD_TEMPLATE
        assert "propose-don't-force" in CLAUDEMD_TEMPLATE

    def test_markers_are_strings(self):
        assert "pm-server:begin" in BEGIN_MARKER
        assert "pm-server:end" in END_MARKER

    def test_template_contains_markers(self):
        assert "<!-- pm-server:begin v={version} -->" in CLAUDEMD_TEMPLATE
        assert "<!-- pm-server:end -->" in CLAUDEMD_TEMPLATE

    def test_begin_pattern_matches_marker(self):
        match = BEGIN_PATTERN.search("<!-- pm-server:begin v=7 -->")
        assert match is not None
        assert match.group(1) == "7"

    def test_other_section_pattern_finds_named_sections(self):
        text = "<!-- foo:begin -->\n<!-- bar:begin -->"
        names = OTHER_SECTION_PATTERN.findall(text)
        assert "foo" in names
        assert "bar" in names

    def test_get_claudemd_status_returns_required_keys(self, tmp_path):
        result = get_claudemd_status(tmp_path)
        for key in ("exists", "has_pm_section", "version", "up_to_date", "other_rule_sections"):
            assert key in result

    def test_ensure_claudemd_creates_file(self, tmp_path):
        message = ensure_claudemd(tmp_path)
        assert (tmp_path / "CLAUDE.md").exists()
        assert "created" in message.lower() or "appended" in message.lower()

    def test_update_claudemd_creates_or_replaces(self, tmp_path):
        message = update_claudemd(tmp_path)
        assert (tmp_path / "CLAUDE.md").exists()
        assert isinstance(message, str)


class TestBackwardCompatShim:
    """pm_server.claudemd is a transparent re-export of pm_server.rules (PMSERV-043)."""

    def test_shim_re_exports_same_function_objects(self):
        import pm_server.claudemd as old_path
        import pm_server.rules as new_path

        assert old_path.ensure_claudemd is new_path.ensure_claudemd
        assert old_path.update_claudemd is new_path.update_claudemd
        assert old_path.get_claudemd_status is new_path.get_claudemd_status

    def test_shim_re_exports_same_constants(self):
        import pm_server.claudemd as old_path
        import pm_server.rules as new_path

        assert old_path.TEMPLATE_VERSION is new_path.TEMPLATE_VERSION
        assert old_path.CLAUDEMD_TEMPLATE is new_path.CLAUDEMD_TEMPLATE
        assert old_path.BEGIN_MARKER is new_path.BEGIN_MARKER
        assert old_path.END_MARKER is new_path.END_MARKER
        assert old_path.BEGIN_PATTERN is new_path.BEGIN_PATTERN
        assert old_path.OTHER_SECTION_PATTERN is new_path.OTHER_SECTION_PATTERN


# --- PMSERV-044: multi-host detection layer -------------------------------


@pytest.fixture
def no_claude_binary(monkeypatch):
    """Force ``shutil.which("claude")`` to return None inside rules.py."""
    monkeypatch.setattr("pm_server.rules.shutil.which", lambda name: None)


@pytest.fixture
def with_claude_binary(monkeypatch):
    """Force ``shutil.which("claude")`` to return a fake path."""
    monkeypatch.setattr(
        "pm_server.rules.shutil.which",
        lambda name: "/fake/bin/claude" if name == "claude" else None,
    )


@pytest.fixture
def fake_codex_config_present(tmp_path, monkeypatch):
    """Provide a writable ~/.codex/config.toml under tmp_path."""
    fake_home = tmp_path / "fake_home"
    fake_home.mkdir()
    codex_dir = fake_home / ".codex"
    codex_dir.mkdir()
    (codex_dir / "config.toml").write_text("# placeholder\n", encoding="utf-8")
    monkeypatch.setenv("HOME", str(fake_home))
    return codex_dir / "config.toml"


@pytest.fixture
def fake_codex_config_absent(tmp_path, monkeypatch):
    """Point HOME at a directory with no ~/.codex/config.toml."""
    fake_home = tmp_path / "fake_home_no_codex"
    fake_home.mkdir()
    monkeypatch.setenv("HOME", str(fake_home))
    return fake_home / ".codex" / "config.toml"


class TestHasPmMarker:
    def test_missing_file_is_false(self, tmp_path):
        assert _has_pm_marker(tmp_path / "missing.md") is False

    def test_file_without_marker_is_false(self, tmp_path):
        f = tmp_path / "plain.md"
        f.write_text("# Just a regular Markdown file\n", encoding="utf-8")
        assert _has_pm_marker(f) is False

    def test_file_with_marker_is_true(self, tmp_path):
        f = tmp_path / "managed.md"
        f.write_text(
            f"# Header\n\n<!-- pm-server:begin v={TEMPLATE_VERSION} -->\n"
            f"rules\n<!-- pm-server:end -->\n",
            encoding="utf-8",
        )
        assert _has_pm_marker(f) is True

    def test_old_version_marker_still_detected(self, tmp_path):
        """Even a stale v0 marker counts as 'managed' (positive signal)."""
        f = tmp_path / "old.md"
        f.write_text("<!-- pm-server:begin v=0 -->\nold\n<!-- pm-server:end -->\n")
        assert _has_pm_marker(f) is True


class TestDetectHosts:
    """Detection layer (PMSERV-044 spec v1, super-research synthesis).

    Strategy: filesystem (primary) + marker (positive) + CLAUDECODE
    (positive only, never negative judgment) + tertiary fallback with
    warning. Codex env vars are intentionally NOT consulted (Codex
    strips inherited env per shell_environment_policy).
    """

    def test_no_signals_falls_back_to_claude_code(
        self, tmp_path, no_claude_binary, fake_codex_config_absent, clean_host_env
    ):
        hosts, source = detect_hosts(tmp_path)
        assert hosts == ["claude-code"]
        assert source == "fallback"

    def test_claude_in_path_detected(
        self, tmp_path, with_claude_binary, fake_codex_config_absent, clean_host_env
    ):
        hosts, source = detect_hosts(tmp_path)
        assert hosts == ["claude-code"]
        assert source == "filesystem+marker+env"

    def test_codex_config_detected(
        self, tmp_path, no_claude_binary, fake_codex_config_present, clean_host_env
    ):
        hosts, source = detect_hosts(tmp_path)
        assert hosts == ["codex"]
        assert source == "filesystem+marker+env"

    def test_both_filesystem_signals_detected(
        self, tmp_path, with_claude_binary, fake_codex_config_present, clean_host_env
    ):
        hosts, source = detect_hosts(tmp_path)
        assert hosts == ["claude-code", "codex"]
        assert source == "filesystem+marker+env"

    def test_claude_marker_in_existing_file_detected(
        self, tmp_path, no_claude_binary, fake_codex_config_absent, clean_host_env
    ):
        """An existing CLAUDE.md with marker is positive signal even if
        the ``claude`` binary is not on PATH (e.g. in a Docker build)."""
        (tmp_path / "CLAUDE.md").write_text(
            f"<!-- pm-server:begin v={TEMPLATE_VERSION} -->\nx\n<!-- pm-server:end -->\n",
        )
        hosts, source = detect_hosts(tmp_path)
        assert hosts == ["claude-code"]
        assert source == "filesystem+marker+env"

    def test_codex_marker_in_existing_file_detected(
        self, tmp_path, no_claude_binary, fake_codex_config_absent, clean_host_env
    ):
        (tmp_path / "AGENTS.md").write_text(
            f"<!-- pm-server:begin v={TEMPLATE_VERSION} -->\ny\n<!-- pm-server:end -->\n",
        )
        hosts, source = detect_hosts(tmp_path)
        assert hosts == ["codex"]
        assert source == "filesystem+marker+env"

    def test_claudecode_env_var_detected(
        self, tmp_path, no_claude_binary, fake_codex_config_absent, monkeypatch
    ):
        monkeypatch.setenv("CLAUDECODE", "1")
        hosts, source = detect_hosts(tmp_path)
        assert hosts == ["claude-code"]
        assert source == "filesystem+marker+env"

    def test_user_provided_use_case_uc8(
        self, tmp_path, with_claude_binary, fake_codex_config_present, clean_host_env
    ):
        """UC8 (user-described primary use case): existing CLAUDE.md+marker
        from prior Claude Code use, then user starts Codex (so
        ``~/.codex/config.toml`` exists). pm_init must detect both hosts
        so AGENTS.md gets created on the next invocation. Cross-check R7."""
        (tmp_path / "CLAUDE.md").write_text(
            f"<!-- pm-server:begin v={TEMPLATE_VERSION} -->\nrules\n<!-- pm-server:end -->\n",
        )

        hosts, source = detect_hosts(tmp_path)

        assert hosts == ["claude-code", "codex"]
        assert source == "filesystem+marker+env"

    def test_returned_hosts_are_sorted_unique(
        self, tmp_path, with_claude_binary, fake_codex_config_absent, monkeypatch
    ):
        """Multiple positive signals for the same host produce no dup."""
        monkeypatch.setenv("CLAUDECODE", "1")  # adds claude-code (already in)
        (tmp_path / "CLAUDE.md").write_text(
            f"<!-- pm-server:begin v={TEMPLATE_VERSION} -->\n.\n<!-- pm-server:end -->\n"
        )
        hosts, _source = detect_hosts(tmp_path)
        assert hosts == ["claude-code"]
        assert len(hosts) == len(set(hosts))


class TestTargetFiles:
    def test_target_files_maps_known_hosts(self):
        assert TARGET_FILES == {"claude-code": "CLAUDE.md", "codex": "AGENTS.md"}


class TestInjectResultDataclass:
    def test_minimal_construction(self):
        r = InjectResult(
            target_file="CLAUDE.md",
            host="claude-code",
            status="created",
            message="created",
        )
        assert r.target_file == "CLAUDE.md"
        assert r.backup_path is None
        assert r.is_dry_run is False

    def test_is_frozen(self):
        r = InjectResult(
            target_file="CLAUDE.md",
            host="claude-code",
            status="created",
            message="created",
        )
        with pytest.raises(FrozenInstanceError):
            r.status = "updated"  # type: ignore[misc]


class TestInjectSummaryDataclass:
    def test_default_construction_yields_empty_lists(self):
        s = InjectSummary()
        assert s.results == []
        assert s.detected_hosts == []
        assert s.detection_source == "explicit"
        assert s.created == []
        assert s.updated == []
        assert s.overall_status == "skipped"

    def test_is_frozen(self):
        s = InjectSummary()
        with pytest.raises(FrozenInstanceError):
            s.overall_status = "failed"  # type: ignore[misc]

    def test_construction_with_results(self):
        r1 = InjectResult(
            target_file="CLAUDE.md",
            host="claude-code",
            status="updated",
            message="updated PM Server rules",
        )
        s = InjectSummary(
            results=[r1],
            detected_hosts=["claude-code"],
            detection_source="filesystem+marker+env",
            updated=["CLAUDE.md"],
            overall_status="updated",
        )
        assert len(s.results) == 1
        assert s.results[0] is r1
        assert s.overall_status == "updated"


class TestInjectPmRules:
    """``inject_pm_rules`` orchestrator: target dispatch + per-host inject.

    Covers the full 4 detection scenarios × {explicit/auto} matrix plus
    dry-run, backup symmetry (CLAUDE.md + AGENTS.md, PMSERV-058),
    partial-failure isolation, and the user-described UC8 acceptance
    scenario.
    """

    # --- target="claude-code" (single-host explicit) ------------------

    def test_claude_code_target_creates_new_file(self, tmp_path):
        summary = inject_pm_rules(tmp_path, target="claude-code")

        assert (tmp_path / "CLAUDE.md").exists()
        assert summary.created == ["CLAUDE.md"]
        assert summary.overall_status == "created"
        assert summary.detection_source == "explicit"
        assert len(summary.results) == 1
        assert summary.results[0].host == "claude-code"
        assert summary.results[0].is_dry_run is False
        # No backup for a *new* file (nothing to back up)
        assert summary.results[0].backup_path is None

    def test_claude_code_target_updates_existing_with_old_marker(self, tmp_path):
        (tmp_path / "CLAUDE.md").write_text(
            "# Header\n\n<!-- pm-server:begin v=0 -->\nold\n<!-- pm-server:end -->\n"
        )

        summary = inject_pm_rules(tmp_path, target="claude-code")

        assert summary.overall_status == "updated"
        assert summary.updated == ["CLAUDE.md"]
        new_content = (tmp_path / "CLAUDE.md").read_text()
        assert f"v={TEMPLATE_VERSION}" in new_content
        assert "v=0" not in new_content
        assert "# Header" in new_content  # surrounding content preserved

    def test_claude_code_target_appends_when_no_markers(self, tmp_path):
        (tmp_path / "CLAUDE.md").write_text("# Existing project notes\n")

        summary = inject_pm_rules(tmp_path, target="claude-code")

        assert summary.overall_status == "updated"  # appended → updated aggregate
        assert summary.results[0].status == "appended"
        new_content = (tmp_path / "CLAUDE.md").read_text()
        assert "# Existing project notes" in new_content
        assert "<!-- pm-server:begin" in new_content

    def test_claude_code_target_creates_backup_when_claude_md_exists(self, tmp_path):
        # PMSERV-058 / ADR-008 amendment A5: CLAUDE.md is now backed up before
        # an existing file is overwritten, symmetric with AGENTS.md.
        (tmp_path / "CLAUDE.md").write_text(
            "# Header\n\n<!-- pm-server:begin v=0 -->\nold\n<!-- pm-server:end -->\n"
        )

        summary = inject_pm_rules(tmp_path, target="claude-code")

        assert summary.overall_status == "updated"
        assert summary.results[0].backup_path is not None
        assert summary.results[0].backup_path.exists()
        assert summary.results[0].backup_path.name.startswith("CLAUDE.md.bak.")

    def test_claude_code_target_no_backup_in_dry_run(self, tmp_path):
        # Dry-run symmetry: no backup, no write (PMSERV-058).
        (tmp_path / "CLAUDE.md").write_text(
            f"<!-- pm-server:begin v={TEMPLATE_VERSION} -->\nx\n<!-- pm-server:end -->\n"
        )

        summary = inject_pm_rules(tmp_path, target="claude-code", dry_run=True)

        assert summary.results[0].backup_path is None
        assert list(tmp_path.glob("CLAUDE.md.bak.*")) == []

    def test_claude_code_target_noop_when_already_current(self, tmp_path):
        # PMSERV-062: re-injecting into a file that already holds the current
        # template is a no-op — reported "skipped" (not "updated"), with no
        # backup, no rewrite, and not counted in `updated`.
        inject_pm_rules(tmp_path, target="claude-code")  # first create
        before = (tmp_path / "CLAUDE.md").read_text()

        summary = inject_pm_rules(tmp_path, target="claude-code")  # re-apply

        assert summary.results[0].status == "skipped"
        assert "already up to date" in summary.results[0].message
        assert "updated" not in summary.results[0].message
        assert summary.results[0].backup_path is None
        assert list(tmp_path.glob("CLAUDE.md.bak.*")) == []
        assert (tmp_path / "CLAUDE.md").read_text() == before  # unchanged
        assert summary.overall_status == "skipped"
        assert summary.updated == []

    def test_claude_code_target_dry_run_noop_reports_skipped(self, tmp_path):
        # PMSERV-062: dry-run on an up-to-date file distinguishes the no-op
        # instead of misreporting it as "updated".
        inject_pm_rules(tmp_path, target="claude-code")  # first create

        summary = inject_pm_rules(tmp_path, target="claude-code", dry_run=True)

        assert summary.results[0].status == "skipped"
        assert summary.results[0].is_dry_run is True
        assert "already up to date" in summary.results[0].message

    # --- target="codex" (AGENTS.md path + backup symmetry) ------------

    def test_codex_target_creates_agents_md_no_backup_for_new_file(self, tmp_path):
        summary = inject_pm_rules(tmp_path, target="codex")

        assert (tmp_path / "AGENTS.md").exists()
        assert summary.created == ["AGENTS.md"]
        # No backup for a *new* file (nothing to back up)
        assert summary.results[0].backup_path is None

    def test_codex_target_creates_backup_when_agents_md_exists(self, tmp_path):
        (tmp_path / "AGENTS.md").write_text(
            f"# Agents\n\n<!-- pm-server:begin v={TEMPLATE_VERSION} -->\n"
            f"old\n<!-- pm-server:end -->\n",
        )

        summary = inject_pm_rules(tmp_path, target="codex")

        assert summary.overall_status == "updated"
        assert summary.results[0].backup_path is not None
        assert summary.results[0].backup_path.exists()
        assert summary.results[0].backup_path.name.startswith("AGENTS.md.bak.")

    def test_codex_target_no_backup_in_dry_run(self, tmp_path):
        (tmp_path / "AGENTS.md").write_text(
            f"<!-- pm-server:begin v={TEMPLATE_VERSION} -->\nx\n<!-- pm-server:end -->\n"
        )

        summary = inject_pm_rules(tmp_path, target="codex", dry_run=True)

        # Dry-run must not write a backup or modify the original
        assert summary.results[0].backup_path is None
        assert summary.results[0].is_dry_run is True
        assert list(tmp_path.glob("AGENTS.md.bak.*")) == []

    # --- target="all" (force both hosts unconditionally) --------------

    def test_all_target_processes_both_hosts(self, tmp_path):
        summary = inject_pm_rules(tmp_path, target="all")

        assert (tmp_path / "CLAUDE.md").exists()
        assert (tmp_path / "AGENTS.md").exists()
        assert set(summary.created) == {"CLAUDE.md", "AGENTS.md"}
        assert summary.detection_source == "explicit"
        assert {r.host for r in summary.results} == {"claude-code", "codex"}

    # --- target="auto" (detection-based) ------------------------------

    def test_auto_target_uses_detection(
        self, tmp_path, with_claude_binary, fake_codex_config_present, clean_host_env
    ):
        """Auto mode dispatches based on ``detect_hosts`` output."""
        summary = inject_pm_rules(tmp_path, target="auto")

        assert summary.detection_source == "filesystem+marker+env"
        assert set(summary.detected_hosts) == {"claude-code", "codex"}
        assert (tmp_path / "CLAUDE.md").exists()
        assert (tmp_path / "AGENTS.md").exists()

    def test_auto_target_falls_back_to_claude_code(
        self, tmp_path, no_claude_binary, fake_codex_config_absent, clean_host_env
    ):
        summary = inject_pm_rules(tmp_path, target="auto")

        assert summary.detection_source == "fallback"
        assert summary.detected_hosts == ["claude-code"]
        assert (tmp_path / "CLAUDE.md").exists()
        assert not (tmp_path / "AGENTS.md").exists()  # codex not detected

    # --- UC8: user-described primary use case (cross-check R7) --------

    def test_uc8_codex_started_creates_agents_md(
        self, tmp_path, with_claude_binary, fake_codex_config_present, clean_host_env
    ):
        """UC8: existing CLAUDE.md+marker (from prior Claude Code use)
        plus newly-installed Codex (~/.codex/config.toml present) →
        ``inject_pm_rules(target="auto")`` creates AGENTS.md while
        leaving the existing CLAUDE.md idempotent. Cross-check R7."""
        # Set up prior Claude Code state
        (tmp_path / "CLAUDE.md").write_text(
            f"<!-- pm-server:begin v={TEMPLATE_VERSION} -->\nexisting\n<!-- pm-server:end -->\n",
        )

        summary = inject_pm_rules(tmp_path, target="auto")

        # Both hosts detected → both processed
        assert set(summary.detected_hosts) == {"claude-code", "codex"}
        # AGENTS.md is newly created
        assert "AGENTS.md" in summary.created
        # CLAUDE.md is updated (always-replace semantics, but content
        # ends up byte-identical to the input since v=current)
        assert "CLAUDE.md" in summary.updated
        assert (tmp_path / "AGENTS.md").exists()

    # --- dry-run --------------------------------------------------------

    def test_dry_run_does_not_write_files(self, tmp_path):
        summary = inject_pm_rules(tmp_path, target="all", dry_run=True)

        # Files NOT written, but results still describe what would happen
        assert not (tmp_path / "CLAUDE.md").exists()
        assert not (tmp_path / "AGENTS.md").exists()
        assert all(r.is_dry_run for r in summary.results)
        # Per-result status reflects intended action
        statuses = [r.status for r in summary.results]
        assert all(s == "created" for s in statuses)

    # --- error handling -------------------------------------------------

    def test_unknown_target_raises_value_error(self, tmp_path):
        with pytest.raises(ValueError, match="unknown target"):
            inject_pm_rules(tmp_path, target="invalid")

    def test_partial_failure_does_not_abort_siblings(self, tmp_path, monkeypatch):
        """Per-host failures are isolated (cross-check D1)."""
        from pm_server import rules as rules_mod

        original = rules_mod._inject_into_file

        def selective_failer(path, host, *, dry_run=False):
            if host == "claude-code":
                raise OSError("simulated claude-code write failure")
            return original(path, host, dry_run=dry_run)

        monkeypatch.setattr(rules_mod, "_inject_into_file", selective_failer)

        summary = inject_pm_rules(tmp_path, target="all")

        # codex sibling should still succeed
        codex_results = [r for r in summary.results if r.host == "codex"]
        claude_results = [r for r in summary.results if r.host == "claude-code"]
        assert len(codex_results) == 1
        assert codex_results[0].status == "created"
        assert (tmp_path / "AGENTS.md").exists()

        # claude-code is recorded as failed
        assert len(claude_results) == 1
        assert claude_results[0].status == "failed"
        assert "simulated" in claude_results[0].message

        # Aggregate priority: failed wins
        assert summary.overall_status == "failed"

    def test_overall_status_priority_updated_over_created(self, tmp_path):
        """When one host is created and another is updated, aggregate
        is ``"updated"`` (priority order ``failed > skipped > updated >
        created``, cross-check D1)."""
        # CLAUDE.md exists with marker → updated
        (tmp_path / "CLAUDE.md").write_text(
            "<!-- pm-server:begin v=0 -->\nold\n<!-- pm-server:end -->\n",
        )
        # AGENTS.md missing → created

        summary = inject_pm_rules(tmp_path, target="all")

        assert summary.overall_status == "updated"
        assert "AGENTS.md" in summary.created
        assert "CLAUDE.md" in summary.updated

    def test_handles_corrupted_marker_begin_without_end(self, tmp_path):
        """Existing file has begin marker but no end marker — inject
        replaces from begin onward (matches update_claudemd corrupted
        semantic). Covers rules.py:506-509."""
        (tmp_path / "CLAUDE.md").write_text(
            "# Header\n\n<!-- pm-server:begin v=0 -->\nold orphan\n# Tail\n"
        )

        summary = inject_pm_rules(tmp_path, target="claude-code")

        assert summary.overall_status == "updated"
        new_content = (tmp_path / "CLAUDE.md").read_text()
        assert "# Header" in new_content
        assert "old orphan" not in new_content  # corrupted section removed
        assert f"v={TEMPLATE_VERSION}" in new_content
        assert "replaced corrupted" in summary.results[0].message

    def test_inject_failure_on_write_yields_failed_status(self, tmp_path, monkeypatch):
        """Atomic-write failure surfaces as ``status="failed"`` rather
        than propagating an exception. Covers rules.py:535-536."""
        from pm_server import rules as rules_mod

        # Create the existing file so we hit the 'write existing' path
        (tmp_path / "CLAUDE.md").write_text(
            "<!-- pm-server:begin v=0 -->\nold\n<!-- pm-server:end -->\n"
        )

        def failing_write(path, content, *, encoding="utf-8"):
            raise OSError("simulated atomic write failure")

        monkeypatch.setattr(rules_mod, "_atomic_write_text", failing_write)

        summary = inject_pm_rules(tmp_path, target="claude-code")

        assert summary.overall_status == "failed"
        assert summary.results[0].status == "failed"
        assert "simulated atomic write failure" in summary.results[0].message


class TestScanRuleFileAndStatus:
    """Coverage for ``_scan_rule_file`` body via ``get_rules_status``."""

    def test_get_rules_status_reflects_existing_claude_md(self, tmp_path):
        """Existing CLAUDE.md with marker hits the read+regex path.
        Covers rules.py:279-290."""
        from pm_server.rules import get_rules_status

        (tmp_path / "CLAUDE.md").write_text(
            f"# Project\n\n<!-- pm-server:begin v={TEMPLATE_VERSION} -->\n"
            f"rules\n<!-- pm-server:end -->\n"
            f"<!-- synaptic-ledger:begin v=1 -->\nx\n<!-- synaptic-ledger:end -->\n",
        )

        status = get_rules_status(tmp_path)

        assert status["claude_code"]["exists"] is True
        assert status["claude_code"]["has_pm_section"] is True
        assert status["claude_code"]["version"] == TEMPLATE_VERSION
        assert status["claude_code"]["up_to_date"] is True
        assert status["claude_code"]["other_rule_sections"] == ["synaptic-ledger"]

    def test_get_rules_status_reflects_existing_agents_md(self, tmp_path):
        from pm_server.rules import get_rules_status

        (tmp_path / "AGENTS.md").write_text(
            "# Agents\n\n<!-- pm-server:begin v=0 -->\nlegacy\n<!-- pm-server:end -->\n",
        )

        status = get_rules_status(tmp_path)

        assert status["codex"]["exists"] is True
        assert status["codex"]["has_pm_section"] is True
        assert status["codex"]["version"] == 0
        assert status["codex"]["up_to_date"] is False  # below current TEMPLATE_VERSION


class TestAggregateOverallStatus:
    def test_empty_results_aggregates_to_skipped(self):
        """Empty results list aggregates to ``"skipped"`` (default).
        Covers rules.py:582."""
        from pm_server.rules import _aggregate_overall_status

        assert _aggregate_overall_status([]) == "skipped"

    def test_inject_status_priority_covers_all_statuses(self):
        """PMSERV-110: every InjectStatus member must have a priority slot, so
        _aggregate_overall_status never falls through to the empty-results
        sentinel for a non-empty result list. Guards the InjectStatus Literal
        and _INJECT_STATUS_PRIORITY against drifting apart (mirrors the
        installer.InstallStatus drift guard)."""
        from typing import get_args

        from pm_server.rules import _INJECT_STATUS_PRIORITY

        assert set(_INJECT_STATUS_PRIORITY) == set(get_args(InjectStatus))
        assert len(_INJECT_STATUS_PRIORITY) == len(get_args(InjectStatus))  # no dupes


class TestCliUpdateRules:
    """``pm-server update-rules`` CLI subcommand (PMSERV-044 Step 4).

    Patches ``pm_server.rules.inject_pm_rules`` to exercise only the CLI
    surface (option parsing, output rendering, exit code). Functional
    behaviour is covered by ``TestInjectPmRules``. Cross-check R6 is
    enforced by ``test_dry_run_tag_appears_exactly_once``.
    """

    @staticmethod
    def _ok_summary(*, dry_run: bool = False, source: str = "explicit") -> InjectSummary:
        return InjectSummary(
            results=[
                InjectResult(
                    target_file="CLAUDE.md",
                    host="claude-code",
                    status="created",
                    message="created CLAUDE.md with PM Server rules",
                    is_dry_run=dry_run,
                ),
            ],
            detected_hosts=["claude-code"],
            detection_source=source,
            created=["CLAUDE.md"],
            overall_status="created",
        )

    @staticmethod
    def _ok_summary_both_hosts(dry_run: bool = False) -> InjectSummary:
        return InjectSummary(
            results=[
                InjectResult(
                    target_file="CLAUDE.md",
                    host="claude-code",
                    status="updated",
                    message="updated PM Server rules in CLAUDE.md",
                    is_dry_run=dry_run,
                ),
                InjectResult(
                    target_file="AGENTS.md",
                    host="codex",
                    status="created",
                    message="created AGENTS.md with PM Server rules",
                    is_dry_run=dry_run,
                ),
            ],
            detected_hosts=["claude-code", "codex"],
            detection_source="explicit",
            created=["AGENTS.md"],
            updated=["CLAUDE.md"],
            overall_status="updated",
        )

    def test_default_target_auto_propagates(self, monkeypatch, tmp_path):
        from click.testing import CliRunner

        from pm_server.__main__ import cli

        # CLI walks up from cwd to find .pm/. Make tmp_path a real project root.
        (tmp_path / ".pm").mkdir()
        (tmp_path / ".pm" / "project.yaml").write_text("name: t\n")
        monkeypatch.chdir(tmp_path)

        captured = {}

        def fake_inject(root, *, target="auto", dry_run=False):
            captured["target"] = target
            captured["dry_run"] = dry_run
            return self._ok_summary()

        monkeypatch.setattr("pm_server.rules.inject_pm_rules", fake_inject)
        result = CliRunner().invoke(cli, ["update-rules"])

        assert result.exit_code == 0
        assert captured == {"target": "auto", "dry_run": False}
        assert "✓ CLAUDE.md: created CLAUDE.md with PM Server rules" in result.output

    def test_target_codex_dispatch(self, monkeypatch, tmp_path):
        from click.testing import CliRunner

        from pm_server.__main__ import cli

        (tmp_path / ".pm").mkdir()
        (tmp_path / ".pm" / "project.yaml").write_text("name: t\n")
        monkeypatch.chdir(tmp_path)

        def fake_inject(root, *, target="auto", dry_run=False):
            assert target == "codex"
            return InjectSummary(
                results=[
                    InjectResult(
                        target_file="AGENTS.md",
                        host="codex",
                        status="created",
                        message="created AGENTS.md with PM Server rules",
                    )
                ],
                created=["AGENTS.md"],
                detection_source="explicit",
                overall_status="created",
            )

        monkeypatch.setattr("pm_server.rules.inject_pm_rules", fake_inject)
        result = CliRunner().invoke(cli, ["update-rules", "--target", "codex"])

        assert result.exit_code == 0
        assert "✓ AGENTS.md:" in result.output

    def test_dry_run_tag_appears_exactly_once(self, monkeypatch, tmp_path):
        """Cross-check R6 / PMSERV-039 L1 lesson regression guard:
        ``[dry-run]`` is rendered by ``_print_inject_summary`` only,
        never embedded in ``InjectResult.message``."""
        from click.testing import CliRunner

        from pm_server.__main__ import cli

        (tmp_path / ".pm").mkdir()
        (tmp_path / ".pm" / "project.yaml").write_text("name: t\n")
        monkeypatch.chdir(tmp_path)

        captured = {}

        def fake_inject(root, *, target="auto", dry_run=False):
            captured["dry_run"] = dry_run
            return self._ok_summary(dry_run=True)

        monkeypatch.setattr("pm_server.rules.inject_pm_rules", fake_inject)
        result = CliRunner().invoke(cli, ["update-rules", "--target", "claude-code", "--dry-run"])

        assert result.exit_code == 0
        assert captured["dry_run"] is True
        # Single-source tag invariant — exactly one [dry-run] in the output.
        assert result.output.count("[dry-run]") == 1
        assert "[dry-run] CLAUDE.md:" in result.output

    def test_fallback_emits_warning_line(self, monkeypatch, tmp_path):
        from click.testing import CliRunner

        from pm_server.__main__ import cli

        (tmp_path / ".pm").mkdir()
        (tmp_path / ".pm" / "project.yaml").write_text("name: t\n")
        monkeypatch.chdir(tmp_path)

        def fake_inject(root, *, target="auto", dry_run=False):
            return self._ok_summary(source="fallback")

        monkeypatch.setattr("pm_server.rules.inject_pm_rules", fake_inject)
        result = CliRunner().invoke(cli, ["update-rules"])

        assert result.exit_code == 0
        # Warning line precedes the host result line.
        assert "⚠" in result.output
        assert "Defaulted to claude-code" in result.output

    def test_failed_status_yields_exit_code_1(self, monkeypatch, tmp_path):
        from click.testing import CliRunner

        from pm_server.__main__ import cli

        (tmp_path / ".pm").mkdir()
        (tmp_path / ".pm" / "project.yaml").write_text("name: t\n")
        monkeypatch.chdir(tmp_path)

        def fake_inject(root, *, target="auto", dry_run=False):
            return InjectSummary(
                results=[
                    InjectResult(
                        target_file="CLAUDE.md",
                        host="claude-code",
                        status="failed",
                        message="failed to write CLAUDE.md: simulated",
                    )
                ],
                detection_source="explicit",
                overall_status="failed",
            )

        monkeypatch.setattr("pm_server.rules.inject_pm_rules", fake_inject)
        result = CliRunner().invoke(cli, ["update-rules", "--target", "claude-code"])

        assert result.exit_code == 1
        assert "✗ CLAUDE.md:" in result.output

    def test_backup_path_displayed_indented(self, monkeypatch, tmp_path):
        """Cross-check R6: backup_path is displayed by the CLI layer
        (not embedded in InjectResult.message)."""
        from pathlib import Path

        from click.testing import CliRunner

        from pm_server.__main__ import cli

        (tmp_path / ".pm").mkdir()
        (tmp_path / ".pm" / "project.yaml").write_text("name: t\n")
        monkeypatch.chdir(tmp_path)

        backup = Path("/fake/AGENTS.md.bak.20260430-180000")

        def fake_inject(root, *, target="auto", dry_run=False):
            return InjectSummary(
                results=[
                    InjectResult(
                        target_file="AGENTS.md",
                        host="codex",
                        status="updated",
                        message="updated PM Server rules in AGENTS.md (v6 → v7)",
                        backup_path=backup,
                    )
                ],
                updated=["AGENTS.md"],
                detection_source="explicit",
                overall_status="updated",
            )

        monkeypatch.setattr("pm_server.rules.inject_pm_rules", fake_inject)
        result = CliRunner().invoke(cli, ["update-rules", "--target", "codex"])

        assert result.exit_code == 0
        assert "backup: /fake/AGENTS.md.bak.20260430-180000" in result.output
        # backup path NOT embedded in the per-host message line itself.
        assert (
            "AGENTS.md.bak"
            not in result.output.split("\n")[
                [i for i, line in enumerate(result.output.split("\n")) if "AGENTS.md:" in line][0]
            ]
        )
