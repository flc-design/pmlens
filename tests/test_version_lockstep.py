"""Release version-lockstep guard — every live pin must track pyproject (PMSERV-172).

``pyproject.toml [project].version`` is the single source of truth (SSoT). A
release bumps it, and every OTHER file that repeats that number has to move in
the same commit. Two incidents motivated this file:

* **v0.10.0** — the plugin pins lagged main by many commits (PMSERV-133 added
  the first five guards).
* **v0.12.1** — the ``pm-server`` wrapper was rebuilt at its committed 0.12.0
  and PyPI rejected the upload with ``400 File already exists``, leaving the
  plugin's fresh ``uvx pm-server@0.12.1`` pin unresolvable.

The 0.13.0 pre-release audit found the guard set was itself incomplete: eight
live values were unguarded, and one of them sat *inside a file the guard set
already covered* — ``plugin/README.md``'s bare ``latest 0.13.0`` line was
invisible to a regex anchored on ``pm-server@…``. A file being "guarded" is not
the same as its every pin being guarded, which is the most deceptive shape this
class of bug takes.

So this module has TWO layers:

1. **Forward** — an explicit registry (:data:`JSON_PINS`, :data:`TOML_PINS`,
   :data:`TEXT_PINS`, plus ``uv.lock`` and the wrapper floor). Each entry says
   where a pin lives and how to extract it; all must equal the SSoT. Text
   surfaces additionally assert an exact match *count*, so ADDING a pin to an
   already-registered file fails until it is registered.
2. **Reverse** — :func:`test_no_unregistered_version_pins_in_release_surfaces`
   re-reads every file in :data:`SCOPE_FILES` and flags any ``x.y.z``-shaped
   string that is neither the SSoT nor an explicit :data:`IGNORED` entry with a
   written reason. This is what catches a *stale* pin nobody registered.

**Why the reverse layer is scoped, not repo-wide.** The repo holds ~90 version
strings that must never move — ``CHANGELOG.md`` headings, ``docs/MIGRATION.md``,
the incident post-mortems in ``.github/workflows/*.yml`` (``release.yml`` even
contains a literal ``0.13.0`` in a comment that equals the SSoT *by
coincidence*), and this file's own prose. A repo-wide scan is red on day one and
would be deleted within a week. :data:`SCOPE_FILES` is the release surface: a
small, closed, rarely-edited set. **Accepted limits**, both established by
mutating the tree and re-running this file rather than by reasoning about it:

* A pin added to a brand-new file outside that set is not detected — add the
  file to :data:`SCOPE_FILES` when you create it.
* A pin added to a *scoped* file **at the current version** and matching no
  registered pattern (say a bare ``pipx install pmlens==0.13.0`` in the
  cheatsheet) is not flagged the moment it is written: it equals the SSoT, so
  there is nothing yet to disagree with. It surfaces at the very next bump,
  where the reverse layer names its exact file, line and text. Detection is
  deferred by one release, not lost — the pin cannot ship stale.

Deliberately frozen, and therefore out of scope: ``docs/user-guide.html`` and
``docs/workflow-guide.html`` sit at 0.12.0 on purpose (see ``docs/README.md``);
``packaging/pmlens-reservation/`` is a name reservation pinned at 0.0.1;
``requirements.lock`` is uv export output and does not pin pmlens itself.
"""

from __future__ import annotations

import json
import re
import tomllib
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
PYPROJECT = REPO_ROOT / "pyproject.toml"

# A version-shaped string anywhere in a line. Both boundaries are load-bearing,
# and both were wrong in the first draft:
#
# * The suffix group means `0.13.0rc1` extracts as `0.13.0rc1`, not as `0.13.0`.
#   A right-boundary-only pattern (`(?![\w.])`) matched NOTHING in `0.13.0rc1`
#   or `0.13.0.post1`, so a stale pin written in PEP 440 form was invisible at
#   every release, not merely deferred by one.
# * `(?<![\d.])` stops the engine restarting mid-number: without it `0.11.25.3`
#   yielded `11.25.3`, a value that appears nowhere in the file, reported as an
#   offender. It rejects a preceding DIGIT OR DOT only — a preceding letter must
#   still match, or `v0.13.0` (the form used throughout the HTML docs) would be
#   skipped.
#
# `{2,}` rather than `{2}` so a four-component version is captured whole.
_SEMVER = re.compile(r"(?<![\d.])\d+(?:\.\d+){2,}(?:[.\w+-]*[\w+])?")


