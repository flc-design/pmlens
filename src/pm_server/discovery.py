"""Project auto-detection and information inference."""

from __future__ import annotations

import json
import os
import tomllib
from pathlib import Path

# Hard cap when reading .git/config as text (defensive against a
# pathologically large or crafted file). 1 MiB is far beyond any real
# git config.
_GIT_CONFIG_MAX_BYTES = 1_048_576

# PMSERV-081 (WF-025 R2, ADR-016): bound the discover_projects walk to
# avoid traversing dependency caches, large virtualenvs, and the user's
# global ~/.pm/ when scan_path lands near $HOME.
_DISCOVERY_MAX_DEPTH = 5

_DISCOVERY_EXCLUDED_DIRS: frozenset[str] = frozenset(
    {
        ".git",
        "node_modules",
        "__pycache__",
        ".venv",
        "venv",
        ".tox",
        ".mypy_cache",
        ".pytest_cache",
        ".ruff_cache",
        "dist",
        "build",
        "target",  # Rust / Cargo
        ".next",
        ".nuxt",
        ".gradle",
        ".idea",
        ".vscode",
    }
)


def _read_git_remote_origin_url(project_path: Path) -> str | None:
    """Return the ``origin`` remote URL by parsing ``.git/config`` directly.

    This deliberately does **not** shell out to ``git``. Running ``git`` on a
    possibly-untrusted working tree lets a malicious ``.git/config`` (e.g.
    ``core.fsmonitor``, ``core.sshCommand``, ``core.hookspath``, ``core.pager``)
    execute arbitrary commands during ordinary operations such as
    ``git remote get-url`` — the CVE-2026-45033 / git config-exec class.
    Parsing the file as plain text cannot execute code: the worst case is
    returning ``None``.

    Returns ``None`` when the URL cannot be determined safely: no repo, a
    ``.git`` *file* (worktree/submodule pointer — not followed, to avoid an
    attacker-influenced ``gitdir:`` redirect), an unreadable/oversized
    config, or no ``origin`` remote.
    """
    git_dir = project_path / ".git"
    if not git_dir.is_dir():
        return None
    config_file = git_dir / "config"
    try:
        if config_file.stat().st_size > _GIT_CONFIG_MAX_BYTES:
            return None
        text = config_file.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None

    in_origin = False
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line[0] in "#;":
            continue
        if line.startswith("["):
            end = line.find("]")
            header = line[1:end].strip().lower() if end != -1 else ""
            # git writes ``[remote "origin"]``; tolerate ``[remote.origin]``.
            in_origin = header in ('remote "origin"', "remote.origin")
            continue
        if in_origin and "=" in line:
            key, _, value = line.partition("=")
            if key.strip().lower() == "url":
                url = value.strip()
                if len(url) >= 2 and url[0] == '"' and url[-1] == '"':
                    url = url[1:-1]
                return url or None
    return None


def detect_project_info(project_path: Path) -> dict:
    """Detect project metadata from common config files.

    Checks Cargo.toml, package.json, pyproject.toml, git remote, and README.md
    to infer project name, version, description, and repository URL.
    """
    info: dict = {
        "name": project_path.name,
        "display_name": project_path.name.replace("-", " ").replace("_", " ").title(),
        "version": "0.1.0",
        "repository": None,
        "description": "",
    }

    # Cargo.toml (Rust)
    cargo_toml = project_path / "Cargo.toml"
    if cargo_toml.exists():
        try:
            with open(cargo_toml, "rb") as f:
                cargo = tomllib.load(f)
            pkg = cargo.get("package", cargo.get("workspace", {}).get("package", {}))
            if pkg:
                info["name"] = pkg.get("name", info["name"])
                info["version"] = pkg.get("version", info["version"])
                info["description"] = pkg.get("description", "") or ""
        except Exception:
            pass

    # package.json (Node.js)
    pkg_json = project_path / "package.json"
    if pkg_json.exists():
        try:
            pkg = json.loads(pkg_json.read_text(encoding="utf-8"))
            info["name"] = pkg.get("name", info["name"])
            info["version"] = pkg.get("version", info["version"])
            info["description"] = pkg.get("description", "") or ""
        except (json.JSONDecodeError, UnicodeDecodeError):
            pass

    # pyproject.toml (Python)
    pyproject = project_path / "pyproject.toml"
    if pyproject.exists():
        try:
            with open(pyproject, "rb") as f:
                pyp = tomllib.load(f)
            proj = pyp.get("project", {})
            if proj:
                info["name"] = proj.get("name", info["name"])
                info["version"] = proj.get("version", info["version"])
                info["description"] = proj.get("description", "") or ""
        except Exception:
            pass

    # Git remote URL — parse .git/config as text; never shell out to ``git``
    # on a possibly-untrusted tree (CVE-2026-45033 / git config-exec class).
    repo_url = _read_git_remote_origin_url(project_path)
    if repo_url:
        info["repository"] = repo_url

    # README.md fallback for description
    readme = project_path / "README.md"
    if readme.exists() and not info["description"]:
        try:
            lines = readme.read_text(encoding="utf-8").splitlines()
            for line in lines:
                stripped = line.strip().lstrip("# ").strip()
                if stripped and not stripped.startswith("!") and len(stripped) > 10:
                    info["description"] = stripped[:200]
                    break
        except UnicodeDecodeError:
            pass

    return info


def discover_projects(
    scan_path: Path,
    *,
    max_depth: int = _DISCOVERY_MAX_DEPTH,
) -> list[dict]:
    """Recursively scan for projects with ``.pm/project.yaml``.

    The walk is bounded to defend against pathological inputs and to avoid
    visiting locations that are not project roots:

    * **max_depth** caps how deep we descend below ``scan_path``. Defaults
      to ``_DISCOVERY_MAX_DEPTH`` (5).
    * **Excluded directory names** (``.git``, ``node_modules``,
      virtualenv/cache dirs, IDE config dirs) are pruned from descent so we
      never walk through dependency trees or VCS internals.
    * The user's **global ``~/.pm/``** is explicitly skipped (ADR-016):
      its layout — registry.yaml + memory.db — is not a project root and
      must never be enumerated as one, especially under Desktop/Cowork.
    * **Symlinks** are not followed (``os.walk(followlinks=False)``) to
      prevent cycles and escapes via attacker-influenced links.
    """
    found: list[dict] = []
    scan_path = scan_path.expanduser().resolve()

    if not scan_path.is_dir():
        return found

    try:
        global_pm = (Path.home() / ".pm").resolve()
    except OSError:
        global_pm = None

    for current, dirnames, _filenames in os.walk(scan_path, followlinks=False):
        current_path = Path(current)
        try:
            rel = current_path.resolve().relative_to(scan_path)
        except (ValueError, OSError):
            dirnames[:] = []
            continue
        depth = 0 if rel == Path(".") else len(rel.parts)

        pm_subdir = current_path / ".pm"
        if ".pm" in dirnames and (pm_subdir / "project.yaml").exists():
            is_global = False
            if global_pm is not None:
                try:
                    is_global = pm_subdir.resolve() == global_pm
                except OSError:
                    is_global = False
            if not is_global:
                found.append({"path": str(current_path), "name": current_path.name})

        pruned: list[str] = []
        for d in dirnames:
            if d in _DISCOVERY_EXCLUDED_DIRS or d == ".pm":
                continue
            if depth + 1 > max_depth:
                continue
            if global_pm is not None:
                try:
                    if (current_path / d).resolve() == global_pm:
                        continue
                except OSError:
                    continue
            pruned.append(d)
        dirnames[:] = pruned

    return found
