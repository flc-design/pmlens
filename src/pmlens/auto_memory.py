"""Auto-memory bridge — ADR-040 / PMSERV-112 v1.

Two bridges between Claude Code's native "auto-memory" store
(``~/.claude/projects/<repo>/memory/*.md``) and the PM ledger, designed so
they can never feed each other:

1. **Read-time overlay** (:func:`build_auto_memory_overlay`): parse
   auto-memory markdown files at query time and surface them in ``pm_recall``
   under a distinct, source-tagged additive key ``auto_memory_entries[]``.
   Nothing is copied into ``memory.db`` — this is dual-write-safe (PMSERV-111:
   no split-brain) and read-only (PMSERV-144/154: safe under ``PM_LENS=1``).

2. **Reverse bridge** (:func:`sync_memory_md_pointer`): append an idempotent
   pointer block into ``MEMORY.md`` so the always-in-context CC index points
   back at the PM ledger. Writer-only, ``PM_LENS=0`` only, opt-in.

The overlay SKIPS ``MEMORY.md`` (a derived pointer index) and the reverse
bridge writes ONLY ``MEMORY.md`` — so the two directions are disjoint and an
ingest loop is structurally impossible (ADR-040).

Physical ingest into ``memory.db`` (provenance columns, content-hash dedup,
cross-project re-export) is deliberately deferred to the physical follow-up
(PMSERV-156); v1 touches none of ``MemoryStore.save`` / ``sync_to_global``.

Provenance is preserved (``source_file`` + ``session_id``), unlike the outbox
*merge* path which drops ``source_session``/``host_id`` (memory:265) — the v1
overlay follows the outbox *read overlay*, not the merge.
"""

from __future__ import annotations

import datetime as _dt
import re
from pathlib import Path

import yaml

from .models import ProjectNotFoundError
from .utils import _atomic_write_text, resolve_project_path

# ─── Constants ───────────────────────────────────────

#: Derived pointer index — always excluded from the overlay so the reverse
#: bridge (which writes ONLY this file) can never be re-ingested (ADR-040).
MEMORY_INDEX_FILENAME = "MEMORY.md"

#: Provenance tag stamped on every overlay entry so a reader (human or model)
#: never confuses an auto-memory note with a promoted PM memory (mirrors the
#: outbox overlay's ``source="outbox(unmerged)"`` — server.py).
AUTO_MEMORY_SOURCE = "auto_memory"

#: Dedicated marker block for the reverse bridge. Deliberately NOT the
#: ``pm-server:begin`` slug used by rules.py for the CLAUDE.md rules section —
#: reusing that would let a CLAUDE.md scan misread the block, and would risk
#: clobbering. MEMORY.md is never processed by rules.py, so a fresh slug is
#: free of collision concerns.
BRIDGE_BEGIN = "<!-- pm-lens:bridge:begin -->"
BRIDGE_END = "<!-- pm-lens:bridge:end -->"

_AUTO_MEMORY_MAX_ENTRIES = 10
_AUTO_MEMORY_CONTENT_LIMIT = 500
_POINTER_EXCERPT_LIMIT = 100

# Claude Code encodes a project's absolute path into its
# ``~/.claude/projects/<name>`` directory name by replacing path punctuation
# with ``-``. The exact rule has drifted across CC versions (so the same repo
# can own several directories — memory:265), therefore the name is NOT
# forward-computed as a single string: :func:`locate_auto_memory_dirs`
# enumerates and matches against a small candidate set.
_NON_ALNUM = re.compile(r"[^0-9A-Za-z]")


def encode_project_dirname(path: Path | str) -> str:
    """Encode an absolute project path to Claude Code's current dir-name rule.

    The current CC rule maps every non-alphanumeric character to ``-`` (so
    ``/``, ``_``, ``.`` and existing ``-`` all collapse). This is the encoding
    the *running* CC uses, so it is the right target for the reverse-bridge
    WRITE path; the READ locator additionally tolerates a legacy variant.
    """
    return _NON_ALNUM.sub("-", str(path))


