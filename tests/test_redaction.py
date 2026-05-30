"""Tests for the Layer-1 deterministic redaction prefilter (PMSERV-115).

Load-bearing guarantees:
* secrets/identifiers are scrubbed out of the postable fields (hook + each
  body segment, individually);
* the report is count-only — a known secret must appear NOWHERE in it
  (must-fix #6);
* allow/deny per-project overrides work and never raise.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from pm_server.redaction import load_redaction_config, redact

# ─── secret scrubbing (high severity) ────────────────


@pytest.mark.parametrize(
    "secret",
    [
        "AKIA1234567890ABCDEF",
        "ghp_abcdefghijklmnopqrstuvwxyz0123456789",
        "github_pat_abcdefghij_klmnopqrstuvwxyz0123456789",
        "sk_live_abcdefghijklmnop1234",
        "xoxb-1234567890-abcdefghijkl",
        "eyJhbGciOiJIUzI1NiIsInR5cCI.eyJzdWIiOiIxMjM0NTY.SflKxwRJSMeKKF2QT4f",
        "postgres://user:pass@db.example.com:5432/prod",
        "api_key=SUPERSECRETVALUE123",
    ],
)
def test_secret_is_scrubbed_from_hook(secret: str) -> None:
    res = redact(f"shipping it: {secret} today", [])
    assert secret not in res.redacted_hook
    assert res.flagged is True
    assert res.report["high_severity_total"] >= 1


def test_private_key_header_scrubbed() -> None:
    res = redact("-----BEGIN RSA PRIVATE KEY-----", [])
    assert "PRIVATE KEY" not in res.redacted_hook
    assert res.report["by_category"].get("secret", 0) >= 1


# ─── medium severity: paths / emails / internal IDs ──


def test_abs_path_scrubbed() -> None:
    res = redact("see /Users/flc001/secret/notes.md for details", [])
    assert "/Users/flc001" not in res.redacted_hook
    assert "<PATH>" in res.redacted_hook
    assert res.report["by_category"].get("path", 0) == 1


def test_email_scrubbed() -> None:
    res = redact("ping nakashin09@gmail.com", [])
    assert "nakashin09@gmail.com" not in res.redacted_hook
    assert "<REDACTED:email>" in res.redacted_hook


def test_internal_ids_scrubbed() -> None:
    res = redact("done PMSERV-114 and ADR-024 see memory:190 in WF-031", [])
    for token in ("PMSERV-114", "ADR-024", "memory:190", "WF-031"):
        assert token not in res.redacted_hook
    assert res.report["by_category"].get("internal_id", 0) == 4


# ─── per-segment scrubbing (must-fix #1 corollary) ───


def test_each_segment_scrubbed_individually() -> None:
    res = redact(
        "clean hook",
        ["first segment ok", "leaked nakashin09@gmail.com here", "AKIA1234567890ABCDEF tail"],
    )
    assert "nakashin09@gmail.com" not in res.redacted_segments[1]
    assert "AKIA1234567890ABCDEF" not in res.redacted_segments[2]
    assert res.redacted_segments[0] == "first segment ok"
    # by_field tallies the per-segment hits.
    assert res.report["by_field"].get("segment_1", 0) == 1
    assert res.report["by_field"].get("segment_2", 0) == 1


# ─── count-only report (must-fix #6) ─────────────────


def test_report_contains_no_cleartext_secret() -> None:
    secret = "AKIA1234567890ABCDEF"
    email = "nakashin09@gmail.com"
    res = redact(f"{secret}", [f"contact {email}", "/Users/flc001/x"])
    blob = json.dumps(res.report)
    assert secret not in blob
    assert email not in blob
    assert "/Users/flc001" not in blob
    # But the structural counts ARE present.
    assert res.report["total"] == 3
    assert set(res.report["by_category"]) == {"secret", "email", "path"}
    assert res.report["catalog_version"] >= 1


def test_clean_draft_not_flagged() -> None:
    res = redact("just a normal build-in-public update, nothing sensitive", ["second clean line"])
    assert res.flagged is False
    assert res.report["total"] == 0
    assert res.report["by_field"] == {}


# ─── allow / deny overrides ──────────────────────────


def test_allow_list_protects_whitelisted_match() -> None:
    res = redact(
        "reach me at public@example.com not at nakashin09@gmail.com",
        [],
        allow=["public@example.com"],
    )
    assert "public@example.com" in res.redacted_hook  # preserved
    assert "nakashin09@gmail.com" not in res.redacted_hook  # scrubbed
    assert res.report["by_category"].get("email", 0) == 1


def test_deny_list_scrubs_custom_literal() -> None:
    res = redact("my handle is secrethandle on the platform", [], deny=["secrethandle"])
    assert "secrethandle" not in res.redacted_hook
    assert "<REDACTED:custom>" in res.redacted_hook
    assert res.report["by_category"].get("custom", 0) == 1
    # custom literals count as high severity (user marked them sensitive).
    assert res.report["high_severity_total"] >= 1


# ─── config loading ──────────────────────────────────


def test_load_redaction_config_missing_returns_empty(tmp_path: Path) -> None:
    cfg = load_redaction_config(tmp_path)
    assert cfg == {"allow": [], "deny": []}


def test_load_redaction_config_reads_lists(tmp_path: Path) -> None:
    (tmp_path / "redaction.yaml").write_text(
        "allow:\n  - pm-server\n  - flc-design\ndeny:\n  - secrethandle\n",
        encoding="utf-8",
    )
    cfg = load_redaction_config(tmp_path)
    assert cfg["allow"] == ["pm-server", "flc-design"]
    assert cfg["deny"] == ["secrethandle"]


def test_load_redaction_config_malformed_returns_empty(tmp_path: Path) -> None:
    (tmp_path / "redaction.yaml").write_text("::: not valid yaml :::\n  - [", encoding="utf-8")
    cfg = load_redaction_config(tmp_path)
    assert cfg == {"allow": [], "deny": []}


def test_load_redaction_config_non_dict_returns_empty(tmp_path: Path) -> None:
    (tmp_path / "redaction.yaml").write_text("- just\n- a\n- list\n", encoding="utf-8")
    cfg = load_redaction_config(tmp_path)
    assert cfg == {"allow": [], "deny": []}


# ─── end-to-end: redacted output is the post source ──


def test_redaction_target_is_post_source() -> None:
    """The redacted fields are what the human will post — assert nothing from
    the dirty input survives into them (redaction-target == post-source)."""
    dirty_hook = "leak /Users/flc001/x and nakashin09@gmail.com"
    dirty_segs = ["token ghp_abcdefghijklmnopqrstuvwxyz0123456789", "ref memory:190"]
    res = redact(dirty_hook, dirty_segs)
    posted = json.dumps([res.redacted_hook, *res.redacted_segments])
    for leak in (
        "/Users/flc001",
        "nakashin09@gmail.com",
        "ghp_abcdefghijklmnopqrstuvwxyz0123456789",
        "memory:190",
    ):
        assert leak not in posted
