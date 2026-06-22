# Migration Guide: PM Server → PM Lens

This document tracks the rebrand from "PM Server" to **PM Lens** (`pmlens`),
split into three phases. It is the source-of-truth runbook; every Phase-2 commit
cites it.

> Last refreshed: 2026-06-22, against **live code** (8-agent recon, verdict
> `ready_with_fixes`). Earlier revisions (2026-06-19) carried stale line numbers
> and counts — superseded below.

---

## Status

- **Phase-1 (display layer) — DONE.** Merged to `main` (commit `ba1cf45`).
  Product display name in README/docs, the GitHub repository rename (now
  `flc-design/pmlens`), the `.mcpb`/plugin display surfaces, **and all
  GitHub/PyPI URLs** were updated here. Verified: no `github.com/flc-design/pm-server`
  remains outside `CHANGELOG.md` history (immutable — do **not** re-touch).
- **Name reservation — DONE.** `pmlens` 0.0.1 placeholder published to PyPI
  (flc-design account) so the name cannot be squatted. Pinned at `0.0.1`
  (`packaging/pmlens-reservation/`) — below `0.11.0` so the real release shadows
  it. See **ADR-031**.
- **Phase-2 (distribution rename + display layer) — PLANNED (PMSERV-136).** This document.
- **Phase-3 (identity rename) — DEFERRED (PMSERV-137).** See **ADR-032**.

---

## Locked decisions (ADR-031 / ADR-032)

1. **Real `pmlens` version: `0.11.0`** — lineage continuation from pm-server
   0.10.0; safely above the reserved 0.0.1 placeholder.
2. **Keep `pm-server` as a thin rename-wrapper** — a new distribution
   `name = "pm-server"`, `version = "0.11.0"` (matches pyproject so the plugin's
   `uvx pm-server@0.11.0` pin resolves to it), `dependency "pmlens>=0.11.0"`,
   with a re-export stub. Lives in a new `packaging/pm-server-wrapper/`
   (mirroring `packaging/pmlens-reservation/`). Published **manually, once**
   (wheel + sdist only, **no** `.mcpb`).
3. **Single-job publish in `release.yml`** — the recurring workflow publishes
   only `pmlens` via Trusted Publisher (one `pypi` environment / one OIDC scope).
   The frozen wrapper is a separate manual step.
4. **Identity renames are Phase-3, not Phase-2.** The import name, MCP
   registration key, FastMCP name, and source folder flip the `mcp__pm-server__*`
   tool namespace and break existing installs without a migration path, so they
   ship in a dedicated Phase-3 with a re-registration runbook + compatibility
   detection. See ADR-032.

---

## Invariants — MUST NOT change in Phase-2

Changing any of these breaks existing installs or orphans users' files:

- **Marker slug** `pm-server:begin` / `pm-server:end` — `rules.py` lines 34-36
  (`BEGIN_MARKER`/`END_MARKER`/`BEGIN_PATTERN`), 40, 165. Line ~195 excludes
  `pm-server` from `other_rule_sections`. Renaming would make `pm_update_rules`
  append a duplicate block instead of upgrading existing CLAUDE.md/AGENTS.md.
- **`FastMCP("pm-server")`** — `server.py:89` (load-bearing, not display).
- **hooks.py load-bearing refs** — `_HOOK_COMMAND_PREFIX = "pm-server hook"` (16),
  `_PM_HOOK_MARKER = "pm-server"` (19), `shutil.which("pm-server")` (48), CLI tip
  (222). These are binary/hook-detection references, not the product name.
- **MCP registration key** `pm-server` (`installer.py` ~14 sites) — Phase-3.
- **Import package** `pm_server`, the source folder, and the `console_scripts`
  `pm-server` CLI name — Phase-3.
- **Tool names** `pm_*`.

---

## Phase-2 — ordered execution plan

Reversible-first, irreversible-last. **Every commit keeps CI green** (source +
its lockstep tests + mirror docs in the same commit). Counts/locations below are
recon-verified but **re-grep at execution time** — never trust a number in a doc
(lesson from PMSERV-132).

### Step 1 — Refresh this runbook (docs-only) ✅ *(this commit)*

Zero CI risk; corrects stale guidance the later steps depend on (GitHub URLs
already done in Phase-1; real display-string count is ~87 not ~60; identity
renames moved to Phase-3; marker slug is invariant). No test references this file.

### Step 2 — Version bump `0.10.0` → `0.11.0`

- **Files (one commit):** `pyproject.toml:7`, `manifest.json:5`,
  `src/pm_server/__init__.py:3`, `uv.lock` (workspace entry), **and the plugin
  version surfaces** — `plugin/.claude-plugin/plugin.json` (`version`),
  `.claude-plugin/marketplace.json` (`metadata.version`), `plugin/.mcp.json`
  (`uvx pm-server@` pin → `pm-server@0.11.0`), `plugin/README.md` pins.
