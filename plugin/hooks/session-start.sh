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
# session_id: prefer jq, fall back to grep/cut so the guard still works when jq
# is absent. Without this fallback the whole guard silently no-ops on jq-less
# machines and we re-inject on every fire (settings.json hook + plugin hook).
session_id=""
if command -v jq >/dev/null 2>&1; then
  session_id="$(printf '%s' "$input" | jq -r '.session_id // empty' 2>/dev/null || true)"
fi
if [ -z "$session_id" ]; then
  session_id="$(printf '%s' "$input" | grep -o '"session_id"[^,}]*' | head -1 | cut -d'"' -f4 || true)"
fi
data_dir="${CLAUDE_PLUGIN_DATA:-${TMPDIR:-/tmp}/pm-server-plugin}"
mkdir -p "$data_dir" 2>/dev/null || true
# Reap stale markers — CLAUDE_PLUGIN_DATA persists across plugin updates, so
# per-session guard files would otherwise accumulate unbounded.
find "$data_dir" -name '.sessionstart.*' -mtime +30 -delete 2>/dev/null || true
if [ -n "$session_id" ]; then
  guard="$data_dir/.sessionstart.$session_id"
  # Atomic claim via noclobber: the first fire creates the marker and proceeds;
  # a concurrent or repeat fire fails the redirect and exits. Avoids the
  # check-then-create TOCTOU of `[ -e ] && exit`.
  if ! ( set -C; : > "$guard" ) 2>/dev/null; then
    exit 0   # already fired this session — do not double-inject
  fi
fi

# --- 2. collision warning -----------------------------------------------------
# Bound the probe: `claude mcp get` can spawn/health-check and must never hang a
# session start. Use timeout when present (timeout or gtimeout); on platforms
# without either (plain macOS) skip the bound rather than break the check.
dup_warning=""
mcp_timeout=""
if command -v timeout >/dev/null 2>&1; then mcp_timeout="timeout 3"
elif command -v gtimeout >/dev/null 2>&1; then mcp_timeout="gtimeout 3"; fi
if command -v claude >/dev/null 2>&1 && $mcp_timeout claude mcp get pm-server >/dev/null 2>&1; then
  dup_warning="WARNING: pm-server is also registered manually (claude mcp). This plugin bundles its own pm-server MCP; tool names are NOT namespaced by server, so you now have duplicate pm_* tools. Run 'claude mcp remove pm-server' to drop the manual registration and rely on the plugin."
fi

# --- 3. session-start directive ----------------------------------------------
# We deliberately do NOT compute project context in the hook itself. pm-server
# is not reliably on PATH here, and even when it is it may resolve a different
# data store (HOME) than the bundled MCP — so a hook-computed status could be
# from the wrong project. Instead we instruct the model to run the ritual
# through the (correctly-scoped) MCP tools — the same contract CLAUDE.md uses.
directive="pm-server plugin active. Begin this session with the pm-server ritual BEFORE your first reply: call pm_status (project state + warnings), pm_next (top 3 tasks), and pm_recall (restore prior-session context). Surface any blockers, overdue items, or tool warnings[] to the user verbatim."

if [ -n "$dup_warning" ]; then
  payload="$(printf '%s\n\n%s' "$dup_warning" "$directive")"
else
  payload="$directive"
fi

if command -v jq >/dev/null 2>&1; then
  jq -n --arg c "$payload" \
    '{hookSpecificOutput: {hookEventName: "SessionStart", additionalContext: $c}}'
else
  # Fallback: SessionStart stdout is injected as context even without the
  # structured envelope.
  printf '%s\n' "$payload"
fi
