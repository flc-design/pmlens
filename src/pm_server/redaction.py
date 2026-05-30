"""Layer-1 deterministic redaction prefilter for X-content drafts (PMSERV-115).

The *only* safety layer in the simplified pipeline (ADR-024): before a draft
enters the human review queue, this module scrubs structured secrets out of the
postable fields (the ``hook`` and **each** thread segment) and replaces them
with readable placeholder tokens. A second, semantic Layer-2 pass
(``/secret-scan`` + ``/privacy-check``) runs in-session and annotates; this
module is the deterministic floor that ships *first* (the cross-check forbids a
fail-open public-posting path).

Design constraints carried from the discovery cross-check (memory:192):

* **No runtime dependency on user-global files.** The pattern catalog is a
  versioned in-package copy of the regexes in
  ``~/.claude/skills/security-audit/patterns.md`` (lifted at author time — it
  needs independent maintenance, it is NOT linked at runtime). Per-project
  overrides live in an optional ``.pm/redaction.yaml`` (``safe_load``).
* **Count-only report (must-fix #6).** :class:`RedactionResult.report` records
  only counts/categories/per-field tallies — never the matched cleartext — so
  the report itself cannot become a second leak vector.
* **Per-segment scrub (must-fix #1 corollary).** Redaction runs on the hook and
  every body segment individually, so no per-segment leak can slip through an
  "assembled blob" gap.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path

import yaml

# Severity is advisory metadata for the count-only report. "high" = credential/
# secret-shaped (a real leak if posted); "medium" = identifying-but-not-secret
# (paths, emails, internal IDs). Both are scrubbed; severity only colors the
# report so a reviewer knows whether a scrub was cosmetic or critical.
_Severity = str  # "high" | "medium"


@dataclass(frozen=True)
class _Pattern:
    name: str
    category: str
    severity: _Severity
    regex: re.Pattern[str]
    placeholder: str


def _p(name: str, category: str, severity: _Severity, pattern: str, placeholder: str) -> _Pattern:
    return _Pattern(name, category, severity, re.compile(pattern), placeholder)


# ─── Versioned in-package catalog (author-time copy; maintain independently) ──
# Ordered most-specific-first so a high-severity secret is consumed before a
# looser pattern (e.g. a connection string) could partially match it. Each
# replacement uses a readable placeholder so the reviewer still understands the
# sentence structure.
CATALOG_VERSION = 1

_PATTERNS: tuple[_Pattern, ...] = (
    # --- High severity: credentials / secrets -------------------------------
    _p("aws_access_key", "secret", "high", r"AKIA[0-9A-Z]{16}", "<REDACTED:secret>"),
    _p(
        "github_token",
        "secret",
        "high",
        r"gh[pousr]_[A-Za-z0-9]{20,}|github_pat_[A-Za-z0-9_]{20,}",
        "<REDACTED:secret>",
    ),
    _p("stripe_key", "secret", "high", r"[sp]k_live_[A-Za-z0-9]{16,}", "<REDACTED:secret>"),
    _p("slack_token", "secret", "high", r"xox[baprs]-[A-Za-z0-9-]{10,}", "<REDACTED:secret>"),
    _p(
        "jwt",
        "secret",
        "high",
        r"eyJ[A-Za-z0-9_-]{10,}\.eyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}",
        "<REDACTED:secret>",
    ),
    _p(
        "private_key_header",
        "secret",
        "high",
        r"-----BEGIN [A-Z ]*PRIVATE KEY-----",
        "<REDACTED:secret>",
    ),
    _p(
        "connection_string",
        "secret",
        "high",
        r"(?:mongodb|postgres(?:ql)?|mysql|redis|amqp)(?:\+srv)?://[^\s]+",
        "<REDACTED:conn>",
    ),
    _p(
        "assigned_secret",
        "secret",
        "high",
        r"(?i)(?:api[_-]?key|api[_-]?secret|access[_-]?token|auth[_-]?token|secret|password|passwd|pwd)"
        r"\s*[:=]\s*['\"]?[^\s'\"]{8,}",
        "<REDACTED:secret>",
    ),
    # --- Medium severity: identifying but not secret ------------------------
    _p("abs_path", "path", "medium", r"(?:/Users/|/home/)[^\s'\"]*", "<PATH>"),
    _p(
        "email",
        "email",
        "medium",
        r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}",
        "<REDACTED:email>",
    ),
    _p("internal_id", "internal_id", "medium", r"\b(?:PMSERV|ADR|KR|WF)-\d+\b", "<ID>"),
    _p("memory_ref", "internal_id", "medium", r"\bmemory:\d+\b", "<ID>"),
)


@dataclass
class RedactionResult:
    """Outcome of redacting a draft's postable fields.

    ``report`` is count-only (no cleartext). ``flagged`` is True when any
    redaction occurred, signalling the reviewer to verify the scrub did not
    mangle meaning and that nothing slipped through.
    """

    redacted_hook: str
    redacted_segments: list[str]
    report: dict = field(default_factory=dict)
    flagged: bool = False


def _redact_text(text: str, allow: frozenset[str], deny: tuple[str, ...]) -> tuple[str, dict]:
    """Scrub a single text field. Returns (redacted_text, by_category counts).

    ``allow`` strings matched by a pattern are left intact (e.g. a public repo
    URL or package name the user whitelisted). ``deny`` literals are scrubbed
    after the regex pass (e.g. a private GitHub username the catalog can't know).
    """
    counts: dict[str, int] = {}

    def _bump(category: str) -> None:
        counts[category] = counts.get(category, 0) + 1

    for pat in _PATTERNS:

        def _sub(m: re.Match[str], _pat: _Pattern = pat) -> str:
            if m.group(0) in allow:
                return m.group(0)
            _bump(_pat.category)
            return _pat.placeholder

        text = pat.regex.sub(_sub, text)

    for literal in deny:
        if not literal:
            continue
        if literal in text:
            occurrences = text.count(literal)
            text = text.replace(literal, "<REDACTED:custom>")
            for _ in range(occurrences):
                _bump("custom")

    return text, counts


def _severity_of(category: str) -> _Severity:
    for pat in _PATTERNS:
        if pat.category == category:
            return pat.severity
    return "high"  # custom deny literals are treated as high (user marked them sensitive)


def redact(
    hook: str,
    body_segments: list[str],
    *,
    allow: list[str] | None = None,
    deny: list[str] | None = None,
) -> RedactionResult:
    """Redact a draft's hook and each body segment individually.

    Returns a :class:`RedactionResult` with the scrubbed fields and a
    count-only report of the shape::

        {
          "catalog_version": int,
          "total": int,
          "high_severity_total": int,
          "by_category": {category: count, ...},
          "by_field": {"hook": int, "segment_0": int, ...},
        }
    """
    allow_set = frozenset(allow or ())
    deny_tuple = tuple(deny or ())

    by_category: dict[str, int] = {}
    by_field: dict[str, int] = {}

    def _merge(field_name: str, field_counts: dict[str, int]) -> None:
        by_field[field_name] = sum(field_counts.values())
        for cat, n in field_counts.items():
            by_category[cat] = by_category.get(cat, 0) + n

    red_hook, hook_counts = _redact_text(hook or "", allow_set, deny_tuple)
    _merge("hook", hook_counts)

    red_segments: list[str] = []
    for i, seg in enumerate(body_segments):
        red_seg, seg_counts = _redact_text(seg or "", allow_set, deny_tuple)
        red_segments.append(red_seg)
        _merge(f"segment_{i}", seg_counts)

    total = sum(by_category.values())
    high_total = sum(n for cat, n in by_category.items() if _severity_of(cat) == "high")
    report = {
        "catalog_version": CATALOG_VERSION,
        "total": total,
        "high_severity_total": high_total,
        "by_category": by_category,
        "by_field": {k: v for k, v in by_field.items() if v > 0},
    }
    return RedactionResult(
        redacted_hook=red_hook,
        redacted_segments=red_segments,
        report=report,
        flagged=total > 0,
    )


def load_redaction_config(pm_path: Path) -> dict:
    """Load optional per-project ``.pm/redaction.yaml`` (safe_load).

    Returns ``{"allow": [...], "deny": [...]}`` with empty lists when the file
    is absent or malformed. Never raises — a broken config must not block the
    pipeline, and (fail-safe) a missing config simply means "in-package catalog
    only". Both keys are coerced to lists of strings.
    """
    config_path = Path(pm_path) / "redaction.yaml"
    if not config_path.exists():
        return {"allow": [], "deny": []}
    try:
        raw = yaml.safe_load(config_path.read_text(encoding="utf-8")) or {}
    except (yaml.YAMLError, OSError):
        return {"allow": [], "deny": []}
    if not isinstance(raw, dict):
        return {"allow": [], "deny": []}

    def _as_str_list(value: object) -> list[str]:
        if not isinstance(value, list):
            return []
        return [str(v) for v in value if isinstance(v, (str, int, float))]

    return {"allow": _as_str_list(raw.get("allow")), "deny": _as_str_list(raw.get("deny"))}
