#!/usr/bin/env bash
# pm-server plugin — PostToolUse hook (git-commit reminder).
#
# Re-homes the post-commit reminder that the manually-registered setup drives
# via the settings.json PostToolUse hook (`pm-server hook post-tool-use`,
# installed by hooks.py). Plugins cannot ship a CLAUDE.md, and a plugin-only
# install would otherwise LOSE this reminder entirely. Like the SessionStart
# hook, this is *directive-only*: it does NOT read tasks.yaml itself (the hook
# cannot know the bundled MCP's HOME/PATH and could read a different ~/.pm), it
# injects a directive telling the model to run the pm_* tools through the
# correctly-scoped MCP.
#
# Double-fire guard: if the manual `pm-server hook post-tool-use` is ALSO
# present in settings.json, DEFER (emit nothing) so the user never gets two
# reminders for one commit. This matches the "bundle + warn, don't force
# removal" collision strategy (ADR-027): both present -> the manual hook wins;
# plugin-only -> this hook fires. No marker file is needed (unlike SessionStart)
# because we WANT to fire on every commit; the only duplication risk is the
# manual hook, which the settings.json probe below handles.
set -uo pipefail

input="$(cat 2>/dev/null || true)"

# --- 1. only act on `git commit` ---------------------------------------------
# Prefer jq to read tool_input.command; fall back to a substring scan of the
# whole payload when jq is absent. The canonical Python hook also matches on the
# "git commit" substring, so the looser fallback only risks a rare spurious
# reminder (e.g. an echo that literally contains "git commit"), never a missed
# or wrong-project mutation.
command_str=""
if command -v jq >/dev/null 2>&1; then
  command_str="$(printf '%s' "$input" | jq -r '.tool_input.command // empty' 2>/dev/null || true)"
fi
haystack="${command_str:-$input}"
case "$haystack" in
  *"git commit"*) ;;
  *) exit 0 ;;
esac

# --- 2. double-fire guard: defer if the manual settings.json hook is present ---
# The manual install writes a PostToolUse hook whose command contains
# "pmlens hook" (new identity) or the legacy "pm-server hook" (hooks.py markers
# "pmlens"/"pm-server" + "hook"). Dual-recognition (PMSERV-137): match EITHER so
# we defer to a manual hook installed under either identity. Resolve the global
# settings path the same way Claude Code does: CLAUDE_CONFIG_DIR overrides ~/.claude.
settings_dir="${CLAUDE_CONFIG_DIR:-$HOME/.claude}"
settings="$settings_dir/settings.json"
if [ -f "$settings" ] && grep -Eq 'pm-server hook|pmlens hook' "$settings" 2>/dev/null; then
  exit 0   # a manual `pm-server hook`/`pmlens hook` post-tool-use will fire — do not double up
fi

# --- 3. directive -------------------------------------------------------------
directive="pm-server plugin: a git commit just completed. Run the post-commit ritual through the pm-server MCP tools: pm_update_task (mark any finished task done), pm_log (record what was accomplished), then pm_next (surface the recommended next tasks). Surface any tool warnings[] to the user verbatim."

if command -v jq >/dev/null 2>&1; then
  jq -n --arg c "$directive" \
    '{hookSpecificOutput: {hookEventName: "PostToolUse", additionalContext: $c}}'
else
  # Fallback: PostToolUse stdout is injected as context even without the
  # structured envelope (mirrors the manual hook's flat additionalContext).
  printf '%s\n' "$directive"
fi