def pyproject_version() -> str:
    """The single source of truth: ``pyproject.toml [project].version``."""
    with PYPROJECT.open("rb") as f:
        return tomllib.load(f)["project"]["version"]


def _read(rel_path: str) -> str:
    # Always read as UTF-8 text. Shell `grep` was observed to classify a valid
    # UTF-8 HTML file in docs/ as binary and skip it silently, which is exactly
    # the kind of invisible gap this module exists to close.
    return (REPO_ROOT / rel_path).read_text(encoding="utf-8")


def _dig(doc: object, pointer: str) -> object:
    """Walk a ``/``-separated pointer, indexing lists by integer segments."""
    node = doc
    for segment in pointer.split("/"):
        node = node[int(segment)] if isinstance(node, list) else node[segment]  # type: ignore[index]
    return node


def _dotted(doc: dict, key: str) -> object:
    node: object = doc
    for segment in key.split("."):
        node = node[segment]  # type: ignore[index]
    return node


# ─── Forward layer: the registry ──────────────────────────────────────────────
#
# (path, pointer/key/pattern, note). The note is printed on failure and is the
# only place that explains WHY the pin exists — keep it truthful.

JSON_PINS: tuple[tuple[str, str, str], ...] = (
    (
        "manifest.json",
        "version",
        "the .mcpb bundle version Claude Desktop/Cowork installs; "
        "also re-checked by scripts/build_mcpb.py",
    ),
    (
        ".claude-plugin/marketplace.json",
        "metadata/version",
        "marketplace listing version (plugins[0] has no version key)",
    ),
    (
        "plugin/.claude-plugin/plugin.json",
        "version",
        "plugin manifest version shown by /plugin",
    ),
)

TOML_PINS: tuple[tuple[str, str, str], ...] = (
    (
        "packaging/pm-server-wrapper/pyproject.toml",
        "project.version",
        "the pm-server compat wrapper — a stale value is the v0.12.1 incident "
        "(PyPI 400 File already exists, plugin pin left unresolvable)",
    ),
)

# (path, pattern, expected number of matches, note). EVERY match must equal the
# SSoT and the count must be exact, so a newly added pin in one of these files
# fails here until it is registered.
TEXT_PINS: tuple[tuple[str, str, int, str], ...] = (
    (
        "src/pmlens/__init__.py",
        r'__version__\s*=\s*["\']([^"\']+)["\']',
        1,
        "what `pmlens --version` prints to users",
    ),
    (
        "plugin/.mcp.json",
        r"pm-server@(\d+\.\d+\.\d+)(?![\w.])",
        1,
        "the load-bearing committed uvx pin — unresolvable if the wrapper is "
        "not published at this exact version",
    ),
    (
        "plugin/README.md",
        r"pm-server[@>=]+(\d+\.\d+\.\d+)(?![\w.])",
        3,
        "the documented release pin (:46 prerequisite, :103 committed pin, "
        ":104 floor form). The generic `pm-server@x.y` placeholder has no "
        "third component and is correctly skipped",
    ),
    (
        "docs/cheatsheet.md",
        r"^>\s+Version\s+(\d+\.\d+\.\d+)(?![\w.])\s+\|",
        1,
        "cheatsheet header stamp",
    ),
    (
        "docs/cheatsheet.ja.md",
        r"^>\s+Version\s+(\d+\.\d+\.\d+)(?![\w.])\s+\|",
        1,
        "cheatsheet header stamp (ja)",
    ),
    (
        "docs/README.md",
        r"architecture\.html\s+は\s+v(\d+\.\d+\.\d+)(?![\w.])",
        1,
        "the LIVE half of a mixed line — the `user-guide / workflow-guide は "
        "v0.12.0` clause on the same line is deliberately frozen and must not "
        "be matched by this anchor",
    ),
    # architecture.html carries three stamps AND two frozen historical `v0.8.0`
    # references, so a bare `v(\d+\.\d+\.\d+)` over-matches. Anchor each stamp.
    (
        "docs/architecture.html",
        r"バージョン\s+(\d+\.\d+\.\d+)(?![\w.])",
        1,
        "header stamp (:92). The `(2026-07-25 時点)` date on the same line is a "
        "human contract, not a pin — never rewrite it from a test",
    ),
    (
        "docs/architecture.html",
        r",\s*v(\d+\.\d+\.\d+)(?![\w.])\)",
        1,
        "tool-count caption stamp (:327)",
    ),
    (
        "docs/architecture.html",
        r"PM\s+Lens\s+v(\d+\.\d+\.\d+)(?![\w.])",
        1,
        "footer stamp (:756). `Generated 2026-06-15` on the same line is frozen "
        "by design; only the version moves",
    ),
)


