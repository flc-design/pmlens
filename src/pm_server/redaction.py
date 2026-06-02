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


# A pragmatic IPv6 matcher (full + ``::``-compressed forms). IPv6 grammar is
# notoriously hard to capture exactly; for redaction we prefer a slightly broad
# matcher (over-scrubbing an address in a public post is harmless) bounded by
# lookarounds so it cannot eat a neighbouring word/hextet. IPv4-mapped and
# zone-index (``%eth0``) variants are intentionally out of scope.
_IPV6 = (
    r"(?<![\w:.])(?:"
    r"(?:[A-Fa-f0-9]{1,4}:){7}[A-Fa-f0-9]{1,4}|"  # 1:2:3:4:5:6:7:8
    r"(?:[A-Fa-f0-9]{1,4}:){1,7}:|"  # 1::            1:2:3:4:5:6:7::
    r"(?:[A-Fa-f0-9]{1,4}:){1,6}:[A-Fa-f0-9]{1,4}|"  # 1::8          1:2:3:4:5:6::8
    r"(?:[A-Fa-f0-9]{1,4}:){1,5}(?::[A-Fa-f0-9]{1,4}){1,2}|"  # 1::7:8       …
    r"(?:[A-Fa-f0-9]{1,4}:){1,4}(?::[A-Fa-f0-9]{1,4}){1,3}|"
    r"(?:[A-Fa-f0-9]{1,4}:){1,3}(?::[A-Fa-f0-9]{1,4}){1,4}|"
    r"(?:[A-Fa-f0-9]{1,4}:){1,2}(?::[A-Fa-f0-9]{1,4}){1,5}|"
    r"[A-Fa-f0-9]{1,4}:(?::[A-Fa-f0-9]{1,4}){1,6}|"  # 1::3:4:5:6:7:8
    r":(?:(?::[A-Fa-f0-9]{1,4}){1,7}|:)"  # ::2:3:4:5:6:7:8   ::
    r")(?![\w:.])"
)


# ─── Versioned in-package catalog (author-time copy; maintain independently) ──
# Ordered most-specific-first so a high-severity secret is consumed before a
# looser pattern (e.g. a connection string) could partially match it. Each
# replacement uses a readable placeholder so the reviewer still understands the
# sentence structure.
#
# v2 (PMSERV-121): added Azure storage AccountKey, GCP service-account markers,
# bearer tokens (high severity), and IPv4 / IPv6 / phone numbers (medium). The
# IP/phone matchers are inherently broad — a 4-segment version string or a long
# separated digit run can match — so they ship at medium severity and a project
# can whitelist a specific false positive via the ``allow`` list.
CATALOG_VERSION = 2

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
    _p("google_api_key", "secret", "high", r"AIza[0-9A-Za-z_\-]{35}", "<REDACTED:secret>"),
    _p("openai_key", "secret", "high", r"sk-(?:proj-)?[A-Za-z0-9_\-]{20,}", "<REDACTED:secret>"),
    _p("npm_token", "secret", "high", r"npm_[A-Za-z0-9]{36}", "<REDACTED:secret>"),
    _p("pypi_token", "secret", "high", r"pypi-[A-Za-z0-9_\-]{16,}", "<REDACTED:secret>"),
    # Azure storage account key embedded in a connection string
    # (DefaultEndpointsProtocol=...;AccountKey=<base64>;...). The generic
    # connection_string pattern only knows mongodb/postgres/... schemes, so the
    # Azure form needs its own rule.
    _p(
        "azure_storage_key",
        "secret",
        "high",
        r"AccountKey=[A-Za-z0-9+/]{40,}={0,2}",
        "<REDACTED:secret>",
    ),
    # GCP service-account JSON markers. The private key itself is caught by
    # private_key_header; these catch the two other high-signal fields — the
    # 40-hex private_key_id and the *.iam.gserviceaccount.com client email
    # (placed in the high block so it is scrubbed as a secret BEFORE the generic
    # medium `email` pattern could downgrade it).
    _p(
        "gcp_sa_key_id",
        "secret",
        "high",
        r"\"private_key_id\"\s*:\s*\"[a-f0-9]{40}\"",
        "<REDACTED:secret>",
    ),
    _p(
        "gcp_sa_email",
        "secret",
        "high",
        r"[a-z0-9][a-z0-9\-]*@[a-z0-9\-]+\.iam\.gserviceaccount\.com",
        "<REDACTED:secret>",
    ),
    # Bearer token in an Authorization header / log line. assigned_secret only
    # matches `key=value`, so `Bearer <token>` needs its own rule.
    _p("bearer_token", "secret", "high", r"(?i)bearer\s+[A-Za-z0-9._\-]{20,}", "<REDACTED:secret>"),
    _p(
        "assigned_secret",
        "secret",
        "high",
        r"(?i)(?:api[_-]?key|api[_-]?secret|access[_-]?token|auth[_-]?token|secret|password|passwd|pwd)"
        r"\s*[:=]\s*['\"]?[^\s'\"]{8,}",
        "<REDACTED:secret>",
    ),
    # --- Medium severity: identifying but not secret ------------------------
    _p(
        "abs_path",
        "path",
        "medium",
        r"(?:/Users/|/home/|[A-Za-z]:[\\/]Users[\\/])[^\s'\"]*",
        "<PATH>",
    ),
    _p(
        "email",
        "email",
        "medium",
        r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}",
        "<REDACTED:email>",
    ),
    _p("internal_id", "internal_id", "medium", r"\b(?:PMSERV|ADR|KR|WF)-\d+\b", "<ID>"),
    _p("memory_ref", "internal_id", "medium", r"\bmemory:\d+\b", "<ID>"),
    # IPv4 dotted-quad, bounded so it cannot match a slice of a longer dotted
    # run (e.g. 1.2.3.4.5). NOTE: a 4-segment version string like "1.2.3.4" is
    # indistinguishable from an IP and WILL be scrubbed — whitelist it via the
    # project allow-list if that is a problem.
    _p(
        "ipv4",
        "ip",
        "medium",
        r"(?<![\d.])(?:(?:25[0-5]|2[0-4]\d|1?\d?\d)\.){3}(?:25[0-5]|2[0-4]\d|1?\d?\d)(?![\d.])",
        "<IP>",
    ),
    _p("ipv6", "ip", "medium", _IPV6, "<IP>"),
    # Phone numbers, kept deliberately narrow to limit false positives: either
    # an international "+<country>…" form, or a separated trunk form like
    # 03-1234-5678 / 090-1234-5678. A bare unseparated digit run is NOT matched.
    _p(
        "phone",
        "phone",
        "medium",
        r"\+\d[\d().\s\-]{6,}\d|\b0\d{1,4}[-\s]\d{2,4}[-\s]\d{3,4}\b",
        "<REDACTED:phone>",
    ),
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