def _encode_candidates(project_root: Path) -> list[str]:
    """Candidate ``~/.claude/projects`` dir names for one repo (drift-tolerant).

    Two rules seen in the wild:
      * current: every non-alnum char → ``-`` (``encode_project_dirname``).
      * legacy: only the path separator ``/`` → ``-`` (keeps ``_`` / ``.``).
    Both are matched so a directory created by an older CC still resolves. A
    non-matching candidate is simply never found on disk, so listing extras is
    harmless.
    """
    s = str(project_root)
    candidates = [encode_project_dirname(s), s.replace("/", "-")]
    # Preserve order, drop dupes (identical when the path has no ``_``/``.``).
    seen: set[str] = set()
    out: list[str] = []
    for c in candidates:
        if c not in seen:
            seen.add(c)
            out.append(c)
    return out


def _home(home: Path | None) -> Path:
    """Resolve the home dir at call time (respects ``$HOME`` on POSIX).

    ``Path.home()`` delegates to ``os.path.expanduser("~")``, which honours
    ``$HOME`` — so tests can redirect it with ``monkeypatch.setenv("HOME", …)``
    exactly like the Lens invariant sweep. Never ``Path("~/.claude")`` (a
    literal tilde is not expanded — CLAUDE.md "よくある間違い").
    """
    return home if home is not None else Path.home()


# ─── Locator ─────────────────────────────────────────


def locate_auto_memory_dirs(
    project_path: str | None = None,
    *,
    auto_memory_path: str | None = None,
    home: Path | None = None,
) -> list[Path]:
    """Find the auto-memory directory/directories for the current repo.

    Resolution order (override-then-discover, mirroring
    ``resolve_project_path``):

    1. ``auto_memory_path`` explicit override — accepts either the ``memory/``
       directory itself or a parent that contains one. Escape hatch for the
       encoding drift described on :data:`_NON_ALNUM`.
    2. Otherwise resolve the project root and ``enumerate-and-match``:
       enumerate ``~/.claude/projects/*/memory`` and keep the ones whose
       parent name matches a :func:`_encode_candidates` entry for this repo.

    Never raises: returns ``[]`` when the project cannot be resolved or nothing
    matches (the overlay must not break ``pm_recall`` — ADR-039 T1 read-purity
    discipline carried over to auto-memory).
    """
    if auto_memory_path:
        try:
            p = Path(auto_memory_path).expanduser().resolve()
        except (OSError, RuntimeError, ValueError):
            return []
        # Prefer a ``memory/`` child (override points at the project dir) over
        # treating ``p`` itself as the memory dir — otherwise a parent that both
        # has a memory/ child AND a stray top-level *.md would mis-resolve to
        # the parent and scan the wrong directory.
        child = p / "memory"
        if child.is_dir():
            return [child]
        if (p / MEMORY_INDEX_FILENAME).is_file() or any(p.glob("*.md")):
            return [p]
        return [p] if p.is_dir() else []

    try:
        project_root = resolve_project_path(project_path)
    except ProjectNotFoundError:
        return []
    except Exception:  # pragma: no cover — defensive: never break recall
        return []

    projects_root = _home(home) / ".claude" / "projects"
    if not projects_root.is_dir():
        return []

    wanted = set(_encode_candidates(project_root))
    dirs: list[Path] = []
    try:
        for entry in sorted(projects_root.iterdir()):
            if entry.name in wanted:
                mem = entry / "memory"
                if mem.is_dir():
                    dirs.append(mem)
    except OSError:  # pragma: no cover — defensive FS guard
        return []
    return dirs


# ─── Parser ──────────────────────────────────────────


def _split_frontmatter(text: str) -> tuple[dict, str]:
    """Split a leading ``---`` YAML frontmatter block from the body.

    Returns ``(frontmatter_dict, body)``. When there is no well-formed leading
    fence, or the YAML is malformed / not a mapping, degrades to ``({}, text)``
    so a bad file still contributes its raw body rather than vanishing or
    raising (uniform ``or {}`` + isinstance guard convention — storage.py /
    utils.py / prompt_pack.py).
    """
    if not text.startswith("---"):
        return {}, text
    # The opening fence is the first line; find the closing ``---`` fence.
    lines = text.splitlines(keepends=True)
    if not lines or lines[0].strip() != "---":
        return {}, text
    for i in range(1, len(lines)):
        if lines[i].strip() == "---":
            block = "".join(lines[1:i])
            body = "".join(lines[i + 1 :])
            try:
                data = yaml.safe_load(block)
            except yaml.YAMLError:
                return {}, text
            if not isinstance(data, dict):
                return {}, body
            return data, body
    # Opening fence but no closing fence — treat whole thing as body.
    return {}, text


