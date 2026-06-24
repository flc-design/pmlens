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

from pmlens.redaction import load_redaction_config, redact

# Secret-shaped fixtures are ASSEMBLED at runtime so the literal forms never
# appear in source text — otherwise GitHub secret scanning flags them on push
# (they are FAKE; no real credentials). The redaction regexes still match the
# assembled values. (A Google-key-shaped literal tripped the scanner on 631b9c7.)
_FAKE = {
    "aws": "AKIA" + "A" * 16,
    "ghp": "ghp_" + "a" * 36,
    "gh_pat": "github_pat_" + "b" * 30,
    "stripe": "sk_live_" + "c" * 20,
    "slack": "xoxb-" + "d" * 16,
    "jwt": "eyJ" + "a" * 16 + "." + "eyJ" + "b" * 16 + "." + "c" * 16,
    "conn": "postgres" + "://user:pass@db.example.com:5432/prod",
    "assigned": "api" + "_key=SUPERSECRETVALUE123",
    "gcp": "AIza" + "0" * 35,
    "openai": "sk-proj-" + "e" * 36,
    "npm": "npm_" + "f" * 36,
    "pypi": "pypi-" + "G" * 20,
    # PMSERV-121 catalog v2 additions (still runtime-assembled per memory:195).
    "azure": "AccountKey=" + "A" * 86 + "==",
    "gcp_sa_email": "svc-bot@my-project.iam.gserviceaccount.com",
    "gcp_key_id": '"private_key_id": "' + "a" * 40 + '"',
    "bearer": "Bearer " + "z" * 30,
}

# ─── secret scrubbing (high severity) ────────────────


@pytest.mark.parametrize("secret", list(_FAKE.values()))
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


def test_windows_abs_path_scrubbed() -> None:
    res = redact(r"see C:\Users\alice\secret.txt for details", [])
    assert r"C:\Users\alice" not in res.redacted_hook
    assert "<PATH>" in res.redacted_hook
    assert res.report["by_category"].get("path", 0) == 1


def test_email_scrubbed() -> None:
    res = redact("ping nakashin09@gmail.com", [])
    assert "nakashin09@gmail.com" not in res.redacted_hook
    assert "<REDACTED:email>" in res.redacted_hook


def test_internal_ids_kept_visible_by_default() -> None:
    """PMSERV-121: internal refs are non-secret and build-in-public posts want
    them visible, so they are NOT scrubbed unless a project opts in."""
    text = "done PMSERV-114 and ADR-024 see memory:190 in WF-031"
    res = redact(text, [])
    for token in ("PMSERV-114", "ADR-024", "memory:190", "WF-031"):
        assert token in res.redacted_hook
    assert res.report["by_category"].get("internal_id", 0) == 0
    assert res.flagged is False


def test_internal_ids_scrubbed_when_opted_in() -> None:
    res = redact(
        "done PMSERV-114 and ADR-024 see memory:190 in WF-031",
        [],
        scrub_internal_ids=True,
    )
    for token in ("PMSERV-114", "ADR-024", "memory:190", "WF-031"):
        assert token not in res.redacted_hook
    assert res.report["by_category"].get("internal_id", 0) == 4


# ─── per-segment scrubbing (must-fix #1 corollary) ───


def test_each_segment_scrubbed_individually() -> None:
    res = redact(
        "clean hook",
        ["first segment ok", "leaked nakashin09@gmail.com here", f"{_FAKE['aws']} tail"],
    )
    assert "nakashin09@gmail.com" not in res.redacted_segments[1]
    assert _FAKE["aws"] not in res.redacted_segments[2]
    assert res.redacted_segments[0] == "first segment ok"
    # by_field tallies the per-segment hits.
    assert res.report["by_field"].get("segment_1", 0) == 1
    assert res.report["by_field"].get("segment_2", 0) == 1


# ─── count-only report (must-fix #6) ─────────────────


def test_report_contains_no_cleartext_secret() -> None:
    secret = _FAKE["aws"]
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
    assert cfg == {"allow": [], "deny": [], "scrub_internal_ids": False}


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
    assert cfg == {"allow": [], "deny": [], "scrub_internal_ids": False}


def test_load_redaction_config_non_dict_returns_empty(tmp_path: Path) -> None:
    (tmp_path / "redaction.yaml").write_text("- just\n- a\n- list\n", encoding="utf-8")
    cfg = load_redaction_config(tmp_path)
    assert cfg == {"allow": [], "deny": [], "scrub_internal_ids": False}