- **Lockstep:** `tests/test_manifest.py` (~45-48 `test_version_matches_pyproject`,
  ~51-63 `test_init_version_matches_pyproject`) **and**
  `tests/test_plugin.py::TestPluginVersionSync` (4 tests) require *every* version
  surface — including the plugin pins — to equal pyproject. The plugin surfaces
  are therefore **not** deferrable (discovered by the full-suite gate, not the
  recon plan). The pin keeps the `pm-server` name (resolved by the wrapper);
  `uvx pm-server@0.11.0` becomes installable once the wrapper ships in Step 6.
- Do **not** change `pyproject` `name=` here (that is the irreversible Step 6).
  Leave `TEMPLATE_VERSION` alone (it moves in Step 4).
- **Verify:** `pytest tests/test_manifest.py`.

### Step 3 — Flip display strings `PM Server` → `PM Lens` (atomic batch)

~87 user-facing occurrences of the two-word product name. **One commit** with
its test assertions to avoid a split-brain state.

- **src/pm_server/ (display only):** `__init__.py`(1), `__main__.py`(9),
  `dashboard.py`(2), `hooks.py`(4 — incl. `[PM Server Lens]`@214 → `[PM Lens]`,
  `[PM Server]`@225 → `[PM Lens]`; do **not** touch lines 16/19/48/222),
  `installer.py`(15 message strings — **not** the registration key),
  `models.py`(2), `redaction.py`(1), `storage.py`(2 — incl. `# PM Server - {filename}`
  YAML header @74), `server.py`(5 docstrings/comments incl. lines 1/415 — do
  **not** touch `FastMCP("pm-server")`@89), `rules.py`(19 message strings — the
  template heading @41 is Step 4), `utils.py`(1).
- **Other display surfaces:** `docs/workflow.md`(96, 183 narrative),
  `conftest.py:1` docstring, `plugin/README.md` (1, 3 — display; line 13
  `uvx pm-server` is a reference, keep).
- **Tests in the same commit:** `tests/test_installer.py:1358` (and ~1383/1388/1394),
  `tests/test_smoke.py:42`, `tests/test_storage.py:66,70`.
- **Verify:** `grep -rn 'PM Server' src/pm_server/*.py` returns only `rules.py:41`
  (Step 4); `pytest tests/` green.

### Step 4 — `TEMPLATE_VERSION` `10` → `11` + heading 4-mirror

- **Files (one commit):** `src/pm_server/rules.py` (`:33` `TEMPLATE_VERSION`,
  `:41` heading `## PM Server 自動行動ルール（必ず従うこと）` → `## PM Lens ...`),
  `README.md` (~400), `README.ja.md` (~341), `docs/design.md` (~946),
  `tests/test_rules.py:52` (`== 10` → `== 11`; rename the pinned-at-v10 test).
- **INVARIANT:** do **not** touch the marker slug (lines 34-36, 40, 165).
- The `10→11` bump is the delivery vehicle: on upgrade it re-injects the new
  heading into existing users' CLAUDE.md/AGENTS.md — *because the marker stays
  constant*.
- **Verify:** `pytest tests/test_rules.py tests/test_claudemd.py` green;
  `grep 'PM Server 自動行動ルール'` across the 4 files → 0;
  `grep 'pm-server:begin' src/pm_server/rules.py` → still 3 sites.

### Step 5 — `.mcpb` name + `release.yml` glob/URL

- **Files (one commit):** `scripts/build_mcpb.py:169` (`pm-server-{version}.mcpb`
  → `pmlens-{version}.mcpb`) + docstring (~20), `.github/workflows/release.yml:90`
  (`path: dist/pm-server-*.mcpb` → `dist/pmlens-*.mcpb`), `release.yml:52`
  (`url: .../project/pm-server/` → `.../project/pmlens/`).
- These move together: if the build writes `pmlens-*.mcpb` but the glob still
  matches `pm-server-*.mcpb`, `pack-mcpb` silently uploads **0 files**
  (`if-no-files-found: error` only fires after path matching).
- **Pre-flight (required):** `python scripts/build_mcpb.py && ls -la dist/pmlens-*.mcpb`.
- `manifest.json` `name` field stays `pm-server` for now (MCP key — Phase-3).

### Step 6 — ⛔ IRREVERSIBLE, GATED: publish `pmlens` 0.11.0 + stage wrapper

- `pyproject.toml:6` `name = "pm-server"` → `name = "pmlens"`. **KEEP**
  `console_scripts` `pm-server` and module `pm_server` (Phase-3) so only the
  *distribution* renames; CLI/import stay backward-compatible.
- **Pre-flight checklist:**
  - full `pytest tests/` (~1033) + all CI checks green **before** tagging;
  - `packaging/pm-server-wrapper/` built and `twine check`-clean, ready to publish;
  - reserved `pmlens` 0.0.1 confirmed `< 0.11.0`.
