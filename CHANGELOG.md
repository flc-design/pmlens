# Changelog

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
