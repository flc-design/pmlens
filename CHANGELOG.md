# Changelog

## [Unreleased]

### Added

- **Destination-neutral content tool names with compatibility aliases
  (PMSERV-182 / PMSERV-189)**: `pm_draft_content` and
  `pm_drafts_pending` are the preferred names. `pm_draft_x` and
  `pm_x_drafts_pending` remain callable with the same arguments and results,
  sharing the same project-local store. Full mode exposes 46 tool names for
  44 operations; Lens remains at 16 names, or 18 with Desktop outbox writes.
  No legacy-name removal version is scheduled. See
  [the migration guide](docs/content-tool-migration.md).

### Changed

- **Executable dependency lock synchronization (PMSERV-186)**: `uv.lock` now
  owns the resolution and `requirements.lock` is its universal hash-bearing
  export. CI and release verification reject project/lock/export drift with
  the same offline check. `make lock-sync` preserves compatible pins;
  `make lock-refresh` explicitly upgrades them. The hash-verified CI install
  also runs `pip check`, so `--no-deps` cannot hide incompatible project ranges.
  This verifies the selected resolution, not the availability of newer PyPI
  releases. Initial alignment moves 18 uv pins to the versions already in
  `requirements.lock`, preserves uv's newer pip 26.2.1, and includes the
  Python-conditional dependencies already present in uv.lock.
- Rule template v14 and new built-in content workflows use the preferred tool
  names. Existing host permissions and copied workflows using the legacy names
  continue to work.
- **Neutral draft storage names with legacy database support (PMSERV-190)**:
  new projects use `.pm/drafts.db`; existing `.pm/x_drafts.db` files continue
  being used in place, including their WAL/SHM files. The implementation moves
  to `pmlens.draft_store` and five test files lose their `x` prefix. The old
  Python module re-exports the same implementation and factory cache. SQL
  schema, IDs, states and append-only triggers stay unchanged. If both database
  names exist, content tools report `draft_store_conflict` without opening
  either; `pm_status` warns and reports an unknown pending count. Older versions
  cannot discover a new `drafts.db`; see the migration guide before mixing
  versions or downgrading.

## [0.15.1] - 2026-09-07

Maintenance release with accurate MCP server identity, FastMCP 4.x support,
dependency lock updates, and corrected installation guidance.

MCP tool count: 44 (unchanged). Test suite: 1,530 passing.

### Changed

- **fastmcp upper bound raised to `<5.0`; fresh installs now resolve fastmcp 4.x
  (PMSERV-185)**: the shipped range moves from `fastmcp>=3.2.0,<4.0` to
  `>=3.2.0,<5.0`, so `pipx install pmlens` now pulls fastmcp 4 and, with it,
  MCP Python SDK 2.x in place of 1.x and `httpx2` in place of `httpx`.

  fastmcp 4 removes server-initiated sampling and roots, restricts
  `ctx.elicit()` to the old protocol, and drops the 3.x module shims. None of
  that reaches this codebase: the entire framework surface in use is
  `FastMCP(name, version=...)`, `mcp.tool()(fn)` and `mcp.run(transport="stdio")`, and
  `Context`, `ctx.*`, sampling, elicitation and roots do not appear in `src/`
  at all. Both fastmcp 3.4.6 and 4.0.3 negotiate protocol revision
  `2025-11-25` in the verified stdio handshake. The revision a client
  negotiates and the revision a framework release announces are separate facts.

  The defensive upper bound itself is retained one major later, for the same
  reason v0.5.0 introduced it: a floating range is what users install, and a
  future 5.x is exactly the event nobody will be watching for.

- **`requirements.lock` had no update path at all, automated or manual
  (PMSERV-185)**: Dependabot's `pip` ecosystem entry has never once touched the
  file — the only `pip`-ecosystem PR this repository has ever received changed
  `pyproject.toml` alone — because that ecosystem matches `requirements*.txt`
  rather than a `.lock` name. Independently, the regeneration command
  documented in `ci.yml` omitted `--upgrade`, and `uv pip compile` reads its
  existing output file as resolution preferences: run as written against a lock
  that already exists, it produced a byte-identical pin set while fastmcp 4 sat
  on PyPI.

  So the hash-verified tree would have stayed on fastmcp 3.4.5 while
  `pyproject.toml` said `<5.0` and `uv.lock` said 4.x — the three-way split
  v0.15.0 removed, restored, with `test-locked` green throughout because that
  job installs the project with `--no-deps` and 3.4.5 still satisfies the
  widened range. Both lockfiles now pin fastmcp 4.0.3, the version the floating
  matrix actually resolved and proved green, so the tested tree and the pinned
  tree are the same tree.

  Both comments are corrected to state what is true rather than what was
  intended. PMSERV-186 tracks replacing them with a guard: this is the second
  time a defense in this area was documented and not executed, and a comment
  cannot fail a build.

### Security

- **Refresh the locked dependency versions**: `uv.lock` now pins
  `cryptography` 50.0.0 (GHSA-g6cj-pr64-35w5) and `pip` 26.2.1;
  `requirements.lock` already pinned `cryptography` 50.0.0 and now pins
  `pip` 26.2. These changes update the reproducible development and test
  environments. Standard PyPI installs resolve dependencies from
  `pyproject.toml` rather than either lockfile.

### Fixed

- **MCP server identity reports the pmlens version (PMSERV-179)**:
  `serverInfo.version` now uses the same `__version__` as the CLI. Previously,
  FastMCP supplied its own framework version, making it hard to identify the
  running pmlens release. Real stdio regression tests cover full and Lens modes.

- **Installation guidance uses the current package (PMSERV-183)**:
  README and quick-reference instructions now recommend `pipx install pmlens`
  and `pipx upgrade pmlens`, avoiding the retired package name in new-user
  instructions and supporting externally managed Python installations.

- **Architecture documentation reports the correct Lens tool counts
  (PMSERV-184)**: 16 tools in read-only Lens mode and 18 with Desktop outbox
  writes enabled, out of 44 total tools.

- **Release recovery guidance reflects the unified publisher (PMSERV-174)**:
  document the completed move to one approval for both distributions and the
  publisher configuration required to restore the fallback wrapper workflow.

## [0.15.0] - 2026-08-01

Three things had been broken for months, and all three were invisible because
each failed without producing an error. A read-only Lens could not see its own
project — `pm_recall` returned three-week-old context while the newest summaries
sat committed and durable on disk. The Claude Code plugin had never once started
the MCP server, from its first commit. And the hash-verified install the
changelog has advertised since v0.6.1 was never executed by anything.

Each is fixed here alongside the guard that would have caught it: a wire-level
test that spawns the server and completes a real MCP handshake, a freshness
check on the cached read path, and a CI job that performs the `--require-hashes`
install for real. That last guard earned its keep immediately — it went red on
its first run, over a platform-conditional dependency a macOS-generated lockfile
could not have contained.

The content pipeline also loses its "X / build-in-public" naming, which described
one destination rather than the capability and implied a risk the implementation
does not carry.

MCP tool count: 44 (unchanged). Test suite: 1,528 passing. ADR-051, ADR-052.

### Changed

- **The content pipeline is no longer named after one destination (PMSERV-181)**:
  it shipped as "X Content Pipeline (Build-in-Public)", which described the
  author's channel rather than the capability. The capability is turning
  knowledge already recorded in `.pm` into a redacted, publishable draft — a
  blog post, an internal write-up, a social thread; the pipeline does not care
  and never did.

  The old name was actively working against the implementation. The design's
  strongest property is that the server holds credentials for no destination
  and has no network egress in scope, so publishing from it is *structurally*
  impossible rather than merely disallowed — the kind of property a security
  reviewer wants to find. Labelled "build-in-public X drafts" in the feature
  list, that reviewer instead reads "posts our internal project context to
  social media" and stops. Same class of defect as the lockfile entry above,
  pointing the other way: there the docs claimed a protection that did not run,
  here they implied a risk that does not exist.

  Renamed across README (EN/JA), both cheatsheets, `architecture.html`,
  `workflow-guide.html`, and the `content-pipeline` workflow template — which
  already carried the neutral name, so this mostly aligns everything else to it.
  `TEMPLATE_VERSION` 12 → 13 because the rule section injected into every
  managed `CLAUDE.md` / `AGENTS.md` carried the old heading; that is the
  widest-reaching surface the framing appeared on. Behaviour, tools, schema and
  the redaction guarantee are all unchanged. Tool identifiers still contain
  `x` (`pm_draft_x`, `pm_x_drafts_pending`) — renaming those is a breaking MCP
  API change, tracked separately.

### Security