def _first_present(frontmatter: dict, *keys: str) -> object | None:
    """Return the first present value across top-level and ``metadata`` scope.

    Auto-memory frontmatter carries fields at the top level in some files and
    nested under ``metadata:`` in others (both shapes exist in the wild —
    ADR-040). This reads each key at the top level first, then under
    ``metadata``.
    """
    metadata = frontmatter.get("metadata")
    metadata = metadata if isinstance(metadata, dict) else {}
    for key in keys:
        if frontmatter.get(key) is not None:
            return frontmatter.get(key)
        if metadata.get(key) is not None:
            return metadata.get(key)
    return None


def _mtime_iso(path: Path) -> str | None:
    """File mtime as an ISO-8601 string (auto-memory has no ``created_at``)."""
    try:
        ts = path.stat().st_mtime
    except OSError:  # pragma: no cover — defensive FS guard
        return None
    return _dt.datetime.fromtimestamp(ts).isoformat(timespec="seconds")


def _truncate(text: str, limit: int) -> tuple[str, bool]:
    if len(text) > limit:
        return text[:limit], True
    return text, False


def parse_auto_memory_file(path: Path) -> dict | None:
    """Parse one auto-memory ``*.md`` file into an overlay-entry dict.

    Returns ``None`` only for the derived index (``MEMORY.md``) or an
    unreadable file. Malformed frontmatter degrades to an entry with
    ``type=None`` and the raw body, never raises (read-purity).

    The entry keeps the 8-key ``recent_memories`` shape (``type`` is the raw
    ``origin_type`` string — NOT coerced to :class:`MemoryType`, whose reader
    strictly validates the enum) plus provenance keys ``source_file`` /
    ``session_id`` and the ``source`` tag.
    """
    if path.name == MEMORY_INDEX_FILENAME:
        return None
    try:
        # utf-8-sig strips a leading BOM (else _split_frontmatter's ``---``
        # probe fails and all frontmatter is lost); errors="replace" keeps a
        # single non-UTF-8 byte from raising UnicodeDecodeError (a ValueError,
        # NOT an OSError) up into pm_recall and breaking a read-only tool.
        text = path.read_text(encoding="utf-8-sig", errors="replace")
    except OSError:  # pragma: no cover — defensive FS guard
        return None

    frontmatter, body = _split_frontmatter(text)

    origin_type = _first_present(frontmatter, "type")
    name = _first_present(frontmatter, "name")
    description = _first_present(frontmatter, "description")
    session_id = _first_present(frontmatter, "originSessionId", "session_id")
    raw_tags = _first_present(frontmatter, "tags")
    if isinstance(raw_tags, str):
        tags: list[str] | None = [t.strip() for t in raw_tags.split(",") if t.strip()]
    elif isinstance(raw_tags, list):
        tags = [str(t) for t in raw_tags]
    else:
        tags = None

    content, truncated = _truncate(body.strip(), _AUTO_MEMORY_CONTENT_LIMIT)

    entry: dict = {
        "name": str(name) if name is not None else path.stem,
        "type": str(origin_type) if origin_type is not None else None,
        "content": content,
        "description": str(description) if description is not None else None,
        "tags": tags,
        "created_at": _mtime_iso(path),
        "session_id": str(session_id) if session_id is not None else None,
        "source_file": path.name,
        "source": AUTO_MEMORY_SOURCE,
    }
    if truncated:
        entry["content_truncated"] = True
    return entry


def _entry_matches_query(entry: dict, query: str) -> bool:
    """Case-insensitive substring match across the readable fields.

    v1 uses a simple substring filter (no FTS) — physical ingest with FTS rank
    is the deferred follow-up (PMSERV-156).
    """
    q = query.lower()
    haystacks = [
        entry.get("content") or "",
        entry.get("name") or "",
        entry.get("description") or "",
        " ".join(entry.get("tags") or []),
        entry.get("type") or "",
    ]
    return any(q in h.lower() for h in haystacks)


# ─── Overlay builder ─────────────────────────────────