def _redact_text(
    text: str,
    allow: frozenset[str],
    deny: tuple[str, ...],
    scrub_internal_ids: bool,
) -> tuple[str, dict]:
    """Scrub a single text field. Returns (redacted_text, by_category counts).

    ``allow`` strings matched by a pattern are left intact (e.g. a public repo
    URL or package name the user whitelisted). ``deny`` literals are scrubbed
    after the regex pass (e.g. a private GitHub username the catalog can't know).

    ``scrub_internal_ids`` is opt-in (default off at the caller): internal refs
    like ``PMSERV-121`` / ``ADR-024`` / ``memory:190`` are NOT secrets and a
    build-in-public post usually wants them visible, so the ``internal_id``
    category is skipped unless a project explicitly opts in (PMSERV-121).
    """
    counts: dict[str, int] = {}

    def _bump(category: str) -> None:
        counts[category] = counts.get(category, 0) + 1

    for pat in _PATTERNS:
        if pat.category == "internal_id" and not scrub_internal_ids:
            continue

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
    scrub_internal_ids: bool = False,
) -> RedactionResult:
    """Redact a draft's hook and each body segment individually.

    ``scrub_internal_ids`` defaults to False: internal refs (PMSERV-/ADR-/KR-/
    WF-/memory:) are non-secret and build-in-public posts usually want them
    visible, so they are kept unless a project opts in via
    ``.pm/redaction.yaml`` ``scrub_internal_ids: true`` (PMSERV-121).

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

    red_hook, hook_counts = _redact_text(hook or "", allow_set, deny_tuple, scrub_internal_ids)
    _merge("hook", hook_counts)

    red_segments: list[str] = []
    for i, seg in enumerate(body_segments):
        red_seg, seg_counts = _redact_text(seg or "", allow_set, deny_tuple, scrub_internal_ids)
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

    Tip: add identifiers the in-package catalog cannot infer — e.g. your GitHub
    username / handle (which can hide inside URLs like github.com/<user>) — to
    ``deny`` so they are scrubbed from drafts.
    """
    config_path = Path(pm_path) / "redaction.yaml"
    if not config_path.exists():
        return {"allow": [], "deny": [], "scrub_internal_ids": False}
    try:
        raw = yaml.safe_load(config_path.read_text(encoding="utf-8")) or {}
    except (yaml.YAMLError, OSError):
        return {"allow": [], "deny": [], "scrub_internal_ids": False}
    if not isinstance(raw, dict):
        return {"allow": [], "deny": [], "scrub_internal_ids": False}

    def _as_str_list(value: object) -> list[str]:
        if not isinstance(value, list):
            return []
        return [str(v) for v in value if isinstance(v, (str, int, float))]

    return {
        "allow": _as_str_list(raw.get("allow")),
        "deny": _as_str_list(raw.get("deny")),
        "scrub_internal_ids": bool(raw.get("scrub_internal_ids", False)),
    }


_REDACTION_CONFIG_TEMPLATE = """\
# PM Server — per-project redaction overrides (.pm/redaction.yaml)
# Layered on top of the in-package catalog (redaction.py). safe_load only.
#
# allow: literal strings the catalog WOULD scrub but you want kept verbatim
#        (e.g. a public repo URL, your package name, a public contact address).
# deny:  literal strings the catalog CANNOT infer but must be scrubbed — most
#        importantly identifiers that hide inside URLs, like your GitHub handle
#        (github.com/<handle>) which no regex can know is yours.
# scrub_internal_ids: when true, also scrub PMSERV-/ADR-/KR-/WF-/memory: refs.
#        Default false — build-in-public posts usually WANT these visible.

allow: []
deny: []
scrub_internal_ids: false
"""


def redaction_config_template() -> str:
    """Return a commented ``.pm/redaction.yaml`` starter template.

    Used by the deny-list UX (PMSERV-121): a project can scaffold this file to
    add identifiers the in-package catalog cannot infer — most importantly a
    GitHub username/handle that can hide inside URLs — to ``deny``.
    """
    return _REDACTION_CONFIG_TEMPLATE