- **The hash-verified install is now actually executed (PMSERV-178)**: v0.6.1
  added `requirements.lock` with SHA-256 hashes and described
  `pip install --require-hashes -r requirements.lock` as the structural defense
  against mid-flight package replacement. Nothing ever ran it. CI, release, and
  the Makefile all installed with `pip install -e ".[dev]"`; no Dependabot
  ecosystem covered Python; and the file drifted two minor versions behind the
  environment anyone actually used, pinning `fastmcp 3.2.4` while both the
  installed toolchain and `uv.lock` had moved on. A documented-but-unexecuted
  security control is worse than an absent one — it reads as a guarantee to
  anyone auditing the repository, and takes about thirty seconds to disprove.

  `ci.yml` gains a `test-locked` job that installs the tree with
  `--require-hashes` and runs the suite against it, and a `lockfile-freshness`
  job that runs `uv lock --check`. The existing matrix job keeps resolving
  dependencies fresh, which is not redundancy: this project ships a floating
  range (`fastmcp>=3.2.0,<4.0`), so users get whatever resolves at install time,
  and the floating job is the early warning for upstream breakage. Dependabot
  now covers `pip` (requirements.lock) and `uv` (uv.lock) alongside
  `github-actions`.

  `requirements.lock` is now generated with
  `uv pip compile --universal --generate-hashes --all-extras`, replacing
  `pip-compile`. This is a correctness fix, not a preference: `pip-compile`
  resolves for the environment it runs in, and `keyring` pulls `SecretStorage`
  and `jeepney` only under `sys_platform == "linux"`. A lock generated on macOS
  therefore omitted them, and `test-locked` failed on its first run against the
  Linux runner — pip refuses an unpinned transitive requirement under
  `--require-hashes`. A universal resolution emits every platform's
  dependencies behind environment markers, so one file installs correctly on
  both; regenerating on Linux instead would only have moved the hole. Verified
  on `linux/amd64` and macOS.

  Scope stated precisely, so this entry does not repeat the mistake it fixes:
  every runtime and dev dependency installed by `test-locked` is hash-verified.
  The build backend for the editable install (hatchling) is fetched by pip's
  build isolation and is **not** hash-pinned — it is a build-time input, not a
  shipped dependency.

  The two lockfiles now have distinct, documented roles instead of silently
  disagreeing: `requirements.lock` is the hash-verified install,
  `uv.lock` is the development environment (`.devcontainer`,
  `uv run --project`). The guards do not depend on Dependabot working —
  `uv lock --check` fails on drift, and a requirements.lock missing a
  dependency fails `test-locked` on import.

### Fixed

- **A Lens viewer could not see its own project (PMSERV-176, ADR-051)**: a new
  session's `pm_recall` returned a three-week-old summary while the newest one
  sat committed and durable on disk — 16 summaries and 29 memories invisible.
  Two mechanisms stacked. The read path opens the database
  `mode=ro&immutable=1`, which makes SQLite skip WAL processing entirely, so the
  reader saw only what had reached the main file; the default
  `wal_autocheckpoint` is 1000 pages (~3.9 MiB) and this repository's database
  stayed under it. And `server._memory_stores` cached stores for the process
  lifetime while `immutable=1` disables change detection, so the view was pinned
  to whatever was first read — an explicit checkpoint changed nothing.

  Neither is fixable on the read path: reading a WAL database read-only needs
  the `-shm` sidecar, and creating one breaks the ADR-028 invariant that a Lens
  host never writes into another project's `.pm/`. So the fix splits by
  ownership. The writer checkpoints (`PASSIVE`, after `save_session_summary` —
  the datum `pm_recall` returns at session start), failure recorded and never
  raised. The reader re-stamps the database file each read and reopens on
  change, using `(exists, size, mtime_ns, header change counter)` — `stat()`
  plus a 28-byte header read, so no connection, no lock, no sidecar. What stays
  invisible is disclosed as `stale_wal_bytes` / `stale_note`, because the
  expensive part of this bug was a caller reading "no recent records" as fact.

  Absence is a stamp too, which also un-sticks the cached in-memory fallback: a
  project whose database did not exist at first touch was reported as having no
  memory at all, permanently, even after `pm_init`.

- **The Claude Code plugin never started the MCP server (PMSERV-177)**:
  `plugin/.mcp.json` launched `uvx pm-server@<version>` with no subcommand.
  `__main__.cli` is a plain `@click.group()` with no `invoke_without_command`,
  so that argv printed usage and exited 2 without reaching `mcp.run()`. It
  shipped that way from the plugin's first commit; only the version string was
  ever bumped. The other three registration surfaces all passed `serve` — the
  gap was that no test read the two static config files for launch shape.
  `tests/test_launch_surfaces.py` now asserts every shipped launch config ends
  in `serve`, fails when an unregistered `.mcp.json` appears, and completes a
  real stdio MCP handshake against a spawned server.

## [0.14.0] - 2026-07-27

Cursor and Grok Build join Claude Code and Codex as supported hosts, and memory
search stops answering with a bare zero — a query that matches nothing now says
which of its terms were the problem. The release pipeline gains the two guards
that would have caught this release's own predecessors: every version pin on the
release surface is now registered and reverse-scanned, and a tag push that gets
only one of its two approvals fails loudly instead of shipping an unresolvable
plugin pin. MCP tool count: 44 (unchanged). Test suite: 1,491 passing.
ADR-047, ADR-048, ADR-049, ADR-050.

### Added

- **Cursor and Grok Build are supported hosts (PMSERV-165)**: `--target` /
  `target=` now accept `cursor` and `grok` alongside `claude-code` and `codex`.
  Cursor registers into `~/.cursor/mcp.json` (`{"mcpServers": …}`, with the
  `"type": "stdio"` field Cursor documents as required); Grok Build registers
  into `~/.grok/config.toml`, whose `[mcp_servers.pmlens]` shape is identical to
  Codex's, so one implementation serves both. Both read `AGENTS.md`.

  Host identity moved out of six unsynchronised literals and two `if/elif`
  dispatch chains into a single registry, `pmlens.hosts.HOSTS`. Those chains had
  no `else`: a host wired into some places and not others produced zero results,
  no error, and an `overall_status` of `"skipped"` — a half-added host that read
  as success. Dispatch is now table-driven and an unwired host fails loudly.

  Three hosts share `AGENTS.md`, so rule injection deduplicates by **file**
  rather than host: `target="all"` writes it once and reports
  `results[].hosts == ["codex", "cursor", "grok"]`. Iterating hosts would have
  backed up and rewritten one file three times, with the second and third
  reporting "already current" because the first had just written it.

  **Grok Build caveats, both verified against the docs Grok ships locally.**
  It already reads Claude Code's and Cursor's MCP configs as compatibility
  sources, so a Claude-Code install often works in Grok with no action —
  registering natively matters because that scan is opt-out and merged at lower
  priority. And it loads *every* recognised rule file in a directory, so a
  project managed for both Claude Code and Codex feeds it the PM rules twice;
  `pm_status` and `pm_update_rules` report that in `warnings[]` rather than
  deleting a file that another host requires.

- **Release version-lockstep guard covers the whole release surface
  (PMSERV-172)**: `tests/test_version_lockstep.py` replaces the seven scattered
  lockstep assertions with one registry plus a scoped reverse scan. Eight live
  values had no guard (`uv.lock`, both cheatsheets, `docs/README.md`, three
  stamps in `docs/architecture.html`, and `plugin/README.md`'s bare
  `latest 0.13.0` — which sat *inside* a file the guards already covered, since
  the regex was anchored on `pm-server@`). Text surfaces now assert an exact pin
  *count*, and the reverse layer flags any version-shaped string in a
  release-surface file that is neither the current version nor a suppression
  carrying a written reason.

### Changed

- **Rule template v11 → v12 (PMSERV-165)**: self-references no longer name
  `CLAUDE.md` (three hosts receive the same text inside `AGENTS.md`), and the
  ADR-028 branch-continuity clause is corrected — it said "hosts without hooks"
  when Cursor and Grok Build both *have* session hooks; what they lack is a
  pmlens-installed one. Existing `CLAUDE.md` / `AGENTS.md` files are re-injected
  on the next `pm_update_rules`.
- **`pm_status` gained a `warnings[]` key**, and `pm_update_rules` results gained
  a `hosts` field naming every host that reads the written file. `host` still
  carries the single owning host.
- **Memory search: the LIKE fallback now AND-s the query's terms, and an empty
  result explains itself (PMSERV-175)**. The fallback used to match the *entire*
  query as one literal substring — never splitting terms, never AND-ing them —
  so a multi-word query hit only when those words appeared verbatim and adjacent
  in stored text. Since the fallback exists to catch what `unicode61` misses on
  CJK, and what it misses is precisely compound/multi-word queries, the safety
  net was widest where it was needed least. `egress ホスティング形態` returned 0
  rows even though both terms lived in the same memory, stored as
  `…ホスティング形態でegress宣言…`; it now returns that row.

  `pm_recall(query=…)` and `pm_memory_search` gained additive keys
  `fallback_reason` (`fts_no_match` | `global_index_absent` | `fts_error`),
  `matched_terms`, `unmatched_terms`, and `dropped_terms`. They appear only when
  they carry information, so an FTS hit keeps its previous response shape. On a
  0-result response the keys separate the two causes that used to look
  identical: a non-empty `unmatched_terms` names terms that exist nowhere (drop
  them and retry), while an empty one means every term exists but no single
  memory holds them all (search fewer terms). That ambiguity was not
  hypothetical — a caller hit it, concluded the search path was broken, read
  `.pm/memory.db` with sqlite3 directly, and recorded that unverified
  conclusion as a memory.

  `search_ex()` and `search_global_ex()` keep their two-tuple signature and are
  now thin delegations to new `search_full()` / `search_global_full()`, which
  return a `SearchDiagnostics`. Per-project and cross-project share one
  `_like_fallback()` implementation so the two indexes cannot degrade
  differently. Term splitting reuses the MATCH path's own token regex, so
  `matched_terms` always describes the split FTS actually attempted. Queries
  longer than 12 terms are capped, with the remainder reported in
  `dropped_terms` rather than silently discarded.

  This is not the trigram migration (still PMSERV-150): that would raise the
  FTS-only recall, whereas this raises the fallback's. Measured golden-corpus
  recall moved from 19/23 to 20/24 combined, with FTS-only unchanged at 14 —
  see `docs/reports/ja-fts-baseline.md`.

### Fixed