# ─── catalog v2 additions (PMSERV-121) ──────────────


def test_azure_storage_key_scrubbed() -> None:
    res = redact(f"conn DefaultEndpointsProtocol=https;{_FAKE['azure']};EndpointSuffix=x", [])
    assert _FAKE["azure"] not in res.redacted_hook
    assert "<REDACTED:secret>" in res.redacted_hook
    assert res.report["high_severity_total"] >= 1


def test_gcp_service_account_email_scrubbed() -> None:
    res = redact(f"runs as {_FAKE['gcp_sa_email']} in prod", [])
    assert _FAKE["gcp_sa_email"] not in res.redacted_hook
    # Categorized as a secret (high), NOT downgraded to <REDACTED:email>.
    assert "<REDACTED:secret>" in res.redacted_hook
    assert res.report["by_category"].get("secret", 0) >= 1
    assert res.report["by_category"].get("email", 0) == 0


def test_gcp_private_key_id_scrubbed() -> None:
    res = redact(f"leaked {_FAKE['gcp_key_id']} from the json", [])
    assert _FAKE["gcp_key_id"] not in res.redacted_hook
    assert res.report["high_severity_total"] >= 1


def test_bearer_token_scrubbed() -> None:
    res = redact(f"Authorization: {_FAKE['bearer']}", [])
    assert _FAKE["bearer"] not in res.redacted_hook
    assert "<REDACTED:secret>" in res.redacted_hook


def test_ipv4_scrubbed() -> None:
    res = redact("server at 192.0.2.42 is up", [])
    assert "192.0.2.42" not in res.redacted_hook
    assert "<IP>" in res.redacted_hook
    assert res.report["by_category"].get("ip", 0) == 1


@pytest.mark.parametrize(
    "addr",
    ["2001:db8:85a3:0:0:8a2e:370:7334", "2001:db8::1", "fe80::1ff:fe23:4567:890a", "::1"],
)
def test_ipv6_scrubbed(addr: str) -> None:
    res = redact(f"bound to {addr} now", [])
    assert addr not in res.redacted_hook
    assert "<IP>" in res.redacted_hook
    assert res.report["by_category"].get("ip", 0) >= 1


@pytest.mark.parametrize("phone", ["+1 (415) 555-2671", "03-1234-5678", "090-1234-5678"])
def test_phone_scrubbed(phone: str) -> None:
    res = redact(f"call {phone} today", [])
    assert phone not in res.redacted_hook
    assert "<REDACTED:phone>" in res.redacted_hook


def test_three_part_version_not_matched_as_ip() -> None:
    """A 3-segment semver is safe; only a 4-segment dotted quad looks like an
    IP (documented false-positive trade-off for the ipv4 pattern)."""
    res = redact("upgraded pm-server to 0.9.0 today", [])
    assert "0.9.0" in res.redacted_hook
    assert res.report["by_category"].get("ip", 0) == 0


# ─── scrub_internal_ids config knob (PMSERV-121) ─────


def test_load_redaction_config_scrub_internal_ids_true(tmp_path: Path) -> None:
    (tmp_path / "redaction.yaml").write_text(
        "allow: []\ndeny: []\nscrub_internal_ids: true\n", encoding="utf-8"
    )
    cfg = load_redaction_config(tmp_path)
    assert cfg["scrub_internal_ids"] is True


def test_redaction_config_template_is_loadable_yaml() -> None:
    import yaml

    from pmlens.redaction import redaction_config_template

    parsed = yaml.safe_load(redaction_config_template())
    assert parsed == {"allow": [], "deny": [], "scrub_internal_ids": False}


# ─── end-to-end: redacted output is the post source ──


def test_redaction_target_is_post_source() -> None:
    """The redacted fields are what the human will post — assert no secret /
    path / email from the dirty input survives into them (redaction-target ==
    post-source). Internal IDs are intentionally NOT in this list: by default
    they are kept visible (PMSERV-121)."""
    dirty_hook = "leak /Users/flc001/x and nakashin09@gmail.com"
    dirty_segs = [f"token {_FAKE['ghp']}", "clean tail"]
    res = redact(dirty_hook, dirty_segs)
    posted = json.dumps([res.redacted_hook, *res.redacted_segments])
    for leak in (
        "/Users/flc001",
        "nakashin09@gmail.com",
        _FAKE["ghp"],
    ):
        assert leak not in posted
