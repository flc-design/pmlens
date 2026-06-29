# Changelog

## [Unreleased]

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
