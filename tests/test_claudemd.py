"""Tests for CLAUDE.md auto-management."""

import pytest

from pmlens.claudemd import (
    TEMPLATE_VERSION,
    ensure_claudemd,
    get_claudemd_status,
    update_claudemd,
)


@pytest.fixture
def project_dir(tmp_path):
    """Create a minimal project directory with .pm/."""
    pm_dir = tmp_path / ".pm"
    pm_dir.mkdir()
    (pm_dir / "project.yaml").write_text("name: test\n")
    return tmp_path


class TestGetClaudemdStatus:
    def test_no_claudemd(self, project_dir):
        status = get_claudemd_status(project_dir)
        assert status["exists"] is False
        assert status["has_pm_section"] is False
        assert status["version"] is None
        assert status["up_to_date"] is False
        assert status["other_rule_sections"] == []

    def test_claudemd_without_markers(self, project_dir):
        (project_dir / "CLAUDE.md").write_text("# My Project\n")
        status = get_claudemd_status(project_dir)
        assert status["exists"] is True
        assert status["has_pm_section"] is False
        assert status["version"] is None
        assert status["other_rule_sections"] == []

    def test_claudemd_with_current_markers(self, project_dir):
        content = (
            f"# My Project\n\n"
            f"<!-- pm-server:begin v={TEMPLATE_VERSION} -->\n"
            f"rules\n"
            f"<!-- pm-server:end -->\n"
        )
        (project_dir / "CLAUDE.md").write_text(content)
        status = get_claudemd_status(project_dir)
        assert status["has_pm_section"] is True
        assert status["version"] == TEMPLATE_VERSION
        assert status["up_to_date"] is True

    def test_claudemd_with_old_markers(self, project_dir):
        content = "<!-- pm-server:begin v=0 -->\nold rules\n<!-- pm-server:end -->\n"
        (project_dir / "CLAUDE.md").write_text(content)
        status = get_claudemd_status(project_dir)
        assert status["has_pm_section"] is True
        assert status["version"] == 0
        assert status["up_to_date"] is False


class TestOtherRuleSections:
    def test_no_other_sections(self, project_dir):
        """Only pm-server markers present → empty list."""
        content = f"<!-- pm-server:begin v={TEMPLATE_VERSION} -->\nrules\n<!-- pm-server:end -->\n"
        (project_dir / "CLAUDE.md").write_text(content)
        status = get_claudemd_status(project_dir)
        assert status["other_rule_sections"] == []

    def test_one_other_section(self, project_dir):
        """pm-server + synaptic-ledger → detects synaptic-ledger."""
        content = (
            f"<!-- pm-server:begin v={TEMPLATE_VERSION} -->\nrules\n<!-- pm-server:end -->\n"
            f"<!-- synaptic-ledger:begin v=1 -->\nledger rules\n<!-- synaptic-ledger:end -->\n"
        )
        (project_dir / "CLAUDE.md").write_text(content)
        status = get_claudemd_status(project_dir)
        assert status["other_rule_sections"] == ["synaptic-ledger"]

    def test_multiple_other_sections(self, project_dir):
        """Multiple MCP rule sections → all detected except pm-server."""
        content = (
            f"<!-- pm-server:begin v={TEMPLATE_VERSION} -->\nrules\n<!-- pm-server:end -->\n"
            f"<!-- synaptic-ledger:begin v=1 -->\nrules\n<!-- synaptic-ledger:end -->\n"
            f"<!-- code-review-bot:begin v=2 -->\nrules\n<!-- code-review-bot:end -->\n"
        )
        (project_dir / "CLAUDE.md").write_text(content)
        status = get_claudemd_status(project_dir)
        assert "synaptic-ledger" in status["other_rule_sections"]
        assert "code-review-bot" in status["other_rule_sections"]
        assert "pm-server" not in status["other_rule_sections"]
        assert len(status["other_rule_sections"]) == 2

    def test_pm_server_excluded_from_other(self, project_dir):
        """pm-server is always excluded from other_rule_sections."""
        content = f"<!-- pm-server:begin v={TEMPLATE_VERSION} -->\nrules\n<!-- pm-server:end -->\n"
        (project_dir / "CLAUDE.md").write_text(content)
        status = get_claudemd_status(project_dir)
        assert "pm-server" not in status["other_rule_sections"]

    def test_underscore_names_detected(self, project_dir):
        """Section names with underscores are also detected."""
        content = (
            f"<!-- pm-server:begin v={TEMPLATE_VERSION} -->\nrules\n<!-- pm-server:end -->\n"
            f"<!-- my_custom_tool:begin v=1 -->\nrules\n<!-- my_custom_tool:end -->\n"
        )
        (project_dir / "CLAUDE.md").write_text(content)
        status = get_claudemd_status(project_dir)
        assert status["other_rule_sections"] == ["my_custom_tool"]


