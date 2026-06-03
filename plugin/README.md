# pm-server — Claude Code plugin

A **hybrid** distribution layer for [pm-server](https://github.com/flc-design/pm-server).
The host-agnostic MCP core stays as-is (so Codex and other hosts keep working);
this plugin adds a Claude-Code-native install + integration layer on top
(ADR-027).

## What it ships

| Component | File | Purpose |
|-----------|------|---------|
| Manifest | `.claude-plugin/plugin.json` | Plugin metadata |
| Bundled MCP | `.mcp.json` | Runs `uvx pm-server@x.y` — no prior `pip install` needed |
| SessionStart hook | `hooks/hooks.json` + `hooks/session-start.sh` | Re-homes the `CLAUDE.md` session ritual: injects project context, warns on duplicate registration |
| Skill | `skills/pm/SKILL.md` | Model-invoked restatement of pm-server's behavioural rules |

### Why a hook + skill instead of CLAUDE.md

Plugins **cannot** ship a `CLAUDE.md` (no system-prompt-level persistent
instructions). pm-server's auto-behaviour (run `pm_status` at session start,
flip tasks to `in_progress`, surface `warnings[]`, …) is therefore re-homed to:

1. **`SessionStart` hook** — deterministically injects `pm-server context-inject`
   output so the session opens already aware of project state. This is *data
   injection*, not a guarantee the model acts — but it removes the "forgot to
   call `pm_status`" failure. (Only `PreToolUse` can hard-block; everything else
   is advisory.)
2. **Skill** — restates the full rule set so the model can follow it mid-session.

`claudemd.py` stays for Codex / non-CC hosts.

## Install (end users)

```text
/plugin marketplace add flc-design/pm-server
/plugin install pm-server@flc-server-marketplace
```

> **Prerequisite:** `uvx` (from [uv](https://docs.astral.sh/uv/)) on PATH, plus
> `pm-server` published to PyPI. Until published, use the dev override below.

### ⚠️ Existing manual-registration users — migrate first

MCP tool names are **not** namespaced by server. If you already registered
pm-server manually (`claude mcp add` / settings.json), installing this plugin
gives you **two** pm-server instances and duplicate `pm_*` tools. The
SessionStart hook detects this and prints a warning. To resolve:

```text
claude mcp remove pm-server     # drop the manual registration; rely on the plugin
```

(Open design question — see "Collision guard" below.)

## Testing in isolation (zero impact on your live setup)

Verified on Claude Code 2.1.161. Two independent levers fully isolate a test
run so it cannot touch your live config or your live `~/.pm` data:

```bash
SANDBOX=/tmp/pm-plugin-sandbox
rm -rf "$SANDBOX"; mkdir -p "$SANDBOX/claude-config" "$SANDBOX/pmhome" "$SANDBOX/testproj"

# Dev MCP config: point at LOCAL source + relocate pm-server's ~/.pm via HOME.
cat > "$SANDBOX/dev.mcp.json" <<JSON
{ "mcpServers": { "pm-server": {
  "command": "uv",
  "args": ["run", "--project", "/ABS/PATH/TO/pm-server", "pm-server", "serve"],
  "env": { "HOME": "$SANDBOX/pmhome", "PYTHONUNBUFFERED": "1" }
} } }
JSON

cd "$SANDBOX/testproj"
CLAUDE_CONFIG_DIR="$SANDBOX/claude-config" \
  claude --plugin-dir /ABS/PATH/TO/pm-server/plugin \
         --strict-mcp-config --mcp-config "$SANDBOX/dev.mcp.json"
```

- `CLAUDE_CONFIG_DIR` → all Claude config (settings, `~/.claude.json` MCP
  registrations, installed plugins, hooks) resolves under the sandbox. Your
  **live global pm-server MCP is invisible** here (proven: `claude mcp list`
  reports "No MCP servers configured").
- `env.HOME` on the bundled MCP → pm-server's `Path.home()/.pm` resolves under
  the sandbox. Your **live `~/.pm` registry + memory.db are untouched** (proven:
  `~/.pm/registry.yaml` sha256 unchanged after an isolated `discover`).
- `--plugin-dir` → loads this plugin for that session only.
- `--strict-mcp-config` → use only `--mcp-config`, ignoring all other MCP config.

Iterate: edit files, then `/reload-plugins` in-session (skills hot-reload;
hooks/MCP need the reload).

## Release vs dev `.mcp.json`

- **Committed (`.mcp.json`)** — release form: `uvx pm-server@0.9.0`. Pinned for
  reproducibility; `uvx --from "pm-server>=0.9.0" pm-server` for a floor instead.
- **Dev** — local source via `uv run --project <repo> pm-server serve`, supplied
  through `--mcp-config` in the isolation harness above (not committed).

## Open design questions (pre-publish)

- **Collision guard.** Current default: bundle the MCP + warn on duplicates.
  Alternatives: (A) *detect-and-defer* — a Setup hook runs `claude mcp add` only
  when pm-server is absent (mutates user config); (B) *centralise* — ship only
  skills/hooks and require a single shared MCP. Decide before publishing.
- **`marketplace.json`** — to be added at the repository root
  (`.claude-plugin/marketplace.json`) listing this `plugin/` directory as the
  catalog source, for the publish step.
- **installer.py double-fire** — when the plugin is active, suppress the
  settings.json hook that `installer.py` injects, or the SessionStart action
  fires twice. The hook's per-session guard dedupes the *plugin's* own hook, but
  not an independent settings.json copy.
