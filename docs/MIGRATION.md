# Migration Guide: PM Server → PM Lens

This document tracks the rebrand from "PM Server" to **PM Lens** (`pmlens`).

Phase-1 (the current branch, `feat/pmlens-rebrand-phase1`) touches the **display
layer only**: the product display name in README/docs, the GitHub repository
name, and the `.mcpb`/plugin display surfaces. Every load-bearing entity — the
PyPI distribution name, the MCP registration key, the Python import name, the
local folder name, all GitHub/PyPI URLs, and the in-code display strings — is
deliberately left unchanged so existing installs keep working. Those entities
migrate together in **Phase-2**, at publish time.

---

## Phase-2 (publish-time entity migration) TODO

The following changes are intentionally deferred to Phase-2. They must land
together at publish time so that no half-renamed state ever ships.

- **PyPI distribution name.** Republish the package as `pmlens`. Keep
  `pm-server` published as a thin rename-wrapper (a shim distribution that
  depends on `pmlens`) so existing `pip install pm-server` / `uvx pm-server`
  users are redirected rather than broken.
- **MCP registration key (`pm-server` → `pmlens`).** Update the registration key
  in `installer.py:249`, the Codex registration table key, and re-register the
  server under the new key. Existing registrations under `pm-server` need a
  documented re-registration step.
- **Python import name (`pm_server` → `pmlens`).** Rename the import package,
  rename the local source folder accordingly, and update the self-registration
  path that points at the package location.
- **In-code display strings (~60).** Update the ~60 hardcoded display strings
  across `installer`, `__main__`, `hooks`, `dashboard`, `storage`, `server`, and
  `__init__`. Update the corresponding assertion in `test_installer.py:1358`.
- **Rules template heading.** Change the `rules.py` template heading
  "PM Server 自動行動ルール" to the PM Lens wording and bump `TEMPLATE_VERSION`
  from `10` to `11`. Update `test_rules.py` in lockstep, and mirror the heading
  change in `README:400` and `design.md` at the same time (these three must move
  together so the drift-guard stays green).
- **Hardcoded GitHub URLs in the plugin/manifest.** Update the 6 hardcoded
  GitHub URLs in the plugin/manifest surfaces to point at the `pmlens`
  repository.
- **`.mcpb` bundle dist name.** Change the build output name in `build_mcpb.py`
  from `pm-server-<v>.mcpb` to `pmlens-<v>.mcpb`.

---

## `.mcpb` rebuild & Claude Desktop reload runbook

After Phase-1 (or any change to the bundled extension), rebuild the `.mcpb` and
reload it in Claude Desktop:

1. **Rebuild the bundle:**

   ```sh
   python scripts/build_mcpb.py
   ```

   This produces `dist/pm-server-0.10.0.mcpb`.

2. **Reinstall in Claude Desktop.** Open **Settings → Extensions**, remove the
   existing extension, and install the freshly built `.mcpb`.

3. **Verify the display name.** After install, the `display_name` will now read
   **"PM Lens (Read-only)"**.

> **Note:** Because the version is unchanged (`0.10.0`), Claude Desktop may not
> auto-detect an update. A manual **remove + reinstall** is recommended to be
> sure the new bundle is picked up.

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