class TestEnsureClaudemd:
    def test_creates_new_file(self, project_dir):
        result = ensure_claudemd(project_dir)
        assert "created" in result
        content = (project_dir / "CLAUDE.md").read_text()
        assert f"v={TEMPLATE_VERSION}" in content
        assert "pm_status" in content
        assert content.endswith("\n")

    def test_appends_to_existing(self, project_dir):
        (project_dir / "CLAUDE.md").write_text("# My Project\n\nExisting content.\n")
        result = ensure_claudemd(project_dir)
        assert "appended" in result
        content = (project_dir / "CLAUDE.md").read_text()
        assert "# My Project" in content
        assert "pm_status" in content

    def test_appends_with_proper_separator(self, project_dir):
        # No trailing newline
        (project_dir / "CLAUDE.md").write_text("# My Project")
        ensure_claudemd(project_dir)
        content = (project_dir / "CLAUDE.md").read_text()
        # Should have blank line between existing content and PM section
        assert "# My Project\n\n<!-- pm-server:begin" in content

    def test_appends_with_single_newline(self, project_dir):
        (project_dir / "CLAUDE.md").write_text("# My Project\n")
        ensure_claudemd(project_dir)
        content = (project_dir / "CLAUDE.md").read_text()
        assert "# My Project\n\n<!-- pm-server:begin" in content

    def test_skips_if_up_to_date(self, project_dir):
        ensure_claudemd(project_dir)  # first call
        result = ensure_claudemd(project_dir)  # second call
        assert "skipped" in result

    def test_updates_old_version(self, project_dir):
        old = (
            "# My Project\n\n<!-- pm-server:begin v=0 -->\nold\n<!-- pm-server:end -->\n\n# Other\n"
        )
        (project_dir / "CLAUDE.md").write_text(old)
        result = ensure_claudemd(project_dir)
        assert "updated" in result
        content = (project_dir / "CLAUDE.md").read_text()
        assert f"v={TEMPLATE_VERSION}" in content
        assert "# My Project" in content  # before preserved
        assert "# Other" in content  # after preserved
        assert "v=0" not in content  # old version gone


class TestUpdateClaudemd:
    def test_creates_new_file(self, project_dir):
        result = update_claudemd(project_dir)
        assert "created" in result
        assert (project_dir / "CLAUDE.md").exists()

    def test_replaces_even_if_current(self, project_dir):
        ensure_claudemd(project_dir)
        result = update_claudemd(project_dir)
        # update always replaces, even if same version
        assert "updated" in result

    def test_preserves_surrounding_content(self, project_dir):
        content = (
            "# Header\n\n<!-- pm-server:begin v=0 -->\nold\n<!-- pm-server:end -->\n\n# Footer\n"
        )
        (project_dir / "CLAUDE.md").write_text(content)
        update_claudemd(project_dir)
        new_content = (project_dir / "CLAUDE.md").read_text()
        assert "# Header" in new_content
        assert "# Footer" in new_content
        assert f"v={TEMPLATE_VERSION}" in new_content

    def test_appends_when_no_markers(self, project_dir):
        (project_dir / "CLAUDE.md").write_text("# Existing\n")
        result = update_claudemd(project_dir)
        assert "appended" in result

    def test_handles_corrupted_markers_begin_only(self, project_dir):
        # begin marker but no end marker
        content = "# Header\n\n<!-- pm-server:begin v=0 -->\nold content\n"
        (project_dir / "CLAUDE.md").write_text(content)
        result = update_claudemd(project_dir)
        assert "corrupted" in result
        new_content = (project_dir / "CLAUDE.md").read_text()
        assert f"v={TEMPLATE_VERSION}" in new_content