# ─── Reverse layer: scope and suppressions ────────────────────────────────────
#
# Every file whose version-shaped strings are scanned wholesale. Keep this small
# and explicit; it is the false-positive suppression mechanism.

SCOPE_FILES: tuple[str, ...] = (
    "pyproject.toml",
    "manifest.json",
    ".claude-plugin/marketplace.json",
    "plugin/.mcp.json",
    "plugin/.claude-plugin/plugin.json",
    "plugin/README.md",
    "packaging/pm-server-wrapper/pyproject.toml",
    "src/pmlens/__init__.py",
    "docs/cheatsheet.md",
    "docs/cheatsheet.ja.md",
    "docs/architecture.html",
    "docs/README.md",
)

# uv.lock is in the release surface but is checked STRUCTURALLY, never
# textually — it holds thousands of third-party versions.
UV_LOCK = "uv.lock"

# Occurrences inside a scoped file that are legitimately NOT the pmlens version.
# Keyed by path; each entry is ``(line regex, value | None, reason)``.
#
# The line regex anchors on CONTEXT rather than on the bare value, so bumping a
# third-party floor cannot silently widen a suppression. The value narrows it
# further: when given, only that exact string is excused on a matching line —
# every other version-shaped string on the same line is still checked. That
# matters because ``docs/README.md:17`` carries the LIVE pin and a deliberately
# frozen one on one line; a line-wide suppression would hide a stale value added
# there forever, at this release and every future one.
#
# ``None`` means "every match on this line", used only where the whole line is
# third-party by construction (PEP 508 requirement strings).
IGNORED: dict[str, tuple[tuple[str, str | None, str], ...]] = {
    "pyproject.toml": (
        (
            # Not anchored at line start: `requires = ["setuptools>=68", ...]`
            # and the dev extra are inline arrays, and anchoring turned the
            # guard red on a release-correct tree the moment either grew a floor.
            r'"[\w.\-\[\]]+\s*[><=!~]',
            None,
            "third-party dependency floors (fastmcp, pyyaml, jinja2, filelock, "
            "build-system requires, dev extras…)",
        ),
    ),
    "plugin/README.md": ((r"Verified on Claude Code", "2.1.161", "the HOST's version, not ours"),),
    "packaging/pm-server-wrapper/pyproject.toml": (
        (
            r"pm-server==0\.10\.0",
            "0.10.0",
            "frozen: the last release that shipped real pm-server code, before "
            "the rename made this a metapackage (ADR-031/032)",
        ),
    ),
    "docs/cheatsheet.md": (
        (r"deprecated since v0\.6\.0", "0.6.0", "frozen: when the legacy alias was deprecated"),
    ),
    "docs/cheatsheet.ja.md": (
        (r"v0\.6\.0 以降 deprecated", "0.6.0", "frozen: when the legacy alias was deprecated"),
    ),
    "docs/architecture.html": (
        (r"outbox\.py</code> は v0\.8\.0", "0.8.0", "frozen: when outbox.py was introduced"),
        (r"PMSERV-105 \(v0\.8\.0 release\)", "0.8.0", "frozen: historical release credit"),
    ),
    "docs/README.md": (
        (
            r"user-guide / workflow-guide は v0\.12\.0 時点",
            "0.12.0",
            "frozen on purpose: only architecture.html was regenerated at "
            "v0.13.0, so these two guides genuinely describe 0.12.0. Scoped to "
            "this VALUE because the same line carries the live architecture.html "
            "pin — a line-wide excuse would hide a stale value added here.",
        ),
    ),
}


