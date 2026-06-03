#!/usr/bin/env bash
# pm-server plugin — SessionStart hook.
#
# Re-homes the "session start" ritual that CLAUDE.md drives for the manually
# registered MCP setup. Plugins cannot ship a CLAUDE.md (no system-prompt-level
# persistent instructions), so we deterministically inject the current project
# context at session start instead. This is data injection, not a guarantee the
# model acts on it — but it removes the "forgot to call pm_status" failure mode.
#
# Behaviour:
#   1. Double-fire guard — settings.json hooks AND plugin hooks both fire; a
#      per-session state file under $CLAUDE_PLUGIN_DATA makes this idempotent.
#   2. Collision warning — if pm-server is ALSO registered manually, warn that
#      the plugin bundles its own (duplicate tools otherwise).
#   3. Context injection — emit `pm-server context-inject` output (the CLI
#      mirror of the MCP session-start data) as SessionStart additionalContext.
set -uo pipefail

input="$(cat 2>/dev/null || true)"

# --- 1. double-fire guard -----------------------------------------------------
session_id=""
if command -v jq >/dev/null 2>&1; then
  session_id="$(printf '%s' "$input" | jq -r '.session_id // empty' 2>/dev/null || true)"
fi
data_dir="${CLAUDE_PLUGIN_DATA:-${TMPDIR:-/tmp}/pm-server-plugin}"
mkdir -p "$data_dir" 2>/dev/null || true
if [ -n "$session_id" ]; then
  guard="$data_dir/.sessionstart.$session_id"
  [ -e "$guard" ] && exit 0   # already fired this session — do not double-inject
  : > "$guard" 2>/dev/null || true
fi

# --- 2. collision warning -----------------------------------------------------
dup_warning=""
if command -v claude >/dev/null 2>&1 && claude mcp get pm-server >/dev/null 2>&1; then
  dup_warning="WARNING: pm-server is also registered manually (claude mcp). This plugin bundles its own pm-server MCP; tool names are NOT namespaced by server, so you now have duplicate pm_* tools. Run 'claude mcp remove pm-server' to drop the manual registration and rely on the plugin."
fi

# --- 3. context injection -----------------------------------------------------
context="$(pm-server context-inject 2>/dev/null || true)"
if [ -z "$context" ]; then
  context="pm-server plugin active. Call pm_status to load project state, pm_next for the top recommended tasks, and pm_recall to restore prior-session context."
fi

if [ -n "$dup_warning" ]; then
  payload="$(printf '%s\n\n%s' "$dup_warning" "$context")"
else
  payload="$context"
fi

if command -v jq >/dev/null 2>&1; then
  jq -n --arg c "$payload" \
    '{hookSpecificOutput: {hookEventName: "SessionStart", additionalContext: $c}}'
else
  # Fallback: SessionStart stdout is injected as context even without the
  # structured envelope.
  printf '%s\n' "$payload"
fi