class TestCorruptedMarkers:
    def test_corrupted_begin_only_appends_clean(self, project_dir):
        """begin マーカーのみで end がない場合、古い残骸を含まない形で追記する。"""
        content = (
            "# Header\n\n<!-- pm-server:begin v=0 -->\nold rules without end marker\n\n# Footer\n"
        )
        (project_dir / "CLAUDE.md").write_text(content)
        update_claudemd(project_dir)
        new_content = (project_dir / "CLAUDE.md").read_text()
        assert "# Header" in new_content
        assert "# Footer" not in new_content  # Footer was after corrupted begin, so it's removed
        assert f"v={TEMPLATE_VERSION}" in new_content
        assert "old rules without end marker" not in new_content


class TestIdempotency:
    """Ensure repeated operations produce stable results."""

    def test_ensure_then_ensure(self, project_dir):
        ensure_claudemd(project_dir)
        content1 = (project_dir / "CLAUDE.md").read_text()
        ensure_claudemd(project_dir)
        content2 = (project_dir / "CLAUDE.md").read_text()
        assert content1 == content2

    def test_update_then_update(self, project_dir):
        update_claudemd(project_dir)
        content1 = (project_dir / "CLAUDE.md").read_text()
        update_claudemd(project_dir)
        content2 = (project_dir / "CLAUDE.md").read_text()
        assert content1 == content2


class TestShimIdentity:
    """Verify ``pmlens.claudemd`` is a transparent re-export shim of ``pmlens.rules``.

    PMSERV-043 (commit fcce596) split claudemd.py into rules.py + a re-export
    shim. v0.4.x callers using ``from pmlens.claudemd import X`` MUST see
    the same object as ``pmlens.rules.X`` — verifiable via ``is`` identity.
    Regression guard added by PMSERV-044 plan v2 (cross-check R7) so that
    upcoming refactors (inject_pm_rules etc.) cannot accidentally rebind a
    shim symbol to a wrapper function and silently break v0.4.x imports.
    """

    def test_ensure_claudemd_is_identical(self):
        from pmlens import claudemd, rules

        assert claudemd.ensure_claudemd is rules.ensure_claudemd

    def test_update_claudemd_is_identical(self):
        from pmlens import claudemd, rules

        assert claudemd.update_claudemd is rules.update_claudemd

    def test_get_claudemd_status_is_identical(self):
        from pmlens import claudemd, rules

        assert claudemd.get_claudemd_status is rules.get_claudemd_status

    def test_template_version_is_identical(self):
        from pmlens import claudemd, rules

        assert claudemd.TEMPLATE_VERSION is rules.TEMPLATE_VERSION

    def test_marker_constants_are_identical(self):
        from pmlens import claudemd, rules

        assert claudemd.BEGIN_MARKER is rules.BEGIN_MARKER
        assert claudemd.END_MARKER is rules.END_MARKER
        assert claudemd.BEGIN_PATTERN is rules.BEGIN_PATTERN
        assert claudemd.OTHER_SECTION_PATTERN is rules.OTHER_SECTION_PATTERN
        assert claudemd.CLAUDEMD_TEMPLATE is rules.CLAUDEMD_TEMPLATE
