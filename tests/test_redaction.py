"""Tests for the Layer-1 deterministic redaction prefilter (PMSERV-115).

Load-bearing guarantees:
* secrets/identifiers are scrubbed out of the postable fields (hook + each
  body segment, individually);
* the report is count-only — a known secret must appear NOWHERE in it
  (must-fix #6);
* allow/deny per-project overrides work and never raise.
* a private key goes as a whole block (catalog v3) however long it is, while
  a BEGIN line named in prose goes alone (as in v2), and every pattern takes
  time linear in the text, matching what the unguarded v2 patterns matched.
"""

from __future__ import annotations

import json
import random
import re
import time
from pathlib import Path

import pytest
import yaml

from pmlens.redaction import (
    _PATTERNS,
    _substitute,
    load_redaction_config,
    redact,
    redact_secrets,
)

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


# ─── catalog v3: a private key goes as a whole block ─

# Filler for a key body, assembled at runtime like _FAKE (memory:195).
_KEY_BODY_LINE = "Zm9v" * 16


def _private_key(
    label: str = "RSA PRIVATE KEY",
    *,
    lines: int = 4,
    end: bool = True,
    sep: str = "\n",
    headers: str = "",
) -> str:
    """A key block; ``sep`` separates its lines, ``headers`` go before the body."""
    head = "-----BEGIN " + label + "-----"
    body = sep.join([_KEY_BODY_LINE] * lines)
    tail = sep + "-----END " + label + "-----" if end else ""
    return f"{head}{sep}{headers}{body}{tail}"


@pytest.mark.parametrize(
    "label",
    [
        "RSA PRIVATE KEY",
        "EC PRIVATE KEY",
        "OPENSSH PRIVATE KEY",
        "ENCRYPTED PRIVATE KEY",
        "PRIVATE KEY",
        "PGP PRIVATE KEY BLOCK",
    ],
)
def test_a_private_key_is_removed_as_a_whole_block(label: str) -> None:
    # v2 removed only the BEGIN line and left the body and the END line.
    text = f"before\n{_private_key(label)}\nafter"
    assert redact_secrets(text) == ("before\n<REDACTED:secret>\nafter", {"secret": 1})
    res = redact(text, [text])
    assert res.redacted_hook == "before\n<REDACTED:secret>\nafter"
    assert res.redacted_segments == [res.redacted_hook]
    assert res.report["by_category"] == {"secret": 2}


def test_a_key_without_its_end_line_is_removed_to_the_end() -> None:
    text = "pasted:\n" + _private_key(end=False)
    assert redact_secrets(text) == ("pasted:\n<REDACTED:secret>", {"secret": 1})


def test_a_key_longer_than_16_kib_goes_with_its_end_line() -> None:
    # A PGP key with a photo ID or several subkeys runs past 16 KiB. The
    # first v3 draft looked for the END line only 16 KiB ahead, then removed
    # 16 KiB and left the rest of the body and the END line, counted as found.
    key = _private_key("PGP PRIVATE KEY BLOCK", lines=330)
    assert len(key) > 20_000
    assert redact_secrets(f"x {key} y") == ("x <REDACTED:secret> y", {"secret": 1})


def test_a_long_key_without_its_end_line_goes_with_all_its_body() -> None:
    text = "x\n" + _private_key(lines=330, end=False) + "\n\n(the rest) y"
    assert redact_secrets(text) == ("x\n<REDACTED:secret>\n\n(the rest) y", {"secret": 1})


# The BEGIN / END lines, assembled at runtime like the key bodies (memory:195).
_BEGIN_LINE = "-----BEGIN " + "RSA PRIVATE KEY-----"
_END_LINE = "-----END " + "RSA PRIVATE KEY-----"
_PROSE = (
    f"Our hook blocks commits containing {_BEGIN_LINE} lines. "
    "It also checks AWS keys. Next we will add Slack tokens."
)


def test_a_begin_line_named_in_prose_goes_alone() -> None:
    # Without key material after it, only the line goes, as in catalog v2;
    # the first v3 draft took the 16 KiB after it, here the rest of the text.
    expected = _PROSE.replace(_BEGIN_LINE, "<REDACTED:secret>")
    assert redact_secrets(_PROSE) == (expected, {"secret": 1})
    assert redact(_PROSE, [_PROSE]).redacted_segments == [expected]
    both = f"between {_BEGIN_LINE} and {_END_LINE} lines"
    assert redact_secrets(both)[0] == f"between <REDACTED:secret> and {_END_LINE} lines"


