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
    # The pattern without its guard, tried once right after each match (see
    # _GUARD_NOTE below); None for an unguarded pattern.
    resume: re.Pattern[str] | None = None


def _p(
    name: str,
    category: str,
    severity: _Severity,
    pattern: str,
    placeholder: str,
    *,
    guard: str = "",
) -> _Pattern:
    """Build a catalog entry.

    Args:
        name: The pattern's name (never reported).
        category: The report category.
        severity: ``"high"`` or ``"medium"``.
        pattern: The regular expression. A named group ``lead`` marks text the
            pattern consumes only to reach its secret; it is kept verbatim.
        placeholder: What a match is replaced with.
        guard: A lookbehind that lets ``pattern`` start only at the first
            character of a run (see _GUARD_NOTE below).

    Returns:
        The compiled entry.
    """
    resume = re.compile(pattern) if guard else None
    return _Pattern(name, category, severity, re.compile(guard + pattern), placeholder, resume)


# _GUARD_NOTE. A pattern that opens with a character class repeated without
# bound (an email's local part, a JWT's first segment) is otherwise retried
# from every character of a long run of that class, each try rescanning the
# rest of the run: time quadratic in the run (65,536 such characters took
# seconds). Each run is therefore tried once, from its first character: a
# one-character lookbehind (``guard``) refuses every other start, and a
# ``lead`` group takes over the characters the original pattern skipped to
# reach its first possible start (kept verbatim, so the output is unchanged).
# Within one run every start meets the same continuation, so the first start
# succeeds exactly when some start did. A match can end inside a run (an
# email's top-level domain followed by "."), where the guard would refuse the
# start the original pattern made next, so the unguarded form is tried once
# right after each match (``resume``). The guarded patterns match what the
# unguarded originals matched; tests/test_redaction.py compares them on random
# text and times every pattern on long runs.
_GUARD_JWT = r"(?<![A-Za-z0-9_\-])"
_GUARD_GCP_SA_EMAIL = r"(?<![a-z0-9\-])"
_GUARD_EMAIL = r"(?<![A-Za-z0-9._%+\-])"

# A private key block (PEM, OpenSSH, PGP armor) goes whole: its BEGIN line,
# any armor headers, the base64 body and its END line, however long. The body
# is recognised by key material, a run of 40 base64 characters (a body line
# holds 64, or 70 for OpenSSH; prose and armor headers hold no such run).
# Without key material only the BEGIN line goes, as in catalog v2, so prose
# that names the line ("blocks -----BEGIN RSA PRIVATE KEY----- lines") keeps
# its words and an ``allow`` entry for the line still applies.
_KEY_BEGIN = r"-----BEGIN [A-Z ]*PRIVATE KEY(?: BLOCK)?-----"
_KEY_END = r"-----END [A-Z ]*PRIVATE KEY(?: BLOCK)?-----"
# A character short of a run of five dashes, that is of the next BEGIN or END
# line. An attempt from one BEGIN line never scans past the next such line, so
# the attempts of a text scan disjoint stretches of it: time linear in it.
_KEY_UNTIL_LINE = r"(?:[^-]|-(?!----))"
_KEY_MATERIAL = r"[A-Za-z0-9+/]{40}"
# What separates the lines of a key as text holds it: whitespace (a YAML plain
# scalar folds line breaks into spaces), or the two-character "\n" / "\r"
# escapes of a JSON string (a service-account key file) or a log line.
_KEY_BREAK = r"(?:\s|\\[rn])"
# An armor header on a line of its own (legacy encrypted PEM, PGP). Its value
# stops at the line's end and, like _KEY_UNTIL_LINE, short of the next BEGIN or
# END line, so the headers of one attempt never run into the next block.
_KEY_HEADER = (
    r"[ \t]*+(?:\r?\n|\\r?\\n)[ \t]*+"
    r"(?:Proc-Type|DEK-Info|Version|Comment|Hash|Charset|MessageID):"
    r"(?:[^\r\n\\-]++|-(?!----))*+"
)
_PRIVATE_KEY = (
    _KEY_BEGIN
    + "(?:"
    # Through the END line, when key material comes before the next BEGIN or
    # END line and that line is an END line. The atomic group commits to the
    # first key material, so a failed attempt is not retried from later runs.
    + rf"(?>{_KEY_UNTIL_LINE}*?{_KEY_MATERIAL}){_KEY_UNTIL_LINE}*+{_KEY_END}"
    # Otherwise (a key cut off mid-paste) the armor headers and the base64
    # body after the BEGIN line, when the body opens with key material. The
    # body ends at the first character that is neither base64 nor a break, so
    # words after a cut-off key can go with it; nothing of the key stays.
    + rf"|(?:{_KEY_HEADER})*+{_KEY_BREAK}*+{_KEY_MATERIAL}"
    + rf"(?:{_KEY_BREAK}*+[A-Za-z0-9+/=]++)*+"
    + ")?"
)


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
#
# v3: a private key with its body is removed as a whole block (BEGIN line,
# armor headers, body and END line, of any length; v2 removed only the BEGIN
# line and left the key body in place), PGP private key blocks are included,
# and the private key pattern runs first, before any other pattern can change
# a key's body. A BEGIN line without key material after it is removed alone,
# as in v2. The jwt / gcp_sa_email / email patterns are guarded so they take
# time linear in the text (what they match is unchanged).
CATALOG_VERSION = 3

