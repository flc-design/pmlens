#!/usr/bin/env bash
# Launch Claude Code with the pm-server plugin in FULL ISOLATION from your live
# setup. Verified on Claude Code 2.1.161.
#
#   - CLAUDE_CONFIG_DIR -> sandbox: your live ~/.claude config (including the
#     globally-registered pm-server MCP) is INVISIBLE to this session.
#   - the bundled MCP runs LOCAL source with HOME -> sandbox, so pm-server's
#     ~/.pm (registry + memory.db) resolves under the sandbox; your live ~/.pm
#     is NOT touched.
#   - --plugin-dir loads the plugin for this session only (nothing installed).
#
# Nothing here writes to your live ~/.claude config or your live ~/.pm data.
#
# Usage:
#   plugin/dev/isolated-test.sh                 # interactive session
#   plugin/dev/isolated-test.sh -p "call pm_status and report"   # headless
#   PM_PLUGIN_SANDBOX=/tmp/foo plugin/dev/isolated-test.sh       # custom sandbox
set -euo pipefail

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
PLUGIN="$REPO/plugin"
SANDBOX="${PM_PLUGIN_SANDBOX:-/tmp/pm-plugin-sandbox}"

mkdir -p "$SANDBOX/claude-config" "$SANDBOX/pmhome" "$SANDBOX/testproj/.pm/daily"
if [ ! -f "$SANDBOX/testproj/.pm/project.yaml" ]; then
  cat > "$SANDBOX/testproj/.pm/project.yaml" <<YAML
name: sandboxproj
display_name: sandboxproj
version: 0.0.1
status: development
started: 2026-01-01
description: pm-server plugin isolation sandbox
phases: []
YAML
  printf '[]\n' > "$SANDBOX/testproj/.pm/tasks.yaml"
fi

# Dev MCP config: LOCAL source (so this exercises your UNCOMMITTED changes) +
# HOME -> sandbox. The committed plugin/.mcp.json uses `uvx pm-server@x.y`, which
# pulls the released build from PyPI instead.
cat > "$SANDBOX/dev.mcp.json" <<JSON
{
  "mcpServers": {
    "pm-server": {
      "command": "uv",
      "args": ["run", "--project", "$REPO", "pm-server", "serve"],
      "env": { "HOME": "$SANDBOX/pmhome", "PYTHONUNBUFFERED": "1" }
    }
  }
}
JSON

# Auth: CLAUDE_CONFIG_DIR fully isolates the config — including the login state
# (~/.claude.json oauthAccount). This is REQUIRED here: your live settings.json
# ships pm-server hooks that would otherwise run with your live HOME and touch
# your live ~/.pm. Provide auth WITHOUT leaking the live config one of two ways:
#   (a) headless:    export CLAUDE_CODE_OAUTH_TOKEN=<token>   # `claude setup-token`
#   (b) interactive: run this script with no -p, then `/login` once inside;
#                    the sandbox config caches it for later runs.
if [ -z "${CLAUDE_CODE_OAUTH_TOKEN:-}" ] \
   && ! grep -q oauthAccount "$SANDBOX/claude-config/.claude.json" 2>/dev/null; then
  echo "NOTE: this isolated sandbox is not authenticated yet."
  echo "  headless    -> run: claude setup-token   then: export CLAUDE_CODE_OAUTH_TOKEN=<token>"
  echo "  interactive -> launch without -p, then /login once (cached in the sandbox)."
  echo
fi

echo "Sandbox:        $SANDBOX"
echo "Live untouched: ~/.claude config + ~/.pm data"
echo "Launching isolated Claude Code session (cwd = sandbox testproj)…"
echo

cd "$SANDBOX/testproj"
exec env CLAUDE_CONFIG_DIR="$SANDBOX/claude-config" \
  claude \
    --plugin-dir "$PLUGIN" \
    --strict-mcp-config --mcp-config "$SANDBOX/dev.mcp.json" \
    "$@"