# ─── Forward tests ────────────────────────────────────────────────────────────


@pytest.mark.parametrize(("path", "pointer", "note"), JSON_PINS, ids=[p for p, _, _ in JSON_PINS])
def test_json_pin_matches_pyproject(path: str, pointer: str, note: str):
    ver = pyproject_version()
    found = _dig(json.loads(_read(path)), pointer)
    assert found == ver, (
        f"{path} {pointer} is {found!r}, expected {ver!r} — {note}. "
        "Bump every release surface in lockstep (PMSERV-133/172)."
    )


@pytest.mark.parametrize(
    ("path", "key", "note"), TOML_PINS, ids=[f"{p}:{k}" for p, k, _ in TOML_PINS]
)
def test_toml_pin_matches_pyproject(path: str, key: str, note: str):
    ver = pyproject_version()
    found = _dotted(tomllib.loads(_read(path)), key)
    assert found == ver, (
        f"{path} {key} is {found!r}, expected {ver!r} — {note}. "
        "Bump every release surface in lockstep (PMSERV-133/172)."
    )


@pytest.mark.parametrize(
    ("path", "pattern", "expected_count", "note"),
    TEXT_PINS,
    ids=[f"{p}::{pat[:28]}" for p, pat, _, _ in TEXT_PINS],
)
def test_text_pin_matches_pyproject(path: str, pattern: str, expected_count: int, note: str):
    ver = pyproject_version()
    found = re.findall(pattern, _read(path), flags=re.MULTILINE)
    assert len(found) == expected_count, (
        f"{path}: expected exactly {expected_count} match(es) of {pattern!r}, "
        f"got {len(found)} ({found}). A pin was added or removed — {note}. "
        "Register it here rather than loosening the count (PMSERV-172)."
    )
    assert all(v == ver for v in found), (
        f"{path} carries version(s) {sorted(set(found))} but pyproject is {ver!r} — "
        f"{note}. Bump in lockstep (PMSERV-172)."
    )


def test_wrapper_dependency_floor_matches_pyproject():
    """The wrapper must require the pmlens it was released alongside.

    Parsed with ``tomllib`` rather than substring-matched: the wrapper file
    carries the version in three comments too, so ``f'version = "{ver}"' in
    text`` was satisfiable by a comment alone, and any legal TOML reflow broke
    the dependency assertion.
    """
    ver = pyproject_version()
    deps = tomllib.loads(_read("packaging/pm-server-wrapper/pyproject.toml"))["project"][
        "dependencies"
    ]
    assert len(deps) == 1, (
        f"the pm-server wrapper is a metapackage and must declare exactly one "
        f"dependency (pmlens); found {deps}"
    )
    assert deps[0].replace(" ", "") == f"pmlens>={ver}", (
        f"wrapper dependency floor is {deps[0]!r}; expected 'pmlens>={ver}' so "
        f"`uvx pm-server@{ver}` installs the matching pmlens (PMSERV-172)."
    )


def test_uv_lock_root_version_matches_pyproject():
    """``uv.lock``'s own entry for this project must track pyproject.

    Since PMSERV-178 this is no longer the only thing standing between a stale
    uv.lock and a green build: ci.yml's ``lockfile-freshness`` job runs
    the locked export check, which fails when the lock no longer matches
    pyproject.toml. That check subsumes this one on paper — a changed project
    version is drift like any other. It is kept anyway for two reasons: it
    names the actual mismatch instead of reporting generic lock staleness, and
    it belongs to the release-surface pin sweep below, which must be readable
    as one set rather than one-test-here-one-job-there.

    Located by ``source.editable == "."`` rather than by name so that adding a
    second local editable member — the situation in which a second version pin
    would appear — also fails here.
    """
    ver = pyproject_version()
    doc = tomllib.loads(_read(UV_LOCK))
    roots = [p for p in doc["package"] if p.get("source", {}).get("editable") == "."]
    assert [r["name"] for r in roots] == ["pmlens"], (
        f"expected exactly one editable root named 'pmlens' in {UV_LOCK}, got "
        f"{[r['name'] for r in roots]} — a new workspace member needs its own "
        "lockstep entry here"
    )
    assert roots[0]["version"] == ver, (
        f"{UV_LOCK} pins pmlens {roots[0]['version']!r} but pyproject is {ver!r}. "
        "Run `uv lock` — do not hand-edit uv.lock (PMSERV-172)."
    )