_PATTERNS: tuple[_Pattern, ...] = (
    # --- High severity: credentials / secrets -------------------------------
    # First: a key's base64 body could hold another pattern's shape, and a
    # placeholder put inside it would end the body early.
    _p("private_key", "secret", "high", _PRIVATE_KEY, "<REDACTED:secret>"),
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
    # The atomic group commits to the run's first "eyJ" (see _GUARD_NOTE).
    _p(
        "jwt",
        "secret",
        "high",
        r"(?>(?P<lead>[A-Za-z0-9_\-]*?)eyJ)[A-Za-z0-9_\-]{10,}"
        r"\.eyJ[A-Za-z0-9_\-]{10,}\.[A-Za-z0-9_\-]{10,}",
        "<REDACTED:secret>",
        guard=_GUARD_JWT,
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
    # GCP service-account JSON markers. The JSON's "private_key" value keeps
    # its PEM line breaks as "\n" escapes, which the private_key pattern
    # above reads as line breaks, so it removes the whole key. These two catch
    # the other high-signal fields — the 40-hex private_key_id and the
    # *.iam.gserviceaccount.com client email (placed in the high block so it is
    # scrubbed as a secret BEFORE the generic medium `email` pattern could
    # downgrade it).
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
        r"(?P<lead>-*)[a-z0-9][a-z0-9\-]*@[a-z0-9\-]+\.iam\.gserviceaccount\.com",
        "<REDACTED:secret>",
        guard=_GUARD_GCP_SA_EMAIL,
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
        r"[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}",
        "<REDACTED:email>",
        guard=_GUARD_EMAIL,
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


def _substitute(pat: _Pattern, text: str, allow: frozenset[str] = frozenset()) -> tuple[str, int]:
    """Replace every match of ``pat`` in ``text`` with its placeholder.

    The leftmost-first scan of ``re.sub``, plus the ``resume`` try right after
    each match for a guarded pattern (_GUARD_NOTE). A ``lead`` group is kept
    verbatim and is not part of what ``allow`` is compared with.

    Args:
        pat: The catalog entry.
        text: The text to scrub.
        allow: Matches to leave as they are.

    Returns:
        The scrubbed text and the number of replacements.
    """
    replaced = 0
    has_lead = "lead" in pat.regex.groupindex

    def replace(match: re.Match[str]) -> str:
        nonlocal replaced
        lead = (match.group("lead") or "") if has_lead else ""
        if match.group(0)[len(lead) :] in allow:
            return match.group(0)
        replaced += 1
        return lead + pat.placeholder

    if pat.resume is None:
        return pat.regex.sub(replace, text), replaced
    pieces: list[str] = []
    pos = 0
    match = pat.regex.search(text)
    # No catalog pattern matches the empty string, so each match moves pos on.
    while match is not None:
        pieces.append(text[pos : match.start()])
        pieces.append(replace(match))
        pos = match.end()
        match = pat.resume.match(text, pos) or pat.regex.search(text, pos)
    pieces.append(text[pos:])
    return "".join(pieces), replaced


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
        text, replaced = _substitute(pat, text, allow)
        if replaced:
            counts[pat.category] = counts.get(pat.category, 0) + replaced

    for literal in deny:
        if not literal:
            continue
        if literal in text:
            occurrences = text.count(literal)
            text = text.replace(literal, "<REDACTED:custom>")
            for _ in range(occurrences):
                _bump("custom")

    return text, counts


def redact_secrets(text: str) -> tuple[str, dict[str, int]]:
    """Scrub ONLY the high-severity credential patterns. Returns (text, counts).

    A deliberately narrower pass than :func:`redact` (PMSERV-168), for content
    crossing a boundary that is not publication. Ingest copies auto-memory
    notes into ``~/.pm/memory.db``, which every project can search — so a
    credential written in one repo's notes becomes readable while working in
    another. That is the risk worth scrubbing.

    The other categories are deliberately left alone. ``redact`` scrubs them
    because a public post must not carry an absolute path, an internal ticket
    ID or an email; an index on the user's own machine must, or it stops being
    useful — auto-memory notes are largely made of paths and ticket refs, and
    PMSERV-170 exists precisely because that identity carries meaning. Scrub
    them here and cross-project search would return rows nobody can act on.

    The ``secret`` patterns are tightly anchored (``AKIA[0-9A-Z]{16}``,
    ``xox[baprs]-…``), so the false-positive cost of scrubbing them by default
    is low — unlike ``email`` or ``ip``, which match ordinary prose.

    Counts are by category and carry no cleartext, matching
    :class:`RedactionResult`'s report discipline: a report that quotes the
    secret to prove it found one has published it again.

    Every pattern takes time linear in the text, so a text of any length can
    be scanned whole (_GUARD_NOTE).
    """
    counts: dict[str, int] = {}
    for pat in _PATTERNS:
        if pat.category != "secret":
            continue
        text, replaced = _substitute(pat, text)
        if replaced:
            counts[pat.category] = counts.get(pat.category, 0) + replaced
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
# PM Lens — per-project redaction overrides (.pm/redaction.yaml)
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