- **A tag push no longer half-releases in silence (PMSERV-173)**: `v*` fires two
  workflows, each with its own manual approval, and approving only one shipped a
  version whose committed `uvx pm-server@X.Y.Z` plugin pin could not resolve —
  with no job red anywhere, since `skip-existing: true` means a green publish job
  is not evidence anything was uploaded. Approving the *wrapper* first was worse:
  it publishes a metapackage requiring a `pmlens` that may never exist, and PyPI
  never allows re-uploading a filename. Both orderings now fail loudly via
  reciprocal PyPI presence gates, the two approvals are named "approval 1 of 2" /
  "2 of 2", and `docs/RELEASING.md` documents the procedure that was previously
  recorded nowhere.
- **The test suite can no longer edit the developer's real editor configs**: it
  runs with `$HOME` pointed at a sandbox — set in `pytest_configure`, before
  collection, and narrowed per test — plus detection guards at both session and
  function scope that fail if a real host config is disturbed or a pmlens backup
  appears beside one. Found the hard way — an installer refactor resolved its
  config path around the seam the fixtures patched and a plain `pytest` wrote
  into the real `~/.grok` and `~/.cursor`.

## [0.13.0] - 2026-07-25

Auto-memory becomes searchable across projects, and two irreversible memory
operations trade after-the-fact warnings for pre-flight gates. This release
also fixes a long-standing bug that made *every* cross-project search return
empty for read-only viewers. MCP tool count: 43 → 44. Test suite: 1,383
passing. ADR-044, ADR-045, ADR-046.

### Added