def test_an_allowed_begin_line_in_prose_is_kept() -> None:
    allowed = redact(_PROSE, [_PROSE], allow=[_BEGIN_LINE])
    assert allowed.redacted_hook == _PROSE
    assert allowed.redacted_segments == [_PROSE]
    assert allowed.report["total"] == 0
    # The allow entry names the line, not a key: a key under that line goes.
    assert redact(_private_key(), [], allow=[_BEGIN_LINE]).redacted_hook == "<REDACTED:secret>"


_PGP_HEADERS = "Version: GnuPG v2\nComment: https://example.org/k\n\n"
_PEM_HEADERS = "Proc-Type: 4,ENCRYPTED\nDEK-Info: AES-128-CBC,0123456789ABCDEF\n\n"


@pytest.mark.parametrize(
    ("case", "kwargs"),
    [
        ("PGP armor headers", {"label": "PGP PRIVATE KEY BLOCK", "headers": _PGP_HEADERS}),
        (
            "PGP armor headers, no END",
            {"label": "PGP PRIVATE KEY BLOCK", "headers": _PGP_HEADERS, "end": False},
        ),
        ("encrypted PEM, no END", {"headers": _PEM_HEADERS, "end": False}),
        ("folded to one line", {"sep": " "}),
        ("folded to one line, no END", {"sep": " ", "end": False}),
        ("CRLF", {"sep": "\r\n"}),
        ("indented", {"sep": "\n    "}),
        ("JSON escapes, no END", {"label": "PRIVATE KEY", "sep": "\\n", "end": False}),
    ],
)
def test_every_layout_of_a_key_goes_whole(case: str, kwargs: dict) -> None:
    text = "before: " + _private_key(**kwargs) + ' "after"'
    assert redact_secrets(text) == ('before: <REDACTED:secret> "after"', {"secret": 1}), case


def test_a_key_in_a_folded_yaml_scalar_goes_whole() -> None:
    # A YAML plain scalar folds the key's line breaks into spaces on load.
    lines = "\n  ".join([_KEY_BODY_LINE] * 4)
    doc = yaml.safe_load(f"context: pasted {_BEGIN_LINE}\n  {lines}\n  {_END_LINE} by mistake\n")
    assert "\n" not in doc["context"]
    assert redact_secrets(doc["context"]) == (
        "pasted <REDACTED:secret> by mistake",
        {"secret": 1},
    )


def test_two_keys_are_two_blocks_and_the_text_between_stays() -> None:
    text = f"{_private_key()}\nbetween\n{_private_key('EC PRIVATE KEY')}"
    assert redact_secrets(text) == (
        "<REDACTED:secret>\nbetween\n<REDACTED:secret>",
        {"secret": 2},
    )


def test_a_service_account_json_key_goes_with_its_escaped_body() -> None:
    # In the JSON file the PEM line breaks are "\n" escapes.
    key = _private_key("PRIVATE KEY").replace("\n", "\\n")
    text = '{"private_key": "' + key + '\\n", "client_email": "' + _FAKE["gcp_sa_email"] + '"}'
    assert redact_secrets(text) == (
        '{"private_key": "<REDACTED:secret>\\n", "client_email": "<REDACTED:secret>"}',
        {"secret": 2},
    )


def test_certificates_and_public_keys_are_left_alone() -> None:
    for label in ("CERTIFICATE", "PUBLIC KEY"):
        text = f"-----BEGIN {label}-----\n{_KEY_BODY_LINE}\n-----END {label}-----"
        assert redact_secrets(text) == (text, {})


# ─── every pattern takes time linear in the text ─────

