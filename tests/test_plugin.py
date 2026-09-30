"""Tests for the Claude Code plugin (``plugin/``) and root ``marketplace.json``.

The plugin is shell + JSON (no Python), so these tests:

1. Validate the marketplace/plugin JSON and their cross-file consistency
   (the marketplace entry must point at the real ``plugin/`` dir and agree
   with ``plugin.json``).
2. Exercise the PostToolUse shell hook via subprocess across its branches:
   git-commit -> directive, non-commit -> silent, manual settings.json hook
   present -> defer (double-fire guard), and the jq-less fallback path.

Pure stdlib. The hooks must work without ``jq`` (the bundled MCP user may not
have it), so we explicitly test that path with a restricted ``PATH``.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
PLUGIN_DIR = REPO_ROOT / "plugin"
MARKETPLACE = REPO_ROOT / ".claude-plugin" / "marketplace.json"
PLUGIN_MANIFEST = PLUGIN_DIR / ".claude-plugin" / "plugin.json"
HOOKS_JSON = PLUGIN_DIR / "hooks" / "hooks.json"
POST_HOOK = PLUGIN_DIR / "hooks" / "post-tool-use.sh"
SESSION_HOOK = PLUGIN_DIR / "hooks" / "session-start.sh"
PLUGIN_MCP = PLUGIN_DIR / ".mcp.json"
PLUGIN_README = PLUGIN_DIR / "README.md"


def _post_input(command: str, cwd: Path, **extra) -> str:
    """A PostToolUse payload shaped like Claude Code's (tool_input first)."""
    return json.dumps({"cwd": str(cwd), "tool_input": {"command": command}, **extra})