# ─── Reverse test ─────────────────────────────────────────────────────────────


def test_no_unregistered_version_pins_in_release_surfaces():
    """Every ``x.y.z`` in a release-surface file is the SSoT or a stated exception.

    This is the layer the forward registry cannot provide: it catches a pin
    nobody remembered to register, which is exactly how the wrapper (v0.12.1)
    and ``plugin/README.md:46`` slipped through.
    """
    ver = pyproject_version()
    offenders: list[str] = []

    for path in SCOPE_FILES:
        suppressions = tuple(
            (re.compile(pattern), value) for pattern, value, _reason in IGNORED.get(path, ())
        )
        for lineno, line in enumerate(_read(path).splitlines(), 1):
            for match in _SEMVER.finditer(line):
                found = match.group(0)
                if found == ver:
                    continue
                if any(
                    rx.search(line) and (value is None or value == found)
                    for rx, value in suppressions
                ):
                    continue
                offenders.append(f"{path}:{lineno}: {found!r} | {line.strip()[:100]}")

    assert not offenders, (
        f"version-shaped strings in release-surface files that are neither the "
        f"current version ({ver}) nor a registered exception. Either bump them "
        f"in lockstep, or add an IGNORED entry WITH A REASON in "
        f"tests/test_version_lockstep.py (PMSERV-172):\n  " + "\n  ".join(offenders)
    )


def test_scope_and_registry_agree():
    """Scope and forward registry must describe the same surface.

    A file in the registry but not in scope gets no reverse coverage; a file in
    scope but in no registry entry is scanned for staleness yet has no
    authoritative extractor. Both are drift, and both are silent without this.
    """
    registered = (
        {p for p, _, _ in JSON_PINS}
        | {p for p, _, _ in TOML_PINS}
        | {p for p, _, _, _ in TEXT_PINS}
    )
    # pyproject.toml is the SSoT itself, so it has no pin entry of its own —
    # but it must still be SCANNED. Subtracting it without asserting its
    # presence let it be dropped from SCOPE_FILES with both assertions green,
    # silently ending reverse coverage of the one file that defines the version.
    assert "pyproject.toml" in SCOPE_FILES, (
        "the SSoT file must stay in reverse-scan scope; a stray version string "
        "in pyproject.toml is exactly what nobody would notice"
    )
    scoped = set(SCOPE_FILES) - {"pyproject.toml"}

    assert registered - scoped == set(), (
        f"registered but out of reverse scope: {sorted(registered - scoped)} — "
        "add them to SCOPE_FILES"
    )
    assert scoped - registered == set(), (
        f"in reverse scope but with no forward extractor: {sorted(scoped - registered)} — "
        "add a JSON_PINS/TOML_PINS/TEXT_PINS entry so the pin has an owner"
    )


def test_release_runbook_lists_every_surface():
    """``docs/RELEASING.md``'s pre-flight table must name every scoped file.

    A human bumping versions follows the runbook, not this registry. A file
    guarded here but missing from the table gets forgotten until CI catches it;
    a file in the table but not here is guarded by nobody. Keeping the two in
    sync by hand is exactly the drift this module exists to prevent, so assert
    it instead.
    """
    runbook = _read("docs/RELEASING.md")
    missing = [path for path in SCOPE_FILES + (UV_LOCK,) if path not in runbook]
    # pyproject.toml is named in prose as the SSoT rather than as a row.
    missing = [p for p in missing if p != "pyproject.toml"]
    assert not missing, (
        "docs/RELEASING.md's pre-flight checklist does not mention "
        f"{missing} — a release surface the runbook never tells you to bump "
        "(PMSERV-172)"
    )


def test_every_registered_path_exists():
    """A renamed or deleted surface must fail loudly, not silently stop being checked."""
    missing = [path for path in {*SCOPE_FILES, UV_LOCK} if not (REPO_ROOT / path).is_file()]
    assert not missing, f"release-surface files missing from the repo: {missing}"
