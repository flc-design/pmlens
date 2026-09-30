"""Rule-section version safety (ADR-055, PMSERV-197 / PMSERV-198).

Several pmlens builds can share a machine — a pipx release, a dev checkout,
a pinned plugin — and all of them write the same CLAUDE.md / AGENTS.md. These
tests pin the guarantees that keep that safe:

* an older build never silently downgrades a newer section (``force`` aside);
* ``target="existing"`` only rewrites files that already carry the section;
* ``pm_status`` reports outdated, newer-than-server and mismatched sections;
* ``update-rules --all`` plans by default and writes only with ``--apply``.
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from pmlens import rules
from pmlens.rules import (
    BEGIN_MARKER,
    END_MARKER,
    TEMPLATE_VERSION,
    duplicate_rule_file_warning,
    inject_pm_rules,
    rules_version_warnings,
    update_claudemd,
)

NEWER = TEMPLATE_VERSION + 1
OLDER = TEMPLATE_VERSION - 1


def _section(version: int, body: str = "rules") -> str:
    return f"# Project\n\n{BEGIN_MARKER.format(version=version)}\n{body}\n{END_MARKER}\n"


def _codes(warnings: list[dict]) -> list[str]:
    return [w["code"] for w in warnings]


# ─── Downgrade refusal ────────────────────────────────────────────────────────


class TestNoSilentDowngrade:
    def test_newer_section_is_left_alone(self, tmp_path: Path):
        original = _section(NEWER, "written by a newer pmlens")
        (tmp_path / "CLAUDE.md").write_text(original, encoding="utf-8")

        summary = inject_pm_rules(tmp_path, target="claude-code")

        (result,) = summary.results
        assert result.status == "skipped"
        assert result.refused_downgrade is True
        assert f"v{NEWER}" in result.message
        assert (tmp_path / "CLAUDE.md").read_text(encoding="utf-8") == original
        # A refused rewrite is not a rewrite: no backup either.
        assert list(tmp_path.glob("CLAUDE.md.bak.*")) == []

    def test_force_rewrites_a_newer_section(self, tmp_path: Path):
        (tmp_path / "CLAUDE.md").write_text(_section(NEWER), encoding="utf-8")

        summary = inject_pm_rules(tmp_path, target="claude-code", force=True)

        (result,) = summary.results
        assert result.status == "updated"
        assert result.refused_downgrade is False
        content = (tmp_path / "CLAUDE.md").read_text(encoding="utf-8")
        assert BEGIN_MARKER.format(version=TEMPLATE_VERSION) in content

    def test_older_section_is_upgraded(self, tmp_path: Path):
        (tmp_path / "CLAUDE.md").write_text(_section(OLDER), encoding="utf-8")

        (result,) = inject_pm_rules(tmp_path, target="claude-code").results

        assert result.status == "updated"
        assert result.refused_downgrade is False

    def test_dry_run_reports_the_refusal(self, tmp_path: Path):
        (tmp_path / "AGENTS.md").write_text(_section(NEWER), encoding="utf-8")

        (result,) = inject_pm_rules(tmp_path, target="codex", dry_run=True).results

        assert result.refused_downgrade is True
        assert result.is_dry_run is True

    def test_update_claudemd_refuses_and_can_be_forced(self, tmp_path: Path):
        original = _section(NEWER)
        (tmp_path / "CLAUDE.md").write_text(original, encoding="utf-8")

        message = update_claudemd(tmp_path)
        assert "skipped" in message
        assert (tmp_path / "CLAUDE.md").read_text(encoding="utf-8") == original

        update_claudemd(tmp_path, force=True)
        content = (tmp_path / "CLAUDE.md").read_text(encoding="utf-8")
        assert BEGIN_MARKER.format(version=TEMPLATE_VERSION) in content


# ─── target="existing" ────────────────────────────────────────────────────────


class TestExistingTarget:
    def test_only_files_with_a_section_are_rewritten(self, tmp_path: Path):
        (tmp_path / "CLAUDE.md").write_text(_section(OLDER), encoding="utf-8")
        agents = "# Agents\n\nThe user's own AGENTS.md with no PM section.\n"
        (tmp_path / "AGENTS.md").write_text(agents, encoding="utf-8")

        summary = inject_pm_rules(tmp_path, target="existing")

        assert [r.target_file for r in summary.results] == ["CLAUDE.md"]
        assert summary.detection_source == "existing"
        assert (tmp_path / "AGENTS.md").read_text(encoding="utf-8") == agents

    def test_never_creates_a_rule_file(self, tmp_path: Path):
        summary = inject_pm_rules(tmp_path, target="existing")

        assert summary.results == []
        assert summary.overall_status == "skipped"
        assert not (tmp_path / "CLAUDE.md").exists()
        assert not (tmp_path / "AGENTS.md").exists()

    def test_shared_agents_md_is_written_once(self, tmp_path: Path):
        (tmp_path / "AGENTS.md").write_text(_section(OLDER), encoding="utf-8")

        summary = inject_pm_rules(tmp_path, target="existing")

        (result,) = summary.results
        assert result.target_file == "AGENTS.md"
        assert set(result.hosts) == {"codex", "cursor", "grok"}

    def test_unknown_target_still_rejected(self, tmp_path: Path):
        with pytest.raises(ValueError, match="unknown target"):
            inject_pm_rules(tmp_path, target="everything")


# ─── Version warnings ─────────────────────────────────────────────────────────


class TestRulesVersionWarnings:
    def test_current_sections_raise_nothing(self, tmp_path: Path):
        (tmp_path / "CLAUDE.md").write_text(_section(TEMPLATE_VERSION), encoding="utf-8")
        (tmp_path / "AGENTS.md").write_text(_section(TEMPLATE_VERSION), encoding="utf-8")

        assert rules_version_warnings(tmp_path) == []

    def test_no_sections_raise_nothing(self, tmp_path: Path):
        (tmp_path / "CLAUDE.md").write_text("# plain notes\n", encoding="utf-8")

        assert rules_version_warnings(tmp_path) == []

    def test_outdated_section(self, tmp_path: Path):
        (tmp_path / "CLAUDE.md").write_text(_section(OLDER), encoding="utf-8")

        (warning,) = rules_version_warnings(tmp_path)

        assert warning["code"] == "rules_outdated"
        assert f"CLAUDE.md (v{OLDER})" in warning["message"]
        assert "target='existing'" in warning["remediation"]
        assert "review" in warning["remediation"]

    def test_outdated_names_the_commit_rule_once_it_is_gone(self, tmp_path: Path, monkeypatch):
        monkeypatch.setattr(rules, "TEMPLATE_VERSION", 15)
        (tmp_path / "CLAUDE.md").write_text(_section(13), encoding="utf-8")

        (warning,) = rules_version_warnings(tmp_path)

        assert "commit without being asked" in warning["message"]

    def test_outdated_without_the_commit_rule_omits_the_reason(self, tmp_path: Path, monkeypatch):
        monkeypatch.setattr(rules, "TEMPLATE_VERSION", 17)
        (tmp_path / "CLAUDE.md").write_text(_section(16), encoding="utf-8")

        (warning,) = rules_version_warnings(tmp_path)

        assert "commit without being asked" not in warning["message"]

    def test_newer_section(self, tmp_path: Path):
        (tmp_path / "AGENTS.md").write_text(_section(NEWER), encoding="utf-8")

        (warning,) = rules_version_warnings(tmp_path)

        assert warning["code"] == "rules_newer_than_server"
        assert f"AGENTS.md (v{NEWER})" in warning["message"]
        assert "Upgrade" in warning["remediation"]

    def test_mismatch_between_files(self, tmp_path: Path):
        (tmp_path / "CLAUDE.md").write_text(_section(TEMPLATE_VERSION), encoding="utf-8")
        (tmp_path / "AGENTS.md").write_text(_section(OLDER), encoding="utf-8")

        warnings = rules_version_warnings(tmp_path)

        assert _codes(warnings) == ["rules_outdated", "rule_file_version_mismatch"]
        mismatch = warnings[1]
        assert f"CLAUDE.md v{TEMPLATE_VERSION} and AGENTS.md v{OLDER}" in mismatch["message"]
        assert "Grok Build" in mismatch["message"]


class TestDuplicateWarningOnlyForSameVersion:
    @pytest.fixture
    def grok_installed(self, isolated_home: Path) -> None:
        (isolated_home / ".grok").mkdir(parents=True)
        (isolated_home / ".grok" / "config.toml").write_text("", encoding="utf-8")

    def test_same_version_is_a_harmless_duplicate(self, tmp_path: Path, grok_installed):
        (tmp_path / "CLAUDE.md").write_text(_section(TEMPLATE_VERSION), encoding="utf-8")
        (tmp_path / "AGENTS.md").write_text(_section(TEMPLATE_VERSION), encoding="utf-8")

        warning = duplicate_rule_file_warning(tmp_path)

        assert warning is not None
        assert warning["remediation"].startswith("Harmless")

    def test_different_versions_are_not_called_harmless(self, tmp_path: Path, grok_installed):
        (tmp_path / "CLAUDE.md").write_text(_section(TEMPLATE_VERSION), encoding="utf-8")
        (tmp_path / "AGENTS.md").write_text(_section(OLDER), encoding="utf-8")

        assert duplicate_rule_file_warning(tmp_path) is None
        assert "rule_file_version_mismatch" in _codes(rules_version_warnings(tmp_path))


# ─── MCP surface ──────────────────────────────────────────────────────────────


class TestServerSurface:
    def test_pm_status_reports_outdated_rules(self, tmp_path: Path, isolated_home):
        from pmlens.server import pm_init, pm_status

        pm_init(project_path=str(tmp_path), project_name="versions")
        (tmp_path / "CLAUDE.md").write_text(_section(OLDER), encoding="utf-8")

        status = pm_status(project_path=str(tmp_path))

        assert "rules_outdated" in _codes(status["warnings"])
        assert status["diagnostics"]["server_template_version"] == TEMPLATE_VERSION

    def test_pm_update_rules_surfaces_a_refused_downgrade(self, tmp_path: Path, isolated_home):
        from pmlens.server import pm_init, pm_update_rules

        pm_init(project_path=str(tmp_path), project_name="versions")
        (tmp_path / "CLAUDE.md").write_text(_section(NEWER), encoding="utf-8")

        result = pm_update_rules(project_path=str(tmp_path), target="existing")

        assert result["results"][0]["refused_downgrade"] is True
        assert "rules_newer_than_server" in _codes(result["warnings"])

        forced = pm_update_rules(project_path=str(tmp_path), target="existing", force=True)
        assert forced["results"][0]["status"] == "updated"


# ─── CLI: update-rules --all ──────────────────────────────────────────────────


class TestCliUpdateRulesAll:
    @pytest.fixture
    def registry(self, tmp_path: Path, monkeypatch) -> list[Path]:
        roots = [tmp_path / "a", tmp_path / "b"]
        for root in roots:
            root.mkdir()
        entries = [SimpleNamespace(name=r.name, path=str(r)) for r in roots]
        monkeypatch.setattr(
            "pmlens.storage.load_registry", lambda: SimpleNamespace(projects=entries)
        )
        return roots

    def _run(self, *args: str):
        from click.testing import CliRunner

        from pmlens.__main__ import cli

        return CliRunner().invoke(cli, ["update-rules", "--all", *args])

    def test_plans_only_and_targets_existing_by_default(self, registry):
        a, b = registry
        (a / "CLAUDE.md").write_text(_section(OLDER), encoding="utf-8")

        result = self._run()

        assert result.exit_code == 0, result.output
        assert "Plan only" in result.output
        assert "[dry-run] CLAUDE.md" in result.output
        assert "nothing to update" in result.output  # project b has no section
        # Nothing written, nothing created.
        assert BEGIN_MARKER.format(version=OLDER) in (a / "CLAUDE.md").read_text(encoding="utf-8")
        assert not (a / "AGENTS.md").exists()
        assert not (b / "CLAUDE.md").exists()

    def test_apply_writes_the_plan(self, registry):
        a, b = registry
        (a / "CLAUDE.md").write_text(_section(OLDER), encoding="utf-8")

        result = self._run("--apply")

        assert result.exit_code == 0, result.output
        assert "Plan only" not in result.output
        content = (a / "CLAUDE.md").read_text(encoding="utf-8")
        assert BEGIN_MARKER.format(version=TEMPLATE_VERSION) in content
        assert not (b / "CLAUDE.md").exists()

    def test_apply_still_refuses_a_downgrade(self, registry):
        a, _ = registry
        original = _section(NEWER)
        (a / "CLAUDE.md").write_text(original, encoding="utf-8")

        result = self._run("--apply")

        assert result.exit_code == 0, result.output
        assert (a / "CLAUDE.md").read_text(encoding="utf-8") == original
        assert f"v{NEWER}" in result.output

    def test_explicit_target_overrides_the_default(self, registry):
        a, _ = registry

        result = self._run("--target", "claude-code", "--apply")

        assert result.exit_code == 0, result.output
        assert (a / "CLAUDE.md").exists()


# ─── Marker robustness (adversarial review, PMSERV-195) ───────────────────────


class TestMarkerRobustness:
    def test_several_sections_are_reported_and_not_rewritten(self, tmp_path: Path):
        original = _section(OLDER) + "\nuser notes\n\n" + _section(NEWER)
        (tmp_path / "CLAUDE.md").write_text(original, encoding="utf-8")

        (result,) = inject_pm_rules(tmp_path, target="existing").results

        assert result.status == "failed"
        assert f"v{OLDER}, v{NEWER}" in result.message
        assert (tmp_path / "CLAUDE.md").read_text(encoding="utf-8") == original
        assert "multiple_pm_sections" in _codes(rules_version_warnings(tmp_path))

    def test_the_newest_of_several_sections_decides_the_version(self, tmp_path: Path):
        (tmp_path / "CLAUDE.md").write_text(
            _section(OLDER) + "\n" + _section(NEWER), encoding="utf-8"
        )
        assert rules.pm_section_versions(tmp_path) == {"CLAUDE.md": NEWER}

    def test_end_marker_line_before_the_section_is_not_paired(self, tmp_path: Path):
        text = (
            "# Notes\n\n"
            f"{rules.END_MARKER}\n\n"
            "keep this paragraph\n\n"
            f"{BEGIN_MARKER.format(version=OLDER)}\nold rules\n{END_MARKER}\n"
        )
        (tmp_path / "CLAUDE.md").write_text(text, encoding="utf-8")

        inject_pm_rules(tmp_path, target="existing")
        once = (tmp_path / "CLAUDE.md").read_text(encoding="utf-8")
        second = inject_pm_rules(tmp_path, target="existing").results[0]

        assert once.count("keep this paragraph") == 1
        assert once.count("pm-server:begin") == 1
        assert "old rules" not in once
        assert second.status == "skipped"  # idempotent

    def test_a_marker_quoted_in_prose_is_not_a_section(self, tmp_path: Path):
        prose = "The section starts at `<!-- pm-server:begin v=99 -->` in this file.\n"
        (tmp_path / "CLAUDE.md").write_text(prose, encoding="utf-8")

        assert rules.pm_section_versions(tmp_path) == {}
        assert rules_version_warnings(tmp_path) == []

    def test_non_utf8_rule_file_does_not_abort_existing(self, tmp_path: Path):
        (tmp_path / "AGENTS.md").write_bytes(b"\xff\xfe not utf-8 \x80")
        (tmp_path / "CLAUDE.md").write_text(_section(OLDER), encoding="utf-8")

        summary = inject_pm_rules(tmp_path, target="existing")

        assert [r.target_file for r in summary.results] == ["CLAUDE.md"]
        # Reading the unreadable file neither raises nor invents a section;
        # CLAUDE.md was just brought up to date, so nothing is left to report.
        assert rules_version_warnings(tmp_path) == []


# ─── Legacy writers honour ADR-055 ────────────────────────────────────────────


class TestLegacyWriters:
    def test_refusal_names_tools_that_exist(self, tmp_path: Path):
        (tmp_path / "CLAUDE.md").write_text(_section(NEWER), encoding="utf-8")
        (result,) = inject_pm_rules(tmp_path, target="claude-code").results
        assert "pm_update_rules(force=True)" in result.message
        assert "--force" in result.message

    def test_pm_init_backs_up_before_replacing(self, tmp_path: Path, isolated_home):
        from pmlens.rules import ensure_claudemd

        (tmp_path / "CLAUDE.md").write_text(_section(OLDER), encoding="utf-8")
        ensure_claudemd(tmp_path)
        assert len(list(tmp_path.glob("CLAUDE.md.bak.*"))) == 1

    def test_update_claudemd_backs_up_before_replacing(self, tmp_path: Path):
        (tmp_path / "CLAUDE.md").write_text(_section(OLDER), encoding="utf-8")
        update_claudemd(tmp_path)
        assert len(list(tmp_path.glob("CLAUDE.md.bak.*"))) == 1

    def test_pm_update_claudemd_reports_a_refused_downgrade(self, tmp_path: Path, isolated_home):
        from pmlens.server import pm_init, pm_update_claudemd

        pm_init(project_path=str(tmp_path), project_name="legacy")
        (tmp_path / "CLAUDE.md").write_text(_section(NEWER), encoding="utf-8")

        result = pm_update_claudemd(project_path=str(tmp_path))

        assert "rules_newer_than_server" in _codes(result["warnings"])
        assert result["after"]["version"] == NEWER


class TestCliGuards:
    def _invoke(self, *args: str):
        from click.testing import CliRunner

        from pmlens.__main__ import cli

        return CliRunner().invoke(cli, list(args))

    def test_update_claudemd_all_is_retired(self):
        result = self._invoke("update-claudemd", "--all")
        assert result.exit_code == 1
        assert "update-rules --all" in result.output

    def test_apply_without_all_is_an_error(self):
        result = self._invoke("update-rules", "--apply")
        assert result.exit_code != 0
        assert "--apply only applies to --all" in result.output

    def test_dry_run_and_apply_contradict(self):
        result = self._invoke("update-rules", "--all", "--dry-run", "--apply")
        assert result.exit_code != 0
        assert "contradict" in result.output


# ─── Session summary overwrite (adversarial review, PMSERV-195) ───────────────


class TestSessionSummaryOverwrite:
    def test_dropping_pending_items_is_reported(self, tmp_path: Path, isolated_home):
        from pmlens.server import pm_init, pm_session_summary

        pm_init(project_path=str(tmp_path), project_name="summary")
        first = pm_session_summary(
            action="save", summary="first", pending="write docs, ship", project_path=str(tmp_path)
        )
        assert first["warnings"] == []

        second = pm_session_summary(
            action="save", summary="second", pending="ship", project_path=str(tmp_path)
        )

        (warning,) = second["warnings"]
        assert warning["code"] == "session_summary_pending_dropped"
        assert "write docs" in warning["message"]