def build_auto_memory_overlay(
    project_path: str | None,
    query: str | None,
    limit: int,
    auto_memory_path: str | None = None,
    *,
    home: Path | None = None,
) -> dict:
    """Build the ``include_auto_memory=True`` overlay for ``pm_recall``.

    Returns ``{"auto_memory_entries": [...], "auto_memory_summary": {...}}`` —
    two additive keys mirroring the outbox overlay's contract. Read-only: never
    creates a directory or file and never raises into ``pm_recall`` (all IO is
    guarded). ``MEMORY.md`` is always skipped.

    * default path (``query is None``): most-recent-first, capped at
      ``min(limit, 10)``.
    * query path: entries whose readable fields substring-match ``query``,
      tagged ``match_source="auto_memory_like"``.
    """
    dirs = locate_auto_memory_dirs(project_path, auto_memory_path=auto_memory_path, home=home)

    entries: list[dict] = []
    seen_files: set[str] = set()
    scanned_dirs = 0
    for d in dirs:
        try:
            md_files = sorted(d.glob("*.md"))
        except OSError:  # pragma: no cover — defensive FS guard
            continue
        scanned_dirs += 1
        for f in md_files:
            if f.name == MEMORY_INDEX_FILENAME:
                continue
            # Drift can surface the same repo under two dir names; dedupe by
            # basename so a file is not listed twice.
            if f.name in seen_files:
                continue
            try:
                entry = parse_auto_memory_file(f)
            except Exception:  # pragma: no cover — no single file may break recall
                continue
            if entry is None:
                continue
            seen_files.add(f.name)
            entries.append(entry)

    total_available = len(entries)
    # Most-recent first (created_at is mtime ISO; None sorts last).
    entries.sort(key=lambda e: e.get("created_at") or "", reverse=True)

    cap = max(0, min(limit, _AUTO_MEMORY_MAX_ENTRIES))
    if query:
        matched = [e for e in entries if _entry_matches_query(e, query)]
        for e in matched:
            e["match_source"] = "auto_memory_like"
        listed = matched[:cap]
    else:
        listed = entries[:cap]

    return {
        "auto_memory_entries": listed,
        "auto_memory_summary": {
            "total_available": total_available,
            "scanned_dirs": scanned_dirs,
            "listed": len(listed),
            "scope": "project",
        },
    }


# ─── Reverse bridge (MEMORY.md pointer) ──────────────


def _pointer_excerpt(content: str) -> str:
    """One-line, punctuation-safe excerpt for a MEMORY.md list item."""
    # First non-empty line, strip markdown heading/list markers that would
    # break the enclosing bullet list, collapse whitespace.
    first = ""
    for line in content.splitlines():
        stripped = line.strip().lstrip("#->*").strip()
        if stripped:
            first = stripped
            break
    # Neutralise any HTML-comment tokens so a memory whose text contains the
    # bridge markers (or any ``<!-- ... -->``) can never smuggle a second
    # BRIDGE_END into the block body and corrupt the splice on the next sync.
    for token in (BRIDGE_BEGIN, BRIDGE_END, "<!--", "-->"):
        first = first.replace(token, "")
    first = re.sub(r"\s+", " ", first).strip()
    excerpt, _ = _truncate(first, _POINTER_EXCERPT_LIMIT)
    return excerpt


def _pointer_line(memory_id: int, mtype: str, created_at: str, content: str) -> str:
    """Render the idempotent pointer line for one PM memory.

    The ``- PM #<id> `` prefix (note trailing space) is the dedup key, chosen
    so ``#12`` never prefix-collides with ``#120``.
    """
    date = (created_at or "").split(" ")[0].split("T")[0]
    date_part = f" ({date})" if date else ""
    excerpt = _pointer_excerpt(content)
    return f"- PM #{memory_id} [{mtype}]{date_part}: {excerpt} — recall via pm_recall"


def _splice_bridge_block(existing: str, block_body: str) -> str:
    """Idempotent marker-block splice (mirrors rules.py ``_inject_into_file``).

    Replaces the region between :data:`BRIDGE_BEGIN`/:data:`BRIDGE_END` with a
    freshly-rendered block, appends the block if absent, and self-heals a
    corrupted begin-without-end. The caller compares the result to ``existing``
    and only writes when it differs (the no-op guard that makes re-runs leave
    the file byte-identical — PMSERV-062).
    """
    block = f"{BRIDGE_BEGIN}\n{block_body}\n{BRIDGE_END}"
    begin_idx = existing.find(BRIDGE_BEGIN)
    end_idx = existing.find(BRIDGE_END)

    if begin_idx != -1 and end_idx != -1 and end_idx > begin_idx:
        before = existing[:begin_idx]
        after = existing[end_idx + len(BRIDGE_END) :]
        return before + block + after
    if begin_idx != -1:
        # Corrupted: begin without a (following) end — truncate and re-append.
        before = existing[:begin_idx]
        return before.rstrip() + "\n\n" + block + "\n"
    # No markers — append after existing content.
    if existing and not existing.endswith("\n"):
        existing = existing + "\n"
    sep = "\n" if existing else ""
    return existing + sep + block + "\n"


