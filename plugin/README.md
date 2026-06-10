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
| SessionStart hook | `hooks/hooks.json` + `hooks/session-start.sh` | Re-homes the `CLAUDE.md` session ritual: injects a directive to run pm_status/pm_next/pm_recall, warns on duplicate registration, per-session double-fire guard |
| PostToolUse hook | `hooks/hooks.json` + `hooks/post-tool-use.sh` | Re-homes the git-commit reminder (run pm_update_task/pm_log/pm_next). Directive-only; **defers** when the manual `pm-server hook` is also in `settings.json`, so a user with both never gets a doubled reminder |
| Skill | `skills/pm/SKILL.md` | Model-invoked restatement of pm-server's behavioural rules |

### Why a hook + skill instead of CLAUDE.md

Plugins **cannot** ship a `CLAUDE.md` (no system-prompt-level persistent
instructions). pm-server's auto-behaviour (run `pm_status` at session start,
flip tasks to `in_progress`, surface `warnings[]`, …) is therefore re-homed to:

1. **`SessionStart` hook** — deterministically injects a *directive* to run the
   ritual (`pm_status` / `pm_next` / `pm_recall`) through the bundled MCP. It does
   NOT compute status itself: pm-server may not be on the hook's PATH, and even
   when it is it can resolve a different `~/.pm` than the bundled MCP (HOME
   override) — so the hook defers to the correctly-scoped MCP tools. This is
   *data injection*, not a guarantee the model acts, but it removes the "forgot to
   call `pm_status`" failure. (Only `PreToolUse` can hard-block; everything else
   is advisory.) Verified: the injected directive reaches the model (present in
   the session transcript).
2. **Skill** — restates the full rule set so the model can follow it mid-session.

`claudemd.py` stays for Codex / non-CC hosts.

## Install (end users)

```text
/plugin marketplace add flc-design/pm-server
/plugin install pm-server@flc-design
```

> **Prerequisite:** `uvx` (from [uv](https://docs.astral.sh/uv/)) on PATH. The
> bundled MCP pulls `pm-server` from PyPI automatically (published — latest
> 0.10.0), so no prior `pip install` is needed.

### ⚠️ Existing manual-registration users — migrate first

MCP tool names are **not** namespaced by server. If you already registered
pm-server manually (`claude mcp add` / settings.json), installing this plugin
gives you **two** pm-server instances and duplicate `pm_*` tools. The
SessionStart hook detects this and prints a warning. To resolve:

```text
claude mcp remove pm-server     # drop the manual registration; rely on the plugin
```

The **PostToolUse** hook is safe to leave as-is during migration: the plugin's
copy auto-defers when it sees the manual `pm-server hook` in `settings.json`, so
you won't get a doubled commit reminder. To fully retire the manual setup you
may also remove that hook (`pm-server uninstall-hooks`), but it is not required.

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

- **Committed (`.mcp.json`)** — release form: `uvx pm-server@0.10.0`. Pinned for
  reproducibility; `uvx --from "pm-server>=0.10.0" pm-server` for a floor instead.
- **Dev** — local source via `uv run --project <repo> pm-server serve`, supplied
  through `--mcp-config` in the isolation harness above (not committed).

## Resolved design decisions

- **Collision guard — bundle + warn + documented migration** (ADR-027). The
  bundled MCP stays; the SessionStart hook detects a manual registration and
  warns with the exact `claude mcp remove` remediation. New (plugin-only) users
  never collide — only a user who keeps *both* a manual registration and the
  plugin sees duplicate `pm_*` tools, and the warning tells them how to resolve
  it. Rejected: *detect-and-defer* (mutates user config, against pm-server's
  "never break an existing setup" rule) and *centralise* (loses the
  "`/plugin install` and it just works" bundling).
- **`marketplace.json`** — added at the repository root
  (`.claude-plugin/marketplace.json`), marketplace name `flc-design`, listing
  this `plugin/` directory (`"source": "./plugin"`) as the catalog source.
- **PostToolUse parity, not double-fire.** The manual install ships *only* a
  `PostToolUse` hook (the git-commit reminder) and *no* SessionStart hook, so
  the original "SessionStart fires twice" worry could not actually occur. The
  real gap was the opposite: a plugin-only user got *no* commit reminder. The
  plugin now ships its own `PostToolUse` hook (`hooks/post-tool-use.sh`) that
  injects the same directive and **defers automatically** when it detects the
  manual `pm-server hook` in `settings.json` — so a user with both gets exactly
  one reminder, with zero manual cleanup required.