- **Physical auto-memory ingest for cross-project search (PMSERV-156,
  ADR-045/046)**: a new `pm_memory_ingest` tool indexes Claude Code's
  auto-memory notes (`~/.claude/projects/<repo>/memory/*.md`) into the global
  cross-project index. Until now those notes were reachable only through
  `pm_recall(include_auto_memory=true)` for the *current* project — the
  `cross_project=true` branch returns before any overlay runs, so the flag was
  silently inert there and the notes were structurally invisible to
  cross-project search (measured on a real corpus: 850 distinct tokens present
  in 150 notes across 20 projects and absent from the ledger).

  The safety boundary is a **fact-based gate**, not the `scope` parameter:
  a real run whose collected notes reach beyond this project's own
  auto-memory directories — whether through `scope="all"` or an
  `auto_memory_path` override — is refused with `blocked=true` and nothing
  written, because it would publish other projects' notes (private ones
  included) into a shared index. `force=true` is the explicit override and a
  dry-run predicts the refusal via `would_block`; the blocked and forced
  warning codes are mutually exclusive. Judging the parameter instead of the
  outcome left the override as an unguarded bypass, so the check moved to
  what was actually collected. `purge=true` is the undo — never gated, since
  the gate's own remediation points at it — and it reports which projects it
  removed rows for.

  Rows land ONLY in the derived `memory_index`, never in a project ledger, so
  `pm_remember` stays the source of truth (PMSERV-111's no-dual-write rule)
  and the `.md` files stay the source of truth for auto-memory. Re-ingest is
  idempotent by content hash and implemented as DELETE-then-INSERT: the
  global FTS5 table is external-content with only after-insert and
  after-delete triggers, so an `UPDATE` would leave the previous text
  searchable and the new text missing, silently. Pruning of vanished notes is
  scoped to the directories a call scanned *successfully* — directory
  listing uses `os.listdir` because `pathlib.glob()` swallows
  `PermissionError` and returns empty, which made an unreadable directory
  look like a successfully-scanned empty one and pruned every row beneath it.
  Scan paths are resolved on both sides of every comparison so a symlinked
  project root no longer re-ingests every note as new on each run, and a repo
  whose encoding drift gave it several store directories dedups to one row
  per note — identity comes from the repo's registered root, so under
  `scope="all"` this holds for repos present in `~/.pm/registry.yaml`. `memory_index` gains `source` / `source_path` / `content_hash`
  via backward-compatible `ADD COLUMN` (existing rows default to
  `source='pm'`), and cross-project results now carry that provenance —
  `source_path` holds the absolute path, deliberately not reusing the
  overlay's basename-valued `source_file` key. Index timestamps are written
  in the ledger's UTC space-separated format (a local `T`-separated value
  sorted after every ledger row in the fallback's `ORDER BY created_at`).
  `dry_run` is a pure preview of the index write: it creates no global
  index and migrates no schema. (Opening any memory tool still
  materialises the project's own `.pm/memory.db` if absent — unchanged
  from previous releases.)

### Changed

- **Session-summary pruning now gates on the ambiguity window instead of
  reporting after the fact (PMSERV-163, ADR-044)**: `pm_memory_cleanup(
  summaries_keep_latest=N, dry_run=False)` refuses to run when its delete set
  reaches into the recent ambiguity window — the rows whose removal disables
  `pm_recall`'s concurrent-session detection. The call returns
  `summaries.blocked=true` with `deleted=0` (all-or-nothing: the delete set is
  one predicate), reports `recent_blocking` / `blocked_would_delete`, and
  raises a `summaries_prune_blocked_recent` warning; a dry-run predicts the
  refusal via `summaries.would_block`. The previous `summaries_pruned_recent`
  warning was post-hoc — it arrived only after a concurrent session's context
  had already been destroyed. The new `summaries_force=true` parameter
  executes the prune anyway and keeps the original post-hoc warning (the two
  warning codes are mutually exclusive). The gate is evaluated on the same
  env-configurable window as the count, inside the same `BEGIN IMMEDIATE`
  transaction as the DELETE it guards, so a concurrent save cannot slip
  between the check and the write.

### Fixed

- **`pm_recall(include_auto_memory=true)` found no notes under a symlinked
  project root (PMSERV-156)**: the locator encoded only the *resolved*
  project path, but Claude Code names its `~/.claude/projects/<repo>` store
  from the path as IT sees it — which may be the unresolved spelling
  (`/var` vs `/private/var`, a symlinked HOME, a container mount). The two
  spellings encode to different directory names, so the lookup matched
  nothing and the overlay returned empty while reporting success. Both
  spellings are now offered as candidates; a name that exists on neither is
  simply never found on disk, so the extra candidate is harmless.

- **Cross-project search returned nothing under `PM_LENS=1` (ADR-046)**: a
  read-only store nulled its global index path outright, so
  `pm_recall(cross_project=true)` and `pm_memory_search(cross_project=true)`
  always returned `[]` for Claude Desktop/Cowork viewers even though both
  tools are on the read-only allowlist. The guard was meant to stop writes,
  not reads. The path is now retained and opened with
  `mode=ro&immutable=1` — which creates no `-wal`/`-shm` sidecars and so
  keeps the RO invariant intact (ADR-028) — while global writes are refused
  by the store itself as defense-in-depth on top of the tool never being
  registered under Lens.

- **`pm_memory_cleanup(keep_latest=...)` floor asymmetry (PMSERV-164,
  ADR-044)**: the memories path accepted values the summaries path had
  rejected since PMSERV-162, and both out-of-range values were traps rather
  than useful inputs — `0` produced `id NOT IN (SELECT ... LIMIT 0)`, an
  empty keep-set matching every row, so a single typo wiped the entire memory
  ledger, while a negative value is read by SQLite as "no limit" and silently
  deleted nothing while reporting a prune. `keep_latest < 1` is now rejected
  without deleting, including in dry-run and when combined with other (ANDed)
  criteria.

## [0.12.1] - 2026-07-17

### Added

- **Desktop outbox read surface (ADR-039 T1-T3, PMSERV-142/145/146)**:
  `pm_outbox_pending` is now read-pure — it opens `~/.pm/desktop/desktop.db`
  with `readonly=True` and never creates it as a side effect — and is
  reachable from both the Lens viewer (`PM_LENS=1`) and the Desktop outbox
  host (`PM_LENS=1` + `PM_DESKTOP_WRITE=1`) without needing write access,
  gaining `pending_total`, `filter_since`, and a host-aware `note`.
  `pm_recall(include_outbox=true)` overlays unmerged Desktop outbox entries
  (`outbox_entries[]` + `outbox_summary{pending_total, project_pending,
  unscoped_pending, scope}`) onto normal recall context, with a lighter
  `outbox_pending_count` / `outbox_note` nudge on ordinary calls when Lens
  has unmerged entries waiting. The deliberate matching asymmetry between
  this overlay's resolve()-based `scope="project"` filter and
  `pm_outbox_pending`'s raw-string `filter_project` is documented
  (PMSERV-146, AD-5).
- **Japanese FTS baseline for cross-project search (PMSERV-143, ADR-039
  T5)**: `search_global`'s SQLite FTS5 index now has a baseline configuration
  validated against Japanese content, with a `LIKE` fallback path for misses.
- **Auto-memory bridge v1 (PMSERV-112, ADR-040)**: a new `auto_memory` module
  bridges Claude Code's native auto-memory store
  (`~/.claude/projects/<repo>/memory/*.md`) and the PM ledger without risking
  split-brain. `pm_recall(include_auto_memory=true)` overlays those notes at
  query time as an additive `auto_memory_entries[]` / `auto_memory_summary{}`
  key (each tagged `source="auto_memory"`, carrying the raw origin type and
  `originSessionId` provenance) — read-only, nothing is copied into
  `memory.db` (dual-write-safe, PMSERV-111), so it is safe under `PM_LENS=1`.
  `pm_remember(bridge_to_memory_md=true)` (opt-in, default OFF, `PM_LENS=0`
  only) appends an idempotent pointer block into `MEMORY.md` so the
  always-in-context index points back at the ledger. The overlay always
  excludes `MEMORY.md` and the reverse bridge writes only `MEMORY.md`, so the
  two directions are disjoint and an ingest loop is structurally impossible.
  Directory resolution enumerates-and-matches the drifting `~/.claude/projects`
  encoding, with an explicit `auto_memory_path` override. Physical
  cross-project ingest + Lens cross-project search are deferred to PMSERV-156.
- **Recency-read expression indexes + session-summary pruning (PMSERV-162,
  ADR-043)**: two expression indexes matching the effective-timestamp order
  (`(COALESCE(...) DESC, id DESC)` and its branch-prefixed sibling) now
  supply the recency reads directly instead of a full SCAN + TEMP B-TREE
  sort. Created in the RW migration path only (never on read-only Lens
  opens, ADR-028); they add no new writer requirement (the store already
  needs SQLite >= 3.24 for UPSERT and creates FTS5, which shipped with
  expression indexes in 3.9), but once created the DB file requires
  SQLite >= 3.9 of any reader. `session_summaries` — the one table that
  was never DELETEd — can now be pruned:
  `pm_memory_cleanup(summaries_keep_latest=N)` keeps the newest N rows by
  the same effective-timestamp order (never `MAX(id)`, which would
  reintroduce the PMSERV-158 bug class on the pruning path) and always
  additionally protects the newest row per branch group (`NULL`/`''` as
  one pseudo-group) so branch-aware recall and `tracks.yaml` glob
  resolution keep every line's last context — including on non-git
  projects. The delete set is one SQL predicate evaluated atomically with
  its counts in a `BEGIN IMMEDIATE` transaction (an adversarial review
  reproduced a concurrent save being deleted by the snapshot-then-DELETE
  draft, and a `too many SQL variables` abort on variable-limit-999
  builds). `N >= 1` is enforced, deletions inside the ambiguity window
  (the same env-configurable width pm_recall's detection uses) surface a
  `summaries_pruned_recent` warning, and results nest under a `summaries`
  key (memories results stay top-level; the one shape change is that a
  zero-match non-dry-run now honestly reports `{"deleted": 0, "dry_run":
  false}` instead of the legacy `{"would_delete": 0, "dry_run": true}`).
- **Prompt Pack v2 — HTML output (PMSERV-155, ADR-041)**: `pm_prompt_pack`
  gains `format="html"` — a single, CDN-free (CSP-safe) file with a lane
  progress diagram (priority + `suggested_model` chips, `blocked_by` hard-dep
  and `after_recommended` soft-dep notes) and a copy button per prompt card —
  plus a `group_by` (`none`/`phase`/`track`) parameter. New optional task
  fields `suggested_model` (opus/sonnet/haiku/any), `after_recommended`, and
  `track`, and project fields `discipline` and `verify_commands`, are all
  defaulted so existing YAML loads unchanged and generation stays read-only.
  The paste-ready prompt body is generated once and shared by the markdown and
  HTML formats, and the HTML template is overridable per project via
  `.pm/prompt-templates/prompt_pack.html`. All task-derived text is Jinja2
  autoescaped and the copy button reads the `<pre>` text content, so the output
  is XSS-safe by construction. CLI + review-workflow integration remain v3.
- **Prompt Pack v3 — CLI + workflow handoff (PMSERV-157)**: a new
  `pmlens prompt-pack` CLI subcommand (`--tag`/`--phase`/`--priority`/
  `--task-id`/`--format`/`--group-by`/`--out`/`--project`) generates a pack
  outside an MCP session, and the development workflow's Task Breakdown step now
  hints prompt-pack generation for a "1 task = 1 session" handoff. The MCP tool
  and the CLI share one orchestration (`prompt_pack.run_prompt_pack`) so they
  cannot drift. Linked memories are now read through a read-only
  (`mode=ro&immutable=1`) connection gated on schema presence, so generating a
  pack never migrates `memory.db` or leaves WAL sidecars — a strict improvement
  to the read-path purity even over the v1/v2 tool.

### Fixed

- **Branch-aware recall returned a stale summary on migrated DBs
  (PMSERV-158, ADR-028)**: the ALTER-added `session_summaries.updated_at`
  column has no default, so single-saved rows were left `NULL` and every
  read that ordered or filtered on the raw column silently skipped them —
  `pm_recall(track=...)` returned an older re-saved summary as the branch
  "latest" and ambiguity detection dropped recent sessions. Fixed with a
  three-layer defense: the INSERT now always writes `updated_at`
  explicitly, the open-time migration backfills `NULL`/empty values from
  `created_at` idempotently on every RW open, and all recency reads order
  by the effective timestamp `COALESCE(NULLIF(updated_at, ''),
  created_at)` (`_ts_expr`), which also degrades to `created_at` when the
  column is absent (pre-migration DB under read-only Lens).
- **"Latest" semantics unified across all recency reads (PMSERV-159/160,
  ADR-042)**: the no-track reads (`pm_recall` default path, track-miss
  fallback, `pm_session_summary` get/list, context-inject Layer-1) ordered
  by bare `id DESC` — "most recently *started*" — which diverges from
  "most recently *worked*" under the id-preserving UPSERT. All recency
  reads now order by `_ts_expr DESC, id DESC` (ties keep the legacy order).
  Because real-time ordering loses the id order's implicit self-healing
  against forward clock skew, a future `updated_at` (NTP skew, restored VM
  snapshot) is now clamped to now by an idempotent open-time heal
  (PMSERV-160) so the very next save wins again.
- **Same-second saves no longer degrade to insertion order (PMSERV-161,
  ADR-043)**: `session_summaries` timestamps are written with millisecond
  precision (`strftime('%Y-%m-%d %H:%M:%f','now')`); second-precision
  `datetime('now')` made saves within one wall-clock second tie on the
  effective timestamp and fall back to the id tiebreak. The PMSERV-160
  future-clamp compares and writes at the same precision (a
  second-precision comparator would spuriously clamp legitimate ms rows
  healed within their own second), and `list_summaries_within` gained the
  `, id DESC` tiebreak the other recency reads already had. Legacy
  second-precision values order correctly against new ms values
  lexicographically, so no data migration is needed. Known accepted
  residual while an old binary shares the DB: its second-precision saves
  and clamp can invert/flatten ordering within a single second
  (self-healing, gone once the old binary is redeployed).
- **`pm_outbox_merge` implicit `.pm/memory.db` creation defect (PMSERV-147,
  ADR-039 T4)**: merging a pending outbox entry whose resolved project
  (`target_project` or the row's `source_project`) had not been `pm_init`'d
  used to reach code paths that could create `.pm/memory.db` as a side
  effect of a failed merge. `pm_outbox_merge` now runs an
  `is_initialized_project` pre-flight guard before touching any project
  store, skipping the id with a `warnings[]` entry
  (`reason="unregistered_project"`) instead — it never auto-initializes a
  project. `pm_outbox_remember`/`pm_outbox_log` gained matching
  `unregistered_project` guidance in their own `warnings[]`, and
  `pm_outbox_pending` gained an additive `unregistered_projects[]` list so a
  caller can see at a glance which pending entries still need `pm_init`
  before they can be merged.

### Documentation

- **Repo tidy-up after the 0.12.0 rename (PMSERV-141)** — synced `README.ja.md`
  with `README.md`: added the missing "pm-server からの移行" section and fixed a
  stale `pip install pm-server` → `pip install -U pmlens` in the pm-agent
  migration block (the EN README had gained the pm-server migration section,
  leaving the JA side a section behind). Archived the one-time
  `docs/memory-layer-prompt.md` (Phase-1 implementation prompt) under
  `docs/archive/` and relocated its docs-index entry to a new アーカイブ
  subsection. Working-tree-only housekeeping (regenerable caches, stale `dist/`
  artifacts trimmed to `pmlens-0.12.0.*`, old rules backups) was pruned
  alongside but is not part of the tracked history.

## [0.12.0] - 2026-06-29

### Changed

- **Phase-3 identity rename `pm_server` → `pmlens` (PMSERV-137, ADR-034)** — the
  load-bearing identifier flip, built on a reversible step ladder and shipping
  (at 0.12.0) with a user-facing re-registration runbook. Done so far on
  `feat/pmserv-137-phase3-rename` (unpublished): the import package renamed
  `pm_server` → `pmlens` with a `sys.modules` alias shim (so `import pm_server` /
  `python -m pm_server` keep working); the live MCP identity flipped
  (`FastMCP("pmlens")`, the 14 MCP registration-key sites, the `.mcpb` manifest
  `name`, the plugin `.mcp.json` key); a `pmlens migrate-from-pm-server` updater
  (Claude Code + Codex re-key, additive `mcp__pm-server__*` → `mcp__pmlens__*`
  permission rewrite) with a read-only cutover awareness banner; and
  dual-recognition in the hooks + plugin shell scripts so a mid-rename user is
  never stranded.
- The binary name (`pm-server` console script), the migrate machinery's legacy
  key, and the CLAUDE.md marker slug (`pm-server:begin`) deliberately stay
  `pm-server` (backward compatibility / the ADR-032 marker invariant).
- **Step 7 (Phase A — reversible)** lands the rest on-branch ahead of the gated
  0.12.0 publish: `prog_name` → `pmlens`; identity prose refreshed across
  `README*` / `docs/*` / cheatsheets / skills (3-term rule — `pm-server` = the
  retained install handle, `pmlens` = the body identity, "PM Lens" = display
  name); the version bumped `0.11.0` → `0.12.0` across every drift-guarded
  surface; and the install/migrate **binary resolver now prefers the `pmlens`
  binary** (`shutil.which("pmlens") or shutil.which("pm-server")`) so the
  migrated `pmlens` registration execs the pipx-installed `pmlens` at cutover,
  with `pm-server` kept as the mid-flight fallback. The `plugin.json` /
  `marketplace.json` plugin **name** deliberately stays `pm-server` (the
  `/plugin install pm-server@flc-design` handle) until cutover, so existing
  installs are not stranded.

### Added

- **Containerized development environment (PMSERV-140, ADR-036)** — `.devcontainer/`
  + a `Makefile` (`make dev-build/test/lint/shell/sandbox/clean`) run development
  inside a Docker container with a disposable HOME, so pmlens's installer/hooks/
  migrate code (which mutates `~/.claude`, `~/.codex`, `~/.pm`) can be exercised
  without touching the host. A `Docker Development` workflow template and a
  `docker-dev` skill guide the flow. The host keeps a stable pip/pipx `pmlens` as
  its "tool"; the container is the "development" half.

### Notes

- **Non-breaking until migrate:** flipping the FastMCP name does not change the
  tool namespace Claude Code shows — that is keyed off the registration key,
  which stays `pm-server` in a user's config until they run
  `pmlens migrate-from-pm-server`. The `mcp__pm-server__*` → `mcp__pmlens__*`
  flip happens on migrate, not on upgrade.
- The `0.12.0` publish that makes this reach users is gated and tracked in
  `docs/MIGRATION.md` (Phase-3, step 7).

## [0.11.0] - 2026-06-22

The **PM Lens rebrand**, phases 1–2: the product is now "PM Lens" and its PyPI
distribution is **`pmlens`**, with `pm-server` retained as a thin compatibility
wrapper. The load-bearing identifiers (Python import name, MCP registration key,
FastMCP name, marker slug) are intentionally unchanged here and flip later in
Phase-3.

### Changed

- **Phase-1 — display layer (PMSERV-134)**: product display name "PM Server" →
  "PM Lens" across README/docs and the `.mcpb`/plugin display surfaces; the
  GitHub repository renamed to `flc-design/pmlens`; all GitHub/PyPI URLs updated.
- **Phase-2 — distribution rename (PMSERV-136, ADR-031/032)**: the PyPI
  distribution renamed `pm-server` → **`pmlens`**; `pm-server` becomes a
  zero-module metapackage depending on `pmlens` (so `pip install pm-server` /
  `uvx pm-server` keep resolving via the dependency). In-code display strings
  flipped to "PM Lens" (~87 sites). Version bumped `0.10.0` → `0.11.0` across
  every drift-guarded surface (pyproject, manifest, `plugin.json`, marketplace,
  the plugin uvx pin, README pins). The Python import name stays `pm_server` and
  the MCP registration key stays `pm-server` (both deferred to Phase-3).

### Security

- **Patched vulnerable transitive dependencies (PMSERV-135)**: bumped flagged
  transitive deps to their fixed versions.

## [0.10.0] - 2026-06-10

This release ships three pillars on top of v0.9.0. **Branch-aware session continuity** (ADR-028/035) lets `pm_recall(track=...)` restore the last context of a specific work line — a raw branch or a logical label defined in `.pm/tracks.yaml` — while the read path stays completely git-free, with the PM_LENS read-only invariant upgraded from convention to a statically proved property. The **.pm → X content pipeline** (ADR-024) adds a per-project staging store and four new MCP tools for build-in-public drafts, with a deterministic Layer-1 redaction prefilter as the shipping safety floor — the server holds no X credentials and structurally cannot post. And a **Claude Code plugin layer** (ADR-026/027) packages pm-server for `/plugin install pm-server@flc-design` with bundled MCP, SessionStart/PostToolUse hooks, and a skill — the host-agnostic MCP core is unchanged. The shared rules template advances v8 → v10, and the storage/installer/discover hardening backlog is cleared. MCP tool count: 38 → 42. Test suite: 1,014 passing.

### Added

- **`pm_recall(track=)` branch-aware session continuity (PMSERV-124, ADR-028)**: per-work-line recall with overall-latest fallback (`track_matched` flag). Branch detection runs on the write path only (`pm_session_summary` text-parses `.git/HEAD`); pm-server never shells out to git, and the read path receives the branch as an argument.
- **Logical track labels via `.pm/tracks.yaml` (PMSERV-125, ADR-035)**: a label maps to branch globs and resolves at query time (rename-resistant); responses report `track_branch`, and a malformed config degrades to raw-branch matching with a `tracks_config_invalid` warning.
- **X content pipeline — staging store + 4 MCP tools (PMSERV-114, PMSERV-116, ADR-024)**: per-project `.pm/x_drafts.db` and `pm_draft_x` / `pm_redact_draft` / `pm_reject_draft` / `pm_x_drafts_pending`. The review queue never returns `raw_content`; all four tools are hidden under `PM_LENS`. Tool count 38 → 42.
- **Layer-1 deterministic redaction prefilter + catalog v2 (PMSERV-115, PMSERV-121)**: scrubs AWS/GitHub/Stripe/Slack tokens, JWTs, private keys, connection strings, plus (v2) Azure keys, GCP service accounts, bearer tokens, IPs, and phone numbers. Count-only reports (never cleartext); internal IDs stay visible by default (`scrub_internal_ids` opt-in); `.pm/redaction.yaml` allow/deny overrides.
- **`content-pipeline` builtin workflow template + `pm_status.x_drafts_pending` diagnostic (PMSERV-117, PMSERV-118)**: extract → draft → redact → review; the diagnostic probes only when the DB already exists. Builtin templates: 4 → 5.
- **`pm_draft_x` debounce + golden-fixture regression (PMSERV-121)**: same-session draft bursts collapse to one proposal within a 10-minute window (`force=true` to bypass); a golden fixture pins the end-to-end pipeline artifact.
- **Claude Code plugin layer (PMSERV-123, ADR-026/027)**: `plugin/` with bundled MCP (`uvx pm-server`, no prior pip install), directive-only SessionStart hook with per-session double-fire guard, PostToolUse commit-reminder parity hook (defers when a manual install is detected), `pm` skill, and a root `marketplace.json` publish catalog. Collision guard decision: bundle + warn + documented migration.
- **Rules template v8 → v10**: memory-layer routing — pm_remember as SSoT vs auto memory, no dual-write (v8, PMSERV-111); X content pipeline propose-don't-force rule (v9, PMSERV-119); branch-aware re-derive rule for hook-less hosts (v10, PMSERV-125).
- **CLAUDE.md backup symmetry (PMSERV-058)**: every existing rule file gets a timestamped `.bak` before overwrite, retiring the AGENTS.md-only asymmetry.
- **`pm_discover` depth-cap exclusion warning (PMSERV-089)**: directories dropped beyond the depth-5 cap are reported with sample paths + remediation instead of vanishing silently (MCP + CLI parity).

### Changed

- **Storage `save_*` helpers privatized to `_save_*` (PMSERV-067)**: the supported write API is the transactional mutators; the three sanctioned in-layer bypass sites are documented in the module docstring.
- **Installer cleanup (PMSERV-054, PMSERV-055)**: `install_mcp` / `uninstall_mcp` wrappers now emit `DeprecationWarning` (removal target v1.0.0); `InstallResult.status` is `Literal`-typed.
- **No-op rule injection reports `skipped`, not `updated` (PMSERV-062, PMSERV-110)**: a byte-identical re-injection touches nothing on disk (no spurious backup); `Inject` status fields are `Literal`-typed.
- **README / repo polish (PMSERV-126)**: 42-tool count, PyPI/CI badges, dashboard screenshot, multi-host metadata, Development Status Alpha → Beta; `uv.lock` tracked for reproducible installs (PMSERV-123).

### Fixed

- **`pm_cleanup` registry TOCTOU (PMSERV-069)**: load + validate + save now run inside one `_yaml_transaction`, so a project registered concurrently can no longer be lost (same fix class as PMSERV-066).
- **`PM_LOCK_TIMEOUT_S` env knob (PMSERV-109)**: the lock-acquire timeout is resolvable from the environment (production fail-fast default of 5 s unchanged) — fixes the concurrent-test CI flake at the root and doubles as an ops knob for slow/contended filesystems.
- **`limit=0` pagination guards (PMSERV-121, PMSERV-122)**: `pm_x_drafts_pending` and `pm_outbox_pending` no longer claim `has_more` on a 0-row page, closing an infinite-pagination loop; count-only probes keep working.
- **Plugin SessionStart hook hardening (PMSERV-123)**: jq-less double-fire guard, atomic marker claim via `set -C`, bounded `claude mcp get` collision probe, 30-day marker reaping.
- **Code-review follow-ups**: redaction coverage + correctness blockers (PMSERV-115, PMSERV-116); branch-aware recall findings (PMSERV-125).

### Security

- **`x_drafts.db` gitignored (PMSERV-120)**: the staging DB holds pre-redaction `raw_content` (secret at rest) and must never reach the repo.
- **The X pipeline is structurally non-posting (ADR-024)**: pm-server holds no X credentials and no network path; the redaction report is count-only so it cannot become a second leak vector; posted drafts freeze their `redacted_*` fields via trigger (PMSERV-121).
- **RO-surface static reachability proof (PMSERV-125)**: an AST call-graph test proves the forward closure of every `RO_ALLOWLIST` tool is disjoint from `read_git_branch` and `subprocess`, upgrading the PM_LENS read-only invariant (ADR-028) from string-match guards to a checked property.

## [0.9.0] - 2026-05-26

*(backfilled 2026-06-10)*

- **`brainstorming` builtin workflow template (PMSERV-107)**: 8-step Double Diamond ideation → requirements → spec → ADR, reusing super-research's 3-parallel-agent pattern with a divergent objective; chains to `development`. Builtin templates: 3 → 4.
- **MCPB manifest rewritten to the v0.4 schema** with bundled source for the uv runtime (PMSERV-106), plus a 3-layer version-drift check in the bundle build.
- **Docs**: user-guide, workflow-guide (template 使い分け matrix), and sync-architecture pages added and synced to v0.8.0/0.9.0.

## [0.8.0] - 2026-05-22

*(backfilled 2026-06-10)* — Phase 2 Desktop sync: writes from Desktop land in an **outbox**, never directly in the main store.

- **DesktopOutboxStore + 5 MCP tools (PMSERV-095–098)**: `pm_outbox_remember` / `pm_outbox_log` stage entries under `PM_DESKTOP_WRITE=1`; `pm_outbox_pending` / `pm_outbox_merge` / `pm_outbox_reject` review them from Claude Code.
- **`pm_status`** exposes the outbox pending count + cleanup (PMSERV-099); installer propagates `PM_DESKTOP_WRITE` and the manifest bundle env (PMSERV-100).
- **Lens invariant test (PMSERV-102)**: the main `memory.db` is asserted unchanged under Phase 2 — Desktop writes cannot touch the SSoT.

## [0.7.1] - 2026-05-21

*(backfilled 2026-06-10; covers v0.6.2..v0.7.1 — the untagged 0.7.0 work plus the 0.7.1 hotfix)*

- **PM_LENS read-only mode** for Claude Desktop / Cowork (PMSERV-079): RO tool allowlist, project SQLite opened read-only with `immutable=1` (PMSERV-080), `pm_schema` version stamps (PMSERV-078); the 0.7.1 hotfix adds a schema guard + Lens-fallback note in read tools (PMSERV-093, PMSERV-091).
- **MCPB bundle**: v0.4 manifest + bundle builder + release CI pack (PMSERV-083); env placement aligned with MCPB schema 0.3+ (PMSERV-084).
- **`resolve_project_path`**: MCP roots + registry picker (PMSERV-082); dead roots branch removed (PMSERV-085). **`discover_projects`** walk bounded with depth cap, excluded dirs, and `~/.pm` skip (PMSERV-081).
- **Security**: `.git/config` parsed directly instead of invoking git (PMSERV-077); hooks and installer made Lens-aware, `PM_LENS` propagated to host configs (PMSERV-086, PMSERV-087).

## [0.6.2] - 2026-05-18

This release completes **Phase C** of the KR-008 supply-chain hardening pass (run `pm_recall query="KR-008"` from inside Claude Code). Every GitHub Action in `ci.yml` and `release.yml` is pinned to a full commit SHA, a grouped Dependabot config keeps those pins fresh, and — as a consequence of pinning to the latest majors — the entire CI/release pipeline moves off the deprecated Node 20 runtime ahead of GitHub's 2026-06-02 Node 20 → Node 24 actions deadline. The choice to pin to the latest majors (`actions/upload-artifact` v4 → v7, `actions/download-artifact` v4 → v8) rather than the minimum Node-24 majors — justified by a per-input breaking-change analysis against pm-server's actual usage and an independent cross-check — is recorded as **ADR-013**.

### Security

- **All GitHub Actions pinned to full commit SHAs (PMSERV-074, Phase C, ADR-013)**: `actions/checkout` → `de0fac2e…` (v6.0.2), `actions/setup-python` → `a309ff8b…` (v6.2.0), `actions/upload-artifact` → `043fb46d…` (v7.0.1), `actions/download-artifact` → `3e5f45b2…` (v8.0.1), across both `ci.yml` and `release.yml`. A mutable tag like `@v4` lets the action's owner — or anyone who compromises their account — silently change the code a workflow runs; pinning to an immutable commit SHA removes that mutable-tag attack surface from the pipeline that publishes to PyPI. `pypa/gh-action-pypi-publish` was already SHA-pinned in v0.6.1 and is unchanged. Each pin carries a `# vX.Y.Z` comment so Dependabot bumps both the SHA and the comment in one PR.
- **`download-artifact` v8 fails closed on a digest mismatch (PMSERV-074, side benefit)**: v8 defaults `digest-mismatch` to `error`, so the `publish` job aborts before touching PyPI if the `dist` artifact handed over from the `build` job does not match its recorded SHA-256. For a normal same-run round-trip the digest always matches, so this adds an integrity gate to the OIDC publish path at no false-positive cost — a defense layer complementing v0.6.1's Trusted Publisher migration.
- **Dependabot keeps the SHA pins fresh (PMSERV-074)**: a new `.github/dependabot.yml` watches the `github-actions` ecosystem weekly (Monday 09:00 Asia/Tokyo) and groups every action bump into a single PR. Full-SHA pins silently rot without this — upstream security fixes stop arriving the moment you pin. The grouping trades PR granularity for occasionally bundling a breaking major with safe patches; the file documents that trade-off and the Actions-Runner-version assumption (≥ v2.327.1, satisfied by GitHub-hosted `ubuntu-latest`).

### Changed

- **CI and release pipeline now run on Node 24**: pinning to the latest action majors moves `runs.using` from `node20` to `node24` for every action. No workflow logic changed — pm-server only uses inputs (`persist-credentials`, `python-version`, `cache`, `cache-dependency-path`, `name`, `path`, `if-no-files-found`) whose names, semantics, and defaults are identical between the old and new majors, verified by an independent cross-check of each action's `action.yml` diff.

### Deferred to v0.7.0

- **`dist/` precommit cleanup + `twine upload` discipline (Phase D-1)**, **`chart.js` vendoring with Subresource Integrity on `dashboard_single.html` (Phase D-2)**, and **memory provenance — a server-assigned `source` column on the SQLite memory store (Phase D-3)**, the defense-in-depth against delayed prompt injection through `pm_remember` / `pm_recall`. Significant code surface; unchanged from the v0.6.1 deferral.

## [0.6.1] - 2026-05-13

This release is a **security hardening pass** in response to the May 2026 PyPI supply chain attack wave (Mini Shai-Hulud second wave on 2026-05-11/12, TeamPCP spring campaign, Anthropic MCP "by design" RCE CVE-2026-30623). The two-phase work raises direct-dependency floors to CVE-fixed versions, pins the full transitive tree with hashed locks, and migrates the publish path from a long-lived PyPI API token to GitHub Actions OIDC via PyPI Trusted Publishers behind a maintainer reviewer gate. The risk analysis itself — including the 5-perspective cross-check that downgraded 4 of 5 initially-Critical CVEs to N/A after fact-check — is captured as Knowledge Record **KR-008** (run `pm_recall query="KR-008"` from inside Claude Code).

### Security

- **Direct-dependency floors raised to CVE-fixed versions (PMSERV-070, Phase A)**: `fastmcp >= 3.2.0,<4.0` (CVE-2026-32871 — OpenAPI provider SSRF + path traversal via `urljoin` accepting `../`), `filelock >= 3.20.3` (CVE-2026-22701 — `SoftFileLock` TOCTOU symlink race), `jinja2 >= 3.1.5` (CVE-2024-56326 sandbox breakout via the `format` method + CVE-2025-27516 sandbox breakout via the `attr` filter). The functional exposure of the running venv was N/A in each case — pm-server does not use the OpenAPI provider, `SoftFileLock`, or untrusted Jinja2 templates — but the previous lower bounds allowed a fresh install (`pip install pm-server`) to resolve to a vulnerable version.
- **`requirements.lock` with hash-locked transitive tree (PMSERV-070, Phase A)**: 84 packages pinned with SHA-256 hashes via `pip-compile --generate-hashes --all-extras --allow-unsafe`. `pip install --require-hashes -r requirements.lock` now refuses any package whose contents differ from the recorded hash. This is the structural defense against Mini Shai-Hulud-style attacks where a published version on PyPI is replaced with a malicious build mid-flight: a hash-mismatched install fails before any code runs. `pip-tools` is now a dev dependency to keep this file in sync.
- **Release pipeline migrates to PyPI Trusted Publishers via OIDC (PMSERV-071 + PMSERV-072, Phase B)**: a new `.github/workflows/release.yml` builds on tag push and publishes via `pypa/gh-action-pypi-publish` (full-SHA-pinned to `cef221092ed1bacb1cc03d23a2d87d1d172e277b` for v1.14.0, PyPA-maintainer-signed and GPG-verified). The publish job is OIDC-only — no long-lived `PYPI_API_TOKEN` — and is gated by a GitHub `pypi` environment that requires maintainer approval and restricts deployments to `v*` tags. The build job runs without secrets and with `persist-credentials: false`. PEP 740 attestations are attached automatically. Six layers of defense-in-depth are active: workflow-level least privilege (`contents: read`), build/publish job separation, no credential persist, artifact integrity (`if-no-files-found: error` + `twine check`), full-SHA pin on the publish action, and the environment reviewer-plus-tag-pattern gate.

### Documentation

- **Past-tense correction for v0.6.0 deprecations**: `README.md`, `README.ja.md`, `docs/cheatsheet.md`, `docs/cheatsheet.ja.md`, `docs/design.md`, `src/pm_server/claudemd.py`, and `src/pm_server/__main__.py` (docstring on `update-claudemd`) carried a "slated for `DeprecationWarning` in v0.6.0" framing that became past tense the moment v0.6.0 shipped. Rephrased as "since v0.6.0 / v0.6.0 以降" so first-time readers don't misread the deprecation as still pending.
- **`CLAUDE.md` backup symmetry drift (PMSERV-058)**: `README.md`, `README.ja.md`, `docs/design.md`, and `src/pm_server/rules.py` all promised the AGENTS.md/CLAUDE.md backup symmetry would land in v0.6.0. It did not — PMSERV-058 is still in `todo`. The text now reads "tracked in PMSERV-058 (originally targeted for v0.6.0, currently deferred)" so the docs don't advertise behaviour the code doesn't yet have.

### Deferred to v0.6.2 / v0.7.0

- **Remaining GitHub Actions on tag-pin (Phase C)**: `actions/checkout@v4`, `actions/setup-python@v5`, `actions/upload-artifact@v4`, `actions/download-artifact@v4` in both `ci.yml` and `release.yml` are still tag-pinned rather than full-SHA-pinned. A Dependabot configuration to keep SHA pins fresh is also pending. Tracked as the next hardening pass.
- **`dist/` precommit cleanup + `twine upload` discipline (Phase D-1)**, **`chart.js` vendoring with Subresource Integrity on `dashboard_single.html` (Phase D-2)**, and **memory provenance — a server-assigned `source` column on the SQLite memory store (Phase D-3)**. The memory-provenance work is the defense-in-depth against delayed prompt injection through `pm_remember` / `pm_recall` — capturing whether a memory entry originated from `user_typed`, `tool_imported`, `web_search`, or `mcp_inbound` so recall can default to user-origin and require an `--include-untrusted` flag to surface the rest. Significant code surface; tracked for v0.7.0.

## [0.6.0] - 2026-05-13

This release closes the **Concurrent Sessions & Data Integrity** track (phase-9). All known lost-update and TOCTOU windows on `~/.pm/registry.yaml` and `.pm/tasks.yaml` are now wrapped in a single `_yaml_transaction`. Memory storage moves to SQLite WAL so reader and writer no longer block each other. CI runs on every push (Python 3.11/3.12/3.13). Two new `pm_status.diagnostics` entries surface stale module-state that previously failed silently.

### Added
- **YAML atomic write + per-file lock (PMSERV-048, ADR-011)**: every `_save_yaml` now writes via `tempfile.mkstemp` + `os.replace`, and reads/writes that compose into a logical transaction are wrapped in `_yaml_transaction(pm_path, "<basename>")`. The transaction is intentionally non-reentrant — a second acquire from the same process deadlocks — so callers must compose, not nest. See the `storage.py` module docstring for the compound-op discipline.
- **SQLite WAL mode for memory (PMSERV-047)**: `MemoryStore` opens `.pm/memory.db` with `PRAGMA journal_mode=WAL` and `busy_timeout=5000`, removing the reader/writer block that previously surfaced under concurrent `pm_recall` / `pm_remember`.
- **`pm_workflow_abandon` MCP tool (PMSERV-052)**: surfaces the existing `WorkflowStatus.ABANDONED` value as a first-class tool, with `reason` and `notes` parameters. MCP tool count: 31 → 32.
- **`pm_status.diagnostics.utils_fingerprint` (PMSERV-060)**: pm_status reports the SHA-256 of `utils.py` as loaded into the running MCP process vs. the file on disk. A `stale: true` flag indicates the MCP server has not picked up newer edits — diagnosing the same stale-import class as PMSERV-068 below, but for `utils.py` constants.
- **`pm_status.diagnostics.builtin_templates_dir` + `pm_workflow_templates.warnings` (PMSERV-068)**: surface the 2026-05-08 incident where `BUILTIN_TEMPLATES_DIR` was resolved at import time and silently invalidated when the wheel was uninstalled (`pip install -e .`). Both the standing diagnostic and an action-time warning code `builtin_templates_dir_missing` are emitted; CLAUDE/AGENTS rules now require relaying `warnings[]` verbatim.
- **GitHub Actions CI (PMSERV-056)**: `.github/workflows/ci.yml` runs `ruff check`, `ruff format --check`, and `pytest --cov` on every push and PR to `main`, on Python 3.11 / 3.12 / 3.13. Two `tests/test_smoke.py` cases cover `pm-server install --dry-run` for both Claude Code and Codex targets so a packaging-time regression cannot land green.
- **`pytest-cov` + branch-coverage configuration (PMSERV-053)**: `pyproject.toml` now declares `pytest-cov` as a dev dependency and configures `[tool.coverage.run] branch = true` with sensible `omit` patterns.

### Fixed
- **`pm_add_issue` compound TOCTOU (PMSERV-065, ADR-012)**: the read-modify-write that creates a child issue and conditionally reverts a `done` parent to `review` now runs under a single `_yaml_transaction(pm_path, "tasks.yaml")`. This closes three race windows in one move: the `add_task` ↔ `update_task` gap (R1), the initial `load_tasks` ↔ parent-deletion race (R2), and the `next_task_number` ↔ append collision (R3). A new `_next_task_number_from_list` pure helper computes the next id from the in-lock task list.
- **`pm_discover` batched register + lock-free snapshot TOCTOU (PMSERV-066)**: replaces the `for proj in found: register_project(...)` loop with one `_yaml_transaction(GLOBAL_PM_DIR, "registry")` that does a fresh `load_registry()` inside the lock, appends every new entry, and writes `registry.yaml` once. The previous implementation snapshotted the registry lock-free at the top of the function and would silently lose entries against a concurrent registration. `__main__.py`'s `discover` CLI now uses the same path so MCP and CLI no longer differ in locking semantics.
- **`test_migrate_from_pm_agent` host-PATH leak (PMSERV-056 followup)**: the migration smoke test no longer inherits the running developer's `PATH`, so a locally-installed `pm-agent` in the runner cannot mask a regression.

### Changed
- **README front-page repositioning (ADR-010 Now action)**: multi-host neutrality (Claude Code + Codex) is the lead sell; the concurrent-session caveat is removed (PMSERV-050) now that PMSERV-047/048 close the underlying race.
- **README scope-and-house clarification (WF-012)**: project display name updated and a trademark notice added to disambiguate from third-party PM systems.

### Concurrent-Session Coverage Summary (phase-9)
9 of 10 phase-9 tasks complete (90%). Remaining task `PMSERV-067` (rename `save_*` helpers to `_save_*` + `__all__` + DeprecationWarning alias) is intentionally deferred to v0.6.1 / v0.7.0 — see KR-007 for the migration plan and the rationale for not bundling the API-hygiene churn with this data-integrity release.

## [0.5.1] - 2026-05-07

### Security
- **Dependency floor hardening (PMSERV-064)**: tightened lower bounds on runtime dependencies so `pip install pm-server` cannot resolve to versions with known issues. Source code is unaffected; only the dependency declaration in `pyproject.toml` changed.
  - `jinja2 >=3.0` → `>=3.1.3` — defensive against CVE-2024-22195 (sandbox escape via `xmlattr` filter, fixed in 3.1.3). pm-server only renders local-trust dashboard HTML (`dashboard.py`), so practical exposure is low, but the floor closes the supply-chain path.
  - `pydantic >=2.0` → `>=2.5` — skips the 2.4.x line that had ReDoS-class regex reports.
  - `pyyaml >=6.0` → `>=6.0.1` — avoids 6.0.0 parsing regression.
  - `click >=8.0` → `>=8.1`.
  - `fastmcp >=2.0` → `>=2.0,<4.0` — defensive upper bound against a future 4.x with potentially breaking changes (v0.5.0 was already tested against fastmcp 3.x in dev).
  - `tomlkit` floor unchanged at `>=0.13`.

### GitHub repository hardening (operational, not packaged)
- Enabled Dependabot vulnerability alerts + automated security fixes, secret scanning + push protection, and CodeQL default setup (Python) on the GitHub repository.
- Added minimal branch protection on `main`: force-push and deletions disallowed, conversation resolution required (admin bypass kept enabled to avoid single-maintainer lockout).

## [0.5.0] - 2026-05-07

### Added
- **Multi-Host MCP Installer (ADR-007, PMSERV-039)**: `pm-server install` and `pm-server uninstall` accept `--target {auto,all,claude-code,codex}` and `--dry-run` flags. New per-host functions `install_claude_code()` and `install_codex()` (the latter uses `tomlkit` for comment-preserving edits of `~/.codex/config.toml` with timestamped backup). Default remains `--target=claude-code` for v0.4.x compatibility.
- **Project Rules Injection — Multi-Host (ADR-008, PMSERV-044)**: `claudemd.py` renamed to `rules.py` (transparent re-export shim kept). New unified API `inject_pm_rules(project_root, target=...)` handles both `CLAUDE.md` and `AGENTS.md` with marker-bracketed in-place updates. New `pm_update_rules` MCP tool; default target is `auto` (filesystem + marker + `CLAUDECODE` env detection, with explicit fallback warning when no signal is found).
- `pm-server update-rules` CLI subcommand with `--target / --dry-run / --all` flags.
- **Multi-Session Disambiguation (ADR-009, PMSERV-049)**: `pm_recall` now returns `current_session_id` and, when several sessions overlap on the same project, a `last_session_candidates` array plus `ambiguity_detected: true` so each session can pick its own context. Memory rows gain an `updated_at` column.
- AGENTS.md instruction-file generation with `<!-- pm-server:begin v=N -->` marker section, mirroring the existing CLAUDE.md treatment.
- `tomlkit` dependency added (~150KB pure Python) for comment-preserving edits of `~/.codex/config.toml`.

### Changed
- MCP tool count: 30 → 31 (`pm_update_rules` added).
- Test count: 413 → 578.
- Dataclasses `InstallResult` / `InstallSummary` (installer.py) and `InjectResult` / `InjectSummary` (rules.py) standardise per-host outcome reporting with `target` / `target_file` / `host` / `status` / `message` / `backup_path` / `is_dry_run` fields.
- `pm_update_claudemd` MCP tool now delegates to `pm_update_rules(target="claude-code")` while preserving its v0.4.x dict response shape verbatim (regression-guarded by `tests/test_server.py::test_pm_update_claudemd_returns_legacy_dict_shape`).
- `pm-server update-claudemd` CLI command kept as a legacy alias of `update-rules --target=claude-code`; both slated for deprecation in v0.6.0 and removal in v1.0.0 (PMSERV-055).
- `pm_status` response gains a `rules` key alongside the legacy `claudemd` key (additive, the latter is unchanged).
- `installer.py` and `rules.py` now share `utils._timestamped_backup` and `utils._atomic_write_text` (mkstemp-based) helpers.

### Fixed
- **Latent atomic-write race in `installer.py`** (PMSERV-044 cross-check R8): the previous fixed `.tmp` suffix could collide between concurrent processes; replaced with `tempfile.mkstemp(dir=path.parent, suffix=".tmp")` via the new shared `utils._atomic_write_text`.
- **Umask permission bug in `_atomic_write_text`** (PMSERV-044 smoke finding, commit `d347306`): mkstemp's default `0o600` mode was leaking into the destination file; the helper now normalises to `0o644 & ~umask` so user-readable files stay user-readable.
- **`pm_server.__version__` desync with package metadata** (PMSERV-042 release-time finding): hardcoded `__version__ = "0.4.0"` in `src/pm_server/__init__.py` was not bumped alongside `pyproject.toml`, causing `pm-server --version` to misreport from a 0.5.0 wheel even though the wheel METADATA was correct. Fixed by syncing to `"0.5.0"`; a structural improvement (derive `__version__` from `importlib.metadata.version()` so `pyproject.toml` becomes the single source of truth) is tracked as a follow-up.

### Documentation
- README: new "Multi-Host Support (Claude Code + Codex CLI)" section covering both installer (`--target` flag) and rules injection (`pm_update_rules`); expanded CLI Commands; refreshed Architecture diagram showing `installer.py` multi-host paths; MCP Tools table updated for `pm_update_rules`.
- `docs/design.md`: new chapter §5.2 (Multi-Host インストーラー戦略 / ADR-007) and chapter §6 (rules.py 設計 / ADR-008). §5.1 marked as legacy alias documentation pointing at PMSERV-055's deprecation timeline. §6–§10 renumbered to §7–§11 to make room for the new chapter.
- `docs/cheatsheet.md` / `cheatsheet.ja.md`: Codex / `pm-server update-rules` usage examples added.
- ADR-008 amendment 2026-04-30 records: target enum {auto, all, claude-code, codex} (A1/A2), 4-step host detection (A3), atomic-write helper unification (A6), UC8 (CLAUDE.md+marker → AGENTS.md auto-creation under Codex CLI) (A7).
- KR-002 (Knowledge Record): Multi-Host Detection Strategy super-research synthesis (Domain Expert / Critical Analyst / Lateral Thinker).

### Known caveats
- `CLAUDE.md` backup symmetry pending: v0.5.0 only creates a timestamped backup for `AGENTS.md` (PMSERV-058 will symmetrise in v0.6.0).
- `pm-server` MCP server processes started **before** v0.5.0 source edits may hit a stale module cache `ImportError` on `pm_status` (lazy import discovers the new `rules.py` against an old cached `utils.py`). Workaround: restart the MCP host. Defensive fingerprint logging tracked under PMSERV-060.
- Multi-session disambiguation surfaces context but does not yet protect storage; YAML atomic write + file locking is on track for v0.5.x via PMSERV-048.

## [0.4.0] - 2026-04-17

### Added
- Workflow Engine: template-based state machine with 5 MCP tools (`pm_workflow_start`, `pm_workflow_advance`, `pm_workflow_status`, `pm_workflow_list`, `pm_workflow_templates`)
- Built-in workflow templates: `discovery` (research/brainstorm) and `development` (implementation)
- Workflow chaining support (e.g., discovery → development)
- Loops, gates (`user_approval`), and optional steps for workflow flexibility
- Knowledge Records: structured knowledge between casual Memory and formal ADR
- `pm_record` and `pm_knowledge` MCP tools with 3 enums (KnowledgeCategory, KnowledgeStatus, ConfidenceLevel)
- Super Research skill + dashboard extensions (Phase 7)
- `pm_add_issue` severity parameter (`defect` | `enhancement`) — gates parent auto-revert
- Structured `warnings[]` array in MCP tool responses: `{level, code, message, remediation}`
- `Task.severity` field persists the issue classification
- CLAUDE.md template v7: documents severity selection, warnings[] relay, workflow rules

### Fixed
- `pm_add_issue` silent parent-revert UX issue (ADR-006): Claude now explicitly relays auto-revert side-effects via structured warnings
- `enhancement` severity issues no longer unexpectedly revert parent tasks from `done`

### Changed
- MCP tool count: 23 → 30
- Pydantic model count: 14 → 17
- Enum count: 10 → 15
- Test count: 305 → 413
- `pm_add_issue` default severity is `defect` (backward-compatible with v0.3.x behavior)
- Legacy fields `parent_reverted` and `message` remain in responses (slated for removal in 0.5.0)

## [0.3.3] - 2026-04-16

### Added
- Child issue (sub-task) support: `pm_add_issue` tool for creating issues linked to parent tasks via `parent_id`
- `pm_tasks` filter by `parent_id` to list child issues
- Auto-revert parent task from `done` to `review` when a child issue is added
- `all_issues_resolved` flag in `pm_update_task` when all sibling issues are done
- PostToolUse hooks: auto-remind PM actions after `git commit` via Claude Code hooks
- `pm-server hook post-tool-use` CLI command for hook handler
- Auto-install hooks from `pm_status` if not configured
- Generic detection of other MCP rule sections in CLAUDE.md (Open-Closed Principle)
- `other_rule_sections` in `pm_status` response for cross-MCP coordination
- CLAUDE.md template v5 with instruction to execute other rule sections

### Fixed
- **Critical**: `resolve_project_path` no longer matches global `~/.pm/` as a project directory (ADR-004)
- Added `_is_project_pm_dir()` guard to distinguish project `.pm/` from global registry
- `pm_cleanup` now detects orphan project files (tasks.yaml, decisions.yaml) in `~/.pm/`
- `pm_log` and `pm_remember` auto-link to active in-progress task when `task_id` is omitted

### Changed
- MCP tool count: 16 → 23
- Pydantic model count: 12 → 14
- Enum count: 9 → 10
- Test count: 136 → 305

## [0.3.2] - 2026-04-15

### Changed
- Updated README.md with Memory Layer documentation
- PyPI package rebuild (v0.3.1 had stale README)

## [0.3.1] - 2026-04-15

### Added
- Memory Layer: `pm_remember`, `pm_recall`, `pm_session_summary` tools
- `pm_memory_search` for advanced full-text search with filters
- `pm_memory_stats` and `pm_memory_cleanup` for memory operations
- SQLite + FTS5 based memory storage with cross-project global index
- Session continuity via `ContextBuilder` (Progressive Disclosure)
- `pm-server context-inject` CLI command
- CLAUDE.md template v2-v4 with memory layer rules

## [0.3.0] - 2026-04-08

### Added
- CLAUDE.md auto-management: `pm_init` automatically adds PM Server rules with version markers
- `pm_update_claudemd` MCP tool (16th tool) for updating PM Server rules section
- `pm-server update-claudemd` CLI command with `--all` flag for batch updates
- `claudemd.py` module with marker-based section management

### Fixed
- storage.py YAML header showing "PM Agent" instead of "PM Server"
- dashboard_portfolio.html title showing old name
- pm_discover MCP tool default scan path changed from "~" to "." (security)
- uninstall_mcp() missing --scope user flag
- migrate_from_pm_agent() now uses shutil.which() and timeout
- Case-insensitive detection of "PM Agent" references in migrate command
- `PmAgentError` renamed to `PmServerError`

### Changed
- Removed internal development prompts from docs/
- Added `.claude/` and `.pm/` to .gitignore
- pyproject.toml: added classifiers and dev extras
- MCP tool count: 15 → 16

## [0.2.0] - 2026-04-08

### Changed
- Package renamed from `pm-agent` to `pm-server` (PyPI name conflict with existing `PMAgent`)
- GitHub repository moved to `flc-design/pm-server`
- Added `pm-server migrate` command for transitioning from pm-agent

### Added
- `README.ja.md` — Japanese README
- `migrate` CLI command for pm-agent → pm-server transition

## [0.1.0] - 2026-04-07

### Added
- 15 MCP tools for project management
- YAML-based task, decision, and log storage
- HTML dashboard with Chart.js (single + portfolio view)
- Text dashboard fallback
- Velocity tracking and risk detection
- Project discovery and auto-registration
- CLI interface (install, uninstall, serve, discover, status)
- Claude Code integration via `claude mcp add --scope user`

### Fixed
- installer.py: use `claude mcp add` instead of writing to wrong settings file
- Template path resolution for packaged installations
- Test isolation: prevent tests from polluting `~/.pm/registry.yaml`

### Documentation
- Development workflow guide (docs/workflow.md)
- Design document (docs/design.md)
- Project status report (docs/status.md)
