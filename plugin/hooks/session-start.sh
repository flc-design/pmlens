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
#   3. Branch surface — read .git/HEAD as text (never `git`) and tell the model
#      to pass track="<branch>" to pm_recall (branch-aware continuity, ADR-028).
#   4. Directive — inject the session-start ritual as SessionStart
#      additionalContext (instruct the model to call the MCP tools itself).
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
# Dual-recognition (PMSERV-137): a manual registration may live under the new
# `pmlens` key or the legacy `pm-server` key — probe BOTH and report the match.
dup_key=""
if command -v claude >/dev/null 2>&1; then
  if $mcp_timeout claude mcp get pmlens >/dev/null 2>&1; then
    dup_key="pmlens"
  elif $mcp_timeout claude mcp get pm-server >/dev/null 2>&1; then
    dup_key="pm-server"
  fi
fi
if [ -n "$dup_key" ]; then
  dup_warning="WARNING: $dup_key is also registered manually (claude mcp). This plugin bundles its own PM Lens MCP; tool names are NOT namespaced by server, so you now have duplicate pm_* tools. Run 'claude mcp remove $dup_key' to drop the manual registration and rely on the plugin."
fi

# --- 3. branch surface (PMSERV-124 / ADR-028) --------------------------------
# Branch-aware continuity: surface the current git branch so the model can pass
# track="<branch>" to pm_recall and restore THIS line's context. We read
# .git/HEAD as TEXT and never run `git` — a hostile .git/config could execute
# code via `git rev-parse` (CVE-2026-45033 / git config-exec class), so we
# mirror discovery.py's deliberate text-parse policy. This is a single cheap,
# local, unambiguous fact (unlike project status, which is why the hook still
# refuses to self-compute status above). Detection mirrors the save path
# (read_git_branch): walk up to the FIRST .git, stop there, and emit no branch
# for a worktree (.git file) — so we never surface a branch the save path won't
# record. Worst case is no note, never a wrong note. cwd is carried in the
# SessionStart hook JSON.
cwd=""
if command -v jq >/dev/null 2>&1; then
  cwd="$(printf '%s' "$input" | jq -r '.cwd // empty' 2>/dev/null || true)"
fi
if [ -z "$cwd" ]; then
  cwd="$(printf '%s' "$input" | grep -o '"cwd"[^,}]*' | head -1 | cut -d'"' -f4 || true)"
fi
[ -z "$cwd" ] && cwd="$PWD"

branch=""
dir="$cwd"
for _ in 1 2 3 4 5 6 7 8; do
  if [ -e "$dir/.git" ]; then
    # First .git wins, and we STOP here. A worktree/submodule .git is a FILE;
    # we deliberately do NOT follow it or walk past it into an enclosing repo —
    # that matches discovery.read_git_branch (the save path), so the surfaced
    # branch and the recorded branch agree. For a worktree this means no branch
    # note (per-directory .pm isolation handles continuity).
    if [ -d "$dir/.git" ] && [ -f "$dir/.git/HEAD" ]; then
      head_line="$(head -n1 "$dir/.git/HEAD" 2>/dev/null || true)"
      case "$head_line" in
        "ref: refs/heads/"*) branch="${head_line#ref: refs/heads/}" ;;
      esac
    fi
    break
  fi
  parent="$(dirname "$dir")"
  [ "$parent" = "$dir" ] && break
  dir="$parent"
done
# Strip a trailing CR so a CRLF .git/HEAD (core.autocrlf / hand-edited) yields
# "main" not "main\r" — the Python parser .strip()s, so the saved branch has no
# CR; without this the track= the model passes would never match the saved row.
branch="${branch%$'\r'}"

branch_note=""
if [ -n "$branch" ]; then
  branch_note=" The current git branch is \`$branch\`; pass track=\"$branch\" to pm_recall to restore this work line's context (branch-aware continuity, ADR-028), and re-pass it after any git checkout during the session."
fi

# --- 4. session-start directive ----------------------------------------------
# We deliberately do NOT compute project context in the hook itself. pm-server
# is not reliably on PATH here, and even when it is it may resolve a different
# data store (HOME) than the bundled MCP — so a hook-computed status could be
# from the wrong project. Instead we instruct the model to run the ritual
# through the (correctly-scoped) MCP tools — the same contract CLAUDE.md uses.
directive="pm-server plugin active. Begin this session with the pm-server ritual BEFORE your first reply: call pm_status (project state + warnings), pm_next (top 3 tasks), and pm_recall (restore prior-session context).${branch_note} Surface any blockers, overdue items, or tool warnings[] to the user verbatim."

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