# Units that make a backtracking pattern retry, each repeated to a run without
# whitespace (a few carry whitespace for the patterns that span it).
_RUN_UNITS = (
    "a",
    "A",
    "1",
    "a1",
    "-",
    ".",
    "@",
    "/",
    "=",
    "_",
    ":",
    "+",
    "a-",
    "a.",
    "1.",
    "a@",
    "eyJ",
    "eyJa.",
    "aB3_-",
    "あ",
    "f:",
    "a@b.cc.",
    "pwd ",
    "+1 ",
    "-----BEGIN PRIVATE KEY-----",
    # BEGIN lines packed close, with key material, armor headers or neither
    # after them: each attempt must stop at the next BEGIN line.
    "-----BEGIN PRIVATE KEY-----\n" + "Zm9v" * 10 + "\n",
    "-----BEGIN PRIVATE KEY-----\nComment: x",
    "-----BEGIN PRIVATE KEY-----" + "a" * 39 + " ",
    "-----END PRIVATE KEY-----",
    "\\n",
)


def _runs(size: int) -> list[tuple[str, str]]:
    runs = [(repr(unit), (unit * (size // len(unit) + 1))[:size]) for unit in _RUN_UNITS]
    rng = random.Random(size)
    mixed = "".join(rng.choice("aZ09._-@/=:+%eyJ") for _ in range(size))
    return [*runs, ("mixed", mixed)]


def test_one_key_line_over_a_long_text_takes_linear_time() -> None:
    begin, end = "-----BEGIN PRIVATE KEY-----", "-----END PRIVATE KEY-----"
    [pat] = [p for p in _PATTERNS if p.name == "private_key"]
    size = 256 * 1024
    for text in (
        begin + ("a " * size)[:size] + end,  # no key material before the END line
        begin + (("a" * 39 + " ") * size)[:size] + end,  # runs one short of it
        begin + "\n" + ("Zm9v" * size)[:size] + ".",  # a body with no END line
        begin + "-" * size,
    ):
        started = time.perf_counter()
        _substitute(pat, text)
        assert time.perf_counter() - started < 1.0


def test_every_pattern_takes_linear_time_on_long_runs() -> None:
    # Some patterns once took time quadratic in a run (65,536 letters took
    # gcp_sa_email about 4 s, 262,144 about a minute). 64 KiB is timed first,
    # so a pattern that turns quadratic again fails within seconds.
    for size, budget in ((64 * 1024, 0.25), (256 * 1024, 1.0)):
        for label, text in _runs(size):
            for pat in _PATTERNS:
                started = time.perf_counter()
                _substitute(pat, text)
                elapsed = time.perf_counter() - started
                assert elapsed < budget, f"{pat.name}: {elapsed:.2f} s on {size} x {label}"


# The guarded patterns (_GUARD_NOTE in redaction.py) against the unguarded
# originals of catalog v2: the same matches, the same output.
_ORIGINALS = {
    "jwt": r"eyJ[A-Za-z0-9_-]{10,}\.eyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}",
    "gcp_sa_email": r"[a-z0-9][a-z0-9\-]*@[a-z0-9\-]+\.iam\.gserviceaccount\.com",
    "email": r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}",
}
_ATOMS = {
    "jwt": ("eyJ", "a", "B", "1", "_", "-", ".", " ", "!", "eyJabcdefghij", ".eyJ"),
    "gcp_sa_email": ("a", "1", "-", "@", ".", "X", " ", "_", "com", ".iam.gserviceaccount.com"),
    "email": ("a", "Z", "1", ".", "_", "%", "+", "-", "@", " ", "!", "ab", "com", ".io", "b.cc"),
}


@pytest.mark.parametrize("name", sorted(_ORIGINALS))
def test_a_guarded_pattern_matches_what_its_original_did(name: str) -> None:
    original = re.compile(_ORIGINALS[name])
    [pat] = [p for p in _PATTERNS if p.name == name]
    rng = random.Random(name)
    for _ in range(4_000):
        text = "".join(rng.choice(_ATOMS[name]) for _ in range(rng.randint(1, 40)))
        assert _substitute(pat, text) == original.subn(pat.placeholder, text), text


def test_an_allowed_match_is_compared_without_its_lead() -> None:
    # The jwt pattern starts at the run's first character and keeps "abc" as
    # its lead; the allow-list still sees the token alone.
    text = f"token abc{_FAKE['jwt']} end"
    assert redact(text, [], allow=[_FAKE["jwt"]]).redacted_hook == text
    assert redact(text, []).redacted_hook == "token abc<REDACTED:secret> end"