def _load(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


# ─── JSON structure & cross-file consistency ──────────────────────────────────


def test_marketplace_json_well_formed():
    mp = _load(MARKETPLACE)
    assert mp["name"], "marketplace name is the @<name> install suffix — required"
    assert isinstance(mp["owner"], dict) and mp["owner"].get("name")
    assert isinstance(mp["plugins"], list) and mp["plugins"]


def test_marketplace_entry_points_at_plugin_dir_and_matches_manifest():
    mp = _load(MARKETPLACE)
    manifest = _load(PLUGIN_MANIFEST)
    entry = next(p for p in mp["plugins"] if p["name"] == manifest["name"])
    src = entry["source"]
    assert isinstance(src, str), "in-repo plugin source must be a relative path string"
    assert (REPO_ROOT / src).resolve() == PLUGIN_DIR.resolve()
    assert (REPO_ROOT / src / ".claude-plugin" / "plugin.json").is_file()


def test_marketplace_name_distinct_from_plugin_name():
    # `/plugin install <plugin>@<marketplace>` reads confusingly if identical.
    mp = _load(MARKETPLACE)
    manifest = _load(PLUGIN_MANIFEST)
    assert mp["name"] != manifest["name"]


def test_hooks_json_registers_both_events():
    hooks = _load(HOOKS_JSON)["hooks"]
    assert "SessionStart" in hooks
    assert "PostToolUse" in hooks
    ptu = hooks["PostToolUse"][0]
    assert ptu["matcher"] == "Bash"
    cmd = ptu["hooks"][0]["command"]
    assert cmd.endswith("post-tool-use.sh")
    assert "${CLAUDE_PLUGIN_ROOT}" in cmd


def test_hook_scripts_present_and_executable():
    for script in (POST_HOOK, SESSION_HOOK):
        assert script.is_file()
        assert os.access(script, os.X_OK), f"{script.name} must be executable"


# ─── PostToolUse shell behaviour ──────────────────────────────────────────────


def _run_post_hook(
    stdin: str, *, config_dir: Path, no_jq: bool = False
) -> subprocess.CompletedProcess:
    env = dict(os.environ)
    env["CLAUDE_CONFIG_DIR"] = str(config_dir)
    if no_jq:
        # Restrict PATH to the coreutils the hook needs, excluding jq, so the
        # `command -v jq` probe fails and the printf fallback is exercised.
        bindir = config_dir / "_bin"
        bindir.mkdir(exist_ok=True)
        for tool in ("bash", "env", "cat", "grep", "head", "cut", "dirname", "sed", "awk"):
            real = shutil.which(tool)
            if real is None:
                pytest.skip(f"cannot build jq-less PATH: {tool} not found")
            link = bindir / tool
            if not link.exists():
                link.symlink_to(real)
        env["PATH"] = str(bindir)
    return subprocess.run(
        ["bash", str(POST_HOOK)],
        input=stdin,
        capture_output=True,
        text=True,
        env=env,
        timeout=20,
    )


@pytest.fixture
def pm_project(tmp_path: Path) -> Path:
    """A PM Lens project: the hooks only speak inside one."""
    root = tmp_path / "proj"
    (root / ".pm").mkdir(parents=True)
    (root / ".pm" / "project.yaml").write_text("name: proj\n", encoding="utf-8")
    return root


@pytest.fixture
def empty_config(tmp_path: Path) -> Path:
    d = tmp_path / "empty"
    d.mkdir()
    (d / "settings.json").write_text('{"hooks":{}}', encoding="utf-8")
    return d


@pytest.fixture
def manual_config(tmp_path: Path) -> Path:
    """Config dir whose settings.json carries the manual pm-server hook."""
    d = tmp_path / "manual"
    d.mkdir()
    settings = {
        "hooks": {
            "PostToolUse": [
                {
                    "matcher": "Bash",
                    "hooks": [{"type": "command", "command": "/x/pm-server hook post-tool-use"}],
                }
            ]
        }
    }
    (d / "settings.json").write_text(json.dumps(settings), encoding="utf-8")
    return d


def _post_tool_use_context(stdout: str) -> str:
    """Parse the envelope Claude Code reads and return its additionalContext.

    Claude Code ignores plain PostToolUse stdout and a top-level
    ``additionalContext`` key, so both the jq path and the jq-less fallback
    must produce exactly this shape (PMSERV-196).
    """
    hook_out = json.loads(stdout)["hookSpecificOutput"]
    assert hook_out["hookEventName"] == "PostToolUse"
    return hook_out["additionalContext"]


def test_directive_emitted_on_git_commit(empty_config: Path, pm_project: Path):
    r = _run_post_hook(_post_input('git commit -m "msg"', pm_project), config_dir=empty_config)
    assert r.returncode == 0
    context = _post_tool_use_context(r.stdout)
    assert "pm_update_task" in context
    assert "pm_log" in context
    # An intermediate commit must not be read as "mark tasks done".
    assert "intermediate commit" in context
    # The next-task listing is not a post-commit duty (ADR-054).
    assert "pm_next" not in context


def test_silent_on_non_commit(empty_config: Path):
    r = _run_post_hook('{"tool_input":{"command":"ls -la"}}', config_dir=empty_config)
    assert r.returncode == 0
    assert r.stdout.strip() == ""


def test_defers_when_manual_hook_present(manual_config: Path, pm_project: Path):
    """Double-fire guard: a manual settings.json hook -> emit nothing."""
    r = _run_post_hook(_post_input('git commit -m "msg"', pm_project), config_dir=manual_config)
    assert r.returncode == 0
    assert r.stdout.strip() == ""


def test_directive_emitted_without_jq(empty_config: Path, pm_project: Path):
    r = _run_post_hook(
        _post_input("git commit -m wip", pm_project), config_dir=empty_config, no_jq=True
    )
    assert r.returncode == 0
    # The printf fallback must emit the same envelope as the jq path; a bare
    # line would be dropped by Claude Code.
    assert "pm_update_task" in _post_tool_use_context(r.stdout)


# ─── Plugin version drift guard — MOVED (PMSERV-133 → PMSERV-172) ─────────────
#
# The plugin pins pm-server's version across several surfaces that must all move
# in lockstep with pyproject.toml on every release. Two incidents shaped that
# guard: the v0.10.0 release skew (a plugin pin lagging main by many commits) and
# the v0.12.1 wrapper miss (PyPI rejected the rebuild with '400 File already
# exists', leaving the plugin's fresh `uvx pm-server@0.12.1` pin unresolvable).
#
# PMSERV-172 found the guard set itself incomplete — notably plugin/README.md's
# bare `latest 0.13.0` line, which the `pm-server@…`-anchored regex could never
# see, i.e. a *guarded file* with an *unguarded line*. All of it now lives in
# tests/test_version_lockstep.py, which additionally scans the whole release
# surface for unregistered pins. Do not re-add lockstep assertions here.


# ─── SessionStart shell behaviour ─────────────────────────────────────────────


def _run_session_hook(tmp_path: Path, *, branch: str, pm: bool = True) -> str:
    """Run session-start.sh in a repo on ``branch`` and return additionalContext.

    PATH is limited to the tools the script needs so the `claude mcp get`
    duplicate probe never touches the real Claude Code configuration.
    """
    repo = tmp_path / "repo"
    (repo / ".git").mkdir(parents=True)
    (repo / ".git" / "HEAD").write_text(f"ref: refs/heads/{branch}\n", encoding="utf-8")
    if pm:
        (repo / ".pm").mkdir()
        (repo / ".pm" / "project.yaml").write_text("name: repo\n", encoding="utf-8")
    bindir = tmp_path / "_bin"
    bindir.mkdir()
    for tool in ("bash", "cat", "grep", "head", "cut", "mkdir", "find", "dirname", "jq"):
        real = shutil.which(tool)
        if real is None:
            pytest.skip(f"cannot build a restricted PATH: {tool} not found")
        (bindir / tool).symlink_to(real)
    env = {"PATH": str(bindir), "CLAUDE_PLUGIN_DATA": str(tmp_path / "data")}
    stdin = json.dumps({"session_id": "s-1", "cwd": str(repo), "source": "startup"})
    r = subprocess.run(
        ["bash", str(SESSION_HOOK)],
        input=stdin,
        capture_output=True,
        text=True,
        env=env,
        timeout=20,
    )
    assert r.returncode == 0, r.stderr
    if not r.stdout.strip():
        return ""
    hook_out = json.loads(r.stdout)["hookSpecificOutput"]
    assert hook_out["hookEventName"] == "SessionStart"
    return hook_out["additionalContext"]


def test_session_directive_defers_timing_to_the_rule_file(tmp_path: Path):
    context = _run_session_hook(tmp_path, branch="feat/x")
    # The plugin meets rule files of every version, so it points at them
    # instead of restating a condition some of them contradict (ADR-054).
    assert "Follow the PM Lens section" in context
    assert "BEFORE your first reply" not in context
    assert "verbatim" not in context
    assert 'track="feat/x"' in context
    assert "re-read .git/HEAD" in context


def test_session_hook_is_silent_outside_a_pm_lens_project(tmp_path: Path):
    # It would otherwise claim "this project tracks tasks in PM Lens" in any
    # repository, contradicting the MCP instructions ("only where .pm/ exists").
    assert _run_session_hook(tmp_path, branch="main", pm=False) == ""


@pytest.mark.parametrize("no_jq", [False, True])
@pytest.mark.parametrize(
    "command",
    [
        'rg -n "git commit" docs',
        'echo "remember to git commit later"',
        "git commit --dry-run -m x",
        "git commit-tree HEAD^{tree} -m x",
        'echo "example; git commit -m x"',
        "git --help commit",
        "git commit --short",
        "cat <<EOF\ngit commit -m fake\nEOF",
    ],
)
def test_post_hook_ignores_text_that_is_not_a_commit(
    empty_config: Path, pm_project: Path, command: str, no_jq: bool
):
    r = _run_post_hook(_post_input(command, pm_project), config_dir=empty_config, no_jq=no_jq)
    assert r.returncode == 0
    assert r.stdout.strip() == ""


@pytest.mark.parametrize("no_jq", [False, True])
def test_post_hook_does_not_read_tool_output(empty_config: Path, pm_project: Path, no_jq: bool):
    # `git status` prints '(use "git add" and/or "git commit -a")'; only the
    # command itself may decide whether a commit happened.
    payload = _post_input(
        "git status",
        pm_project,
        tool_response={
            "stdout": 'no changes added to commit (use "git add" and/or "git commit -a")'
        },
    )
    r = _run_post_hook(payload, config_dir=empty_config, no_jq=no_jq)
    assert r.stdout.strip() == ""


@pytest.mark.parametrize("no_jq", [False, True])
@pytest.mark.parametrize(
    "command",
    ["cd sub && git commit -m x", "git -C . commit -m x", "GIT_EDITOR=true git commit --amend"],
)
def test_post_hook_recognises_real_commits(
    empty_config: Path, pm_project: Path, command: str, no_jq: bool
):
    r = _run_post_hook(_post_input(command, pm_project), config_dir=empty_config, no_jq=no_jq)
    assert "pm_update_task" in _post_tool_use_context(r.stdout)


def test_post_hook_is_silent_outside_a_pm_lens_project(empty_config: Path, tmp_path: Path):
    r = _run_post_hook(_post_input("git commit -m x", tmp_path), config_dir=empty_config)
    assert r.stdout.strip() == ""


@pytest.mark.parametrize(
    "path",
    [PLUGIN_DIR / "skills" / "pm" / "SKILL.md", REPO_ROOT / "skill" / "SKILL.md"],
)
def test_skills_do_not_contradict_the_rule_template(path: Path):
    # ADR-054: the plugin cannot ship CLAUDE.md, so its skill is where the
    # plugin user reads the rules. It must not bring back what v15 removed.
    text = path.read_text(encoding="utf-8")
    assert "アトミックコミットを作成" not in text
    assert "最初の発話の前" not in text
    assert "/clear 前に" not in text


@pytest.mark.parametrize("no_jq", [False, True])
def test_post_hook_ignores_a_commit_in_another_repository(
    empty_config: Path, pm_project: Path, tmp_path: Path, no_jq: bool
):
    other = tmp_path / "other"
    other.mkdir()
    for command in (f"git -C {other} commit -m x", f"cd {other} && git commit -m x"):
        r = _run_post_hook(_post_input(command, pm_project), config_dir=empty_config, no_jq=no_jq)
        assert r.stdout.strip() == "", command


def test_post_hook_names_the_project_the_commit_went_to(
    empty_config: Path, pm_project: Path, tmp_path: Path
):
    other = tmp_path / "other"
    (other / ".pm").mkdir(parents=True)
    (other / ".pm" / "project.yaml").write_text("name: other\n", encoding="utf-8")
    r = _run_post_hook(
        _post_input(f"cd {other} && git commit -m x", pm_project), config_dir=empty_config
    )
    assert f"project at {other}" in _post_tool_use_context(r.stdout)
