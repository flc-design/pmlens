"""Golden-fixture regression for the X content pipeline (PMSERV-121).

Pins the *artifact shape* of a representative build-in-public thread end to
end: a realistic hook + thread (the kind a session would draft) is run through
the redaction floor and the staged → redacted → review-queue path, and the
exact scrubbed output + count-only report are frozen.

Two guarantees ride on this golden:

* **catalog v2 scrubs the secret/path/email/ip surface** — a regression that
  weakened a pattern would change the pinned output and fail here.
* **internal IDs stay VISIBLE by default (PMSERV-121 item 1)** — the build-in-
  public voice keeps PMSERV-/ADR-/memory: refs, so the golden asserts they are
  present, not scrubbed.

Secret-shaped inputs are ASSEMBLED at runtime (memory:195) so no literal
credential form appears in this source file; the pinned EXPECTED output only
ever contains placeholder tokens, which are safe to write verbatim.
"""

from __future__ import annotations

import json
from pathlib import Path

import pmlens.server as srv
from pmlens.redaction import redact

# ─── Golden input (assembled at runtime — no literal secret forms) ───────────

_FAKE_AWS = "AKIA" + "A" * 16
_PATH = "/Users/flc001/.pm/x_drafts.db"
_EMAIL = "dev@example.com"

GOLDEN_HOOK = "Shipped PR2 hardening for our build-in-public pipeline (PMSERV-121)"
GOLDEN_SEGMENTS = [
    "The catalog now scrubs Azure/GCP/bearer tokens + IPs. See ADR-024 for the design.",
    f"Debugging tip: never paste a key like {_FAKE_AWS} or email me at {_EMAIL} in public.",
    f"Config lives at {_PATH} and the prod box was 192.0.2.42 once.",
    "Lesson saved as memory:190 - the full thread reconstructs from .pm.",
]

# ─── Golden output (frozen; placeholders only) ───────────────────────────────

EXPECTED_HOOK = "Shipped PR2 hardening for our build-in-public pipeline (PMSERV-121)"
EXPECTED_SEGMENTS = [
    "The catalog now scrubs Azure/GCP/bearer tokens + IPs. See ADR-024 for the design.",
    "Debugging tip: never paste a key like <REDACTED:secret> or email me at "
    "<REDACTED:email> in public.",
    "Config lives at <PATH> and the prod box was <IP> once.",
    "Lesson saved as memory:190 - the full thread reconstructs from .pm.",
]
EXPECTED_REPORT = {
    "catalog_version": 2,
    "total": 4,
    "high_severity_total": 1,
    "by_category": {"secret": 1, "email": 1, "path": 1, "ip": 1},
    "by_field": {"segment_1": 2, "segment_2": 2},
}

# Internal refs that MUST survive (build-in-public visibility, PMSERV-121 #1).
_KEPT_IDS = ("PMSERV-121", "ADR-024", "memory:190")
# Sensitive forms that must NEVER survive into the postable artifact.
_SCRUBBED = (_FAKE_AWS, _PATH, _EMAIL, "192.0.2.42")


def test_golden_redaction_artifact() -> None:
    """The pure redact() output is byte-for-byte the frozen golden."""
    res = redact(GOLDEN_HOOK, GOLDEN_SEGMENTS)
    assert res.redacted_hook == EXPECTED_HOOK
    assert res.redacted_segments == EXPECTED_SEGMENTS
    assert res.report == EXPECTED_REPORT
    assert res.flagged is True


def test_golden_keeps_internal_ids_visible() -> None:
    """Artifact-shape regression for item 1: internal refs are NOT scrubbed."""
    res = redact(GOLDEN_HOOK, GOLDEN_SEGMENTS)
    blob = json.dumps([res.redacted_hook, *res.redacted_segments])
    for kept in _KEPT_IDS:
        assert kept in blob
    for leak in _SCRUBBED:
        assert leak not in blob


def _make_project(tmp_path: Path) -> Path:
    proj = tmp_path / "goldenproj"
    (proj / ".pm" / "daily").mkdir(parents=True)
    (proj / ".pm" / "project.yaml").write_text(
        "name: goldenproj\ndisplay_name: goldenproj\nversion: 0.0.1\n"
        "status: development\nstarted: 2026-01-01\ndescription: golden\nphases: []\n",
        encoding="utf-8",
    )
    (proj / ".pm" / "tasks.yaml").write_text("[]\n", encoding="utf-8")
    return proj


def test_golden_pipeline_surfaces_redacted_artifact(tmp_path: Path) -> None:
    """End to end: pm_draft_x → pm_redact_draft → pm_x_drafts_pending yields the
    golden redacted artifact, and the raw concentrate / secret never leak."""
    proj = _make_project(tmp_path)
    draft_id = srv.pm_draft_x(
        signal_type="lesson",
        source_refs=["memory:190", "ADR-024"],
        raw_content="raw concentrate " + _FAKE_AWS,
        hook=GOLDEN_HOOK,
        body=GOLDEN_SEGMENTS,
        project_path=str(proj),
    )["draft_id"]
    red = srv.pm_redact_draft(draft_id=draft_id, project_path=str(proj))
    assert red["status"] == "redacted"
    assert red["report"] == EXPECTED_REPORT

    page = srv.pm_x_drafts_pending(filter_status="redacted", project_path=str(proj))
    item = page["items"][0]
    assert item["redacted_hook"] == EXPECTED_HOOK
    assert json.loads(item["redacted_body_json"]) == EXPECTED_SEGMENTS

    # The whole page payload is what a human copy-pastes — no raw/secret in it,
    # but the kept internal IDs ARE there (build-in-public provenance).
    payload = json.dumps(page)
    for leak in _SCRUBBED:
        assert leak not in payload
    assert "raw_content" not in item
    for kept in _KEPT_IDS[1:]:  # ADR-024 / memory:190 live in the body text
        assert kept in payload