- Publish: push tag `v0.11.0` → `release.yml` stops at the **`pypi` environment
  manual approval** (the sole one-way door) → OIDC Trusted Publisher uploads.
- **Within hours:** manually publish the `pm-server` wrapper (0.11.0,
  `dependency pmlens>=0.11.0`) so `pip install pm-server` / `uvx pm-server` /
  the committed plugin pin `uvx pm-server@0.11.0` all resolve.
- **Verify:** `pip install pmlens==0.11.0` works; PyPI shows 0.11.0 shadowing
  0.0.1; `pip install pm-server` resolves to pmlens via the wrapper.

### Post-publish follow-ups (separate commits, not in Steps 1-6)

- **Plugin version pin** — the plugin's `uvx pm-server@` pin and the
  `plugin.json` / `marketplace.json` versions are bumped to `0.11.0` in **Step 2**
  (enforced by `tests/test_plugin.py::TestPluginVersionSync` — cannot be
  deferred). The pin keeps the `pm-server` package name, resolved by the wrapper;
  it becomes installable once the wrapper@0.11.0 ships (Step 6). Flipping the pin's
  package name to `pmlens` and the `.mcp.json` `:3` registration key is **Phase-3**.
- **SKILL.md sync** — `skill/SKILL.md` and `plugin/skills/pm/SKILL.md` have
  diverged (plugin variant is the superset). No test enforces equality;
  converge in a dedicated docs cleanup (non-blocking).

---

## Phase-3 (deferred) — identity rename · PMSERV-137 / ADR-032

After `pmlens` 0.11.0 is live. Flips the load-bearing identifiers, so it ships
**with** a user-facing re-registration runbook and `mcp__pm-server__*` →
`mcp__pmlens__*` compatibility detection:

- **Import package** `pm_server` → `pmlens`: rename the `src/` folder, ~39 import
  sites, and the self-registration path.
- **MCP registration key** `pm-server` → `pmlens`: `installer.py` ~14 sites
  (205/249/303/325/428/429/451/475/487/494/569/580/604/610) + the Codex
  registration table key.
- **FastMCP name** (`server.py:89`), **manifest** `entry_point`/`args`,
  **console_scripts** `pm-server`.
- **Tests:** `test_installer.py` (~15 sites), `test_plugin.py:220`,
  `test_manifest.py` module-name checks (~108-109).
- The marker slug stays invariant throughout.

---

## Rollback

- **Steps 1-5 (reversible):** plain source/doc/test edits — `git revert` + re-run
  CI. No published artifacts, no user-state mutation.
- **Step 4 caveat:** once a release *ships* with `TEMPLATE_VERSION = 11`, users'
  CLAUDE.md/AGENTS.md auto-update on their next session. Reverting the code after
  a shipped release causes version ping-pong — revert Step 4 **only before** any
  release goes out. (The marker is never touched, so no file is orphaned.)
- **Step 6 (irreversible):** a published PyPI name + version can never be
  unpublished or overwritten. Forward-only fix: `twine yank` the bad release and
  publish a higher patch (e.g. 0.11.1); the reserved 0.0.1 guarantees 0.11.0
  shadows it cleanly. The `pm-server` wrapper is the end-user rollback — as long
  as it ships within hours, `pip install pm-server` / `uvx pm-server` keep
  resolving via the dependency on `pmlens`.

---

## `.mcpb` rebuild & Claude Desktop reload runbook

After any change to the bundled extension, rebuild the `.mcpb` and reload it in
Claude Desktop:

1. **Rebuild the bundle:**

   ```sh
   python scripts/build_mcpb.py
   ```

   Today this produces `dist/pm-server-0.10.0.mcpb`. After Phase-2 Steps 2 & 5 it
   becomes `dist/pmlens-0.11.0.mcpb`.

2. **Reinstall in Claude Desktop.** Open **Settings → Extensions**, remove the
   existing extension, and install the freshly built `.mcpb`.

3. **Verify the display name.** After install, the `display_name` reads
   **"PM Lens (Read-only)"**.

> **Note:** If the version is unchanged, Claude Desktop may not auto-detect an
> update. A manual **remove + reinstall** is recommended.

---

## Data persistence

Reinstalling the extension does **not** touch your data. Registered projects,
tasks, and memory all live **outside** the `.mcpb` bundle and are **not** removed
by uninstall/reinstall:

- **Registered projects:** `~/.pm/registry.yaml`
- **Tasks:** per-project `.pm/tasks.yaml`
- **Memory:**
  - per-project `.pm/memory.db`
  - global `~/.pm/memory.db`
  - desktop outbox `~/.pm/desktop/desktop.db`

The only Desktop-managed value lost on uninstall is the `user_config`
`project_path`. Re-enter it after reinstall.

**Optional precautionary backup** before reinstalling:

```sh
cp -a ~/.pm ~/.pm.bak.$(date +%Y%m%d-%H%M%S)
```
