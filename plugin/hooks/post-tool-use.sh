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

# --- 1. only act on `git commit` run as a command -----------------------------
# The reminder reaches the model, so a false match is a false "a commit just
# completed" claim. Read ONLY tool_input.command — never the whole payload,
# whose tool_response can contain text such as `git status`'s
# '(use "git add" and/or "git commit -a")' — and require `git ... commit` in
# command position, mirroring hooks.is_git_commit.
command_str=""
cwd=""
if command -v jq >/dev/null 2>&1; then
  command_str="$(printf '%s' "$input" | jq -r '.tool_input.command // empty' 2>/dev/null || true)"
  cwd="$(printf '%s' "$input" | jq -r '.cwd // empty' 2>/dev/null || true)"
else
  # Claude Code serialises tool_input before tool_response, so the first
  # "command" key is tool_input.command. Undo the JSON escapes that matter here.
  if [[ $input =~ \"command\"[[:space:]]*:[[:space:]]*\"(([^\"\\]|\\.)*)\" ]]; then
    command_str="${BASH_REMATCH[1]}"
    command_str="${command_str//\\n/$'\n'}"
    command_str="${command_str//\\\"/\"}"
  fi
  cwd="$(printf '%s' "$input" | grep -o '"cwd"[^,}]*' | head -1 | cut -d'"' -f4 || true)"
fi
[ -n "$command_str" ] || exit 0

segments="${command_str//&&/$'\n'}"
segments="${segments//||/$'\n'}"
segments="${segments//;/$'\n'}"
segments="${segments//|/$'\n'}"
is_commit=0
git_commit_re='^([^[:space:]]*/)?git([[:space:]]+(-[Cc][[:space:]]+[^[:space:]]+|--(git-dir|work-tree|namespace)[[:space:]]+[^[:space:]]+|-[^[:space:]]+))*[[:space:]]+commit([[:space:]]|$)'
while IFS= read -r segment; do
  segment="${segment#"${segment%%[![:space:]]*}"}"
  while [[ $segment =~ ^[A-Za-z_][A-Za-z0-9_]*=[^[:space:]]*[[:space:]]+(.*)$ ]]; do
    segment="${BASH_REMATCH[1]}"
  done
  if [[ $segment =~ $git_commit_re ]] && [[ $segment != *--dry-run* ]]; then
    is_commit=1
    break
  fi
done <<< "$segments"
[ "$is_commit" -eq 1 ] || exit 0

# --- 1b. only in a PM Lens project -------------------------------------------
# Walk up from the session cwd for .pm/project.yaml, like the pm_* tools do
# (PM_PROJECT_PATH first). Outside a PM Lens project there is nothing to record.
[ -n "$cwd" ] || cwd="$PWD"
pm_found=0
if [ -n "${PM_PROJECT_PATH:-}" ] && [ -f "$PM_PROJECT_PATH/.pm/project.yaml" ]; then
  pm_found=1
fi
dir="$cwd"
while [ "$pm_found" -eq 0 ]; do
  if [ -f "$dir/.pm/project.yaml" ]; then pm_found=1; break; fi
  parent="$(dirname "$dir")"
  [ "$parent" = "$dir" ] && break
  dir="$parent"
done
[ "$pm_found" -eq 1 ] || exit 0

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
directive="pm-server plugin: a git commit just completed. If it finished a task that is not yet marked done, update it with pm_update_task and record it with pm_log; for an intermediate commit, change nothing. Tell the user about any warnings[] the tools return."

if command -v jq >/dev/null 2>&1; then
  jq -n --arg c "$directive" \
    '{hookSpecificOutput: {hookEventName: "PostToolUse", additionalContext: $c}}'
else
  # Claude Code ignores plain PostToolUse stdout, so the fallback must emit the
  # same envelope. The directive is a fixed string with no double quotes or
  # backslashes, so it can be embedded without escaping.
  printf '{"hookSpecificOutput":{"hookEventName":"PostToolUse","additionalContext":"%s"}}\n' "$directive"
fi