def _existing_bridge_lines(existing: str) -> list[str]:
    """Return the pointer lines currently inside the bridge block (if any)."""
    begin_idx = existing.find(BRIDGE_BEGIN)
    end_idx = existing.find(BRIDGE_END)
    if begin_idx == -1 or end_idx == -1 or end_idx <= begin_idx:
        return []
    inner = existing[begin_idx + len(BRIDGE_BEGIN) : end_idx]
    return [ln.rstrip() for ln in inner.splitlines() if ln.strip()]


def sync_memory_md_pointer(
    memory_id: int,
    mtype: str,
    content: str,
    *,
    project_path: str | None = None,
    auto_memory_path: str | None = None,
    home: Path | None = None,
    created_at: str = "",
) -> dict:
    """Append an idempotent pointer to the PM ledger into ``MEMORY.md``.

    The reverse half of the auto-memory bridge (ADR-040). Opt-in and — by the
    caller's contract — ``PM_LENS=0`` only (the tool is already hidden under
    ``PM_LENS=1`` via ``RO_ALLOWLIST`` gating; the caller adds the explicit
    guard as defense-in-depth, PMSERV-144 pattern). Writes ONLY ``MEMORY.md``,
    which the overlay never reads, so no ingest loop.

    Idempotent: a pointer for ``memory_id`` already present makes this a no-op
    (``skipped=True``, file byte-unchanged). The target dir is created when
    absent so the feature works on first use.

    Returns a small result dict (``synced`` / ``skipped`` / ``path`` / an
    ``error`` on failure) so a silent miss never "looks like a bug".
    """
    target_dir = _resolve_bridge_dir(project_path, auto_memory_path, home)
    if target_dir is None:
        return {"synced": False, "skipped": False, "error": "could not resolve target project"}

    memory_md = target_dir / MEMORY_INDEX_FILENAME
    new_line = _pointer_line(memory_id, mtype, created_at, content)
    dedup_prefix = f"- PM #{memory_id} "

    try:
        existing = memory_md.read_text(encoding="utf-8") if memory_md.exists() else ""
    except OSError as e:  # pragma: no cover — defensive FS guard
        return {"synced": False, "skipped": False, "error": f"read failed: {e}"}

    current_lines = _existing_bridge_lines(existing)
    if any(ln.startswith(dedup_prefix) for ln in current_lines):
        return {"synced": True, "skipped": True, "path": str(memory_md)}

    block_body = "\n".join([*current_lines, new_line])
    new_content = _splice_bridge_block(existing, block_body)
    if new_content == existing:  # pragma: no cover — dedup covers this above
        return {"synced": True, "skipped": True, "path": str(memory_md)}

    try:
        target_dir.mkdir(parents=True, exist_ok=True)
        _atomic_write_text(memory_md, new_content)
    except OSError as e:  # pragma: no cover — defensive FS guard
        return {"synced": False, "skipped": False, "error": f"write failed: {e}"}
    return {"synced": True, "skipped": False, "path": str(memory_md)}


def _resolve_bridge_dir(
    project_path: str | None,
    auto_memory_path: str | None,
    home: Path | None,
) -> Path | None:
    """Pick the ``memory/`` dir to write ``MEMORY.md`` into.

    Prefers an existing located dir (what the running CC actually uses); falls
    back to the current-rule forward-computed canonical path so the feature
    works before CC has created the dir. Returns ``None`` only when the project
    cannot be resolved and no override is given.
    """
    located = locate_auto_memory_dirs(project_path, auto_memory_path=auto_memory_path, home=home)
    if located:
        return located[0]
    if auto_memory_path:
        try:
            return Path(auto_memory_path).expanduser().resolve()
        except (OSError, RuntimeError, ValueError):
            return None
    try:
        project_root = resolve_project_path(project_path)
    except ProjectNotFoundError:
        return None
    return _home(home) / ".claude" / "projects" / encode_project_dirname(project_root) / "memory"
