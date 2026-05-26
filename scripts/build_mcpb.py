#!/usr/bin/env python3
"""Build the pm-server .mcpb bundle (MCPB v0.4, server.type = "uv").

A .mcpb bundle (Claude Desktop / Cowork distribution format) is a ZIP archive
whose root contains ``manifest.json`` conforming to the MCPB spec. For
``server.type = "uv"`` (v0.4+) the host runs the bundled entry script via
``uv run --directory ${__dirname} <entry>``; uv installs the dependencies
declared in the bundled ``pyproject.toml`` into an isolated environment at
launch time. That means the bundle MUST include:

  * ``manifest.json``       — points at ``server.entry_point``
  * ``pyproject.toml``      — declares runtime dependencies
  * ``src/pm_server/`` …    — the actual Python package (no ``__pycache__``)
  * ``.mcpbignore``         — exclusion rules
  * ``README.md`` / ``LICENSE`` — user-facing metadata

This script validates the manifest against the v0.4 schema (the previous
``server.uv = {package, command, args}`` shape was an unreleased preview —
Claude Desktop's Zod validator rejects it with "Unrecognized key(s) in object:
'uv'"; see PMSERV-106). It then writes ``dist/pm-server-<version>.mcpb``.

Run from the repo root: ``python scripts/build_mcpb.py``.
"""

from __future__ import annotations

import json
import sys
import tomllib
import zipfile
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
MANIFEST_PATH = REPO_ROOT / "manifest.json"
PYPROJECT_PATH = REPO_ROOT / "pyproject.toml"
MCPBIGNORE_PATH = REPO_ROOT / ".mcpbignore"
PACKAGE_SRC = REPO_ROOT / "src" / "pm_server"
DIST_DIR = REPO_ROOT / "dist"

ALLOWED_PLATFORMS = {"darwin", "linux", "win32"}


def _read_manifest() -> dict:
    if not MANIFEST_PATH.exists():
        raise SystemExit(f"manifest.json not found at {MANIFEST_PATH}")
    try:
        return json.loads(MANIFEST_PATH.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise SystemExit(f"manifest.json is not valid JSON: {exc}") from exc


def _read_pyproject_version() -> str:
    with open(PYPROJECT_PATH, "rb") as f:
        data = tomllib.load(f)
    try:
        return data["project"]["version"]
    except KeyError as exc:
        raise SystemExit("pyproject.toml is missing [project] version") from exc


def validate(manifest: dict, pyproject_version: str) -> None:
    """Raise SystemExit with a clear message if any invariant is violated.

    Pinned against MCPB v0.4 spec (https://github.com/anthropics/mcpb).
    """
    errors: list[str] = []

    if manifest.get("manifest_version") != "0.4":
        errors.append(f"manifest_version must be '0.4', got {manifest.get('manifest_version')!r}")

    m_version = manifest.get("version")
    if m_version != pyproject_version:
        errors.append(
            f"manifest.json version {m_version!r} != pyproject.toml version "
            f"{pyproject_version!r} — bump both in lockstep"
        )

    server = manifest.get("server") or {}
    if server.get("type") != "uv":
        errors.append(f"server.type must be 'uv' (MCPB v0.4, ADR-014), got {server.get('type')!r}")

    if "uv" in server:
        errors.append(
            "server.uv = {...} is NOT in MCPB v0.4 schema; Claude Desktop's Zod "
            "validator rejects it with `Unrecognized key(s) in object: 'uv'` "
            "(PMSERV-106). Remove this nested block — the runtime invocation "
            "lives in server.mcp_config.command + args."
        )

    entry_point = server.get("entry_point")
    if not entry_point or not isinstance(entry_point, str):
        errors.append(
            f"server.entry_point is required for server.type='uv' (v0.4); got {entry_point!r}"
        )
    elif not (REPO_ROOT / entry_point).exists():
        errors.append(
            f"server.entry_point {entry_point!r} does not exist at "
            f"{REPO_ROOT / entry_point}; the bundle would point to a missing file."
        )

    mcp_config = server.get("mcp_config") or {}
    if mcp_config.get("command") != "uv":
        errors.append(
            "server.mcp_config.command must be 'uv' (host invokes `uv run`), "
            f"got {mcp_config.get('command')!r}"
        )
    args = mcp_config.get("args") or []
    if "${__dirname}" not in args:
        errors.append(
            "server.mcp_config.args must include '${__dirname}' so the host "
            f"resolves the entry script relative to the install dir (got {args!r})"
        )
    if not ("-m" in args and "pm_server" in args):
        errors.append(
            "server.mcp_config.args must invoke the package via `python -m pm_server` "
            "(NOT `python src/pm_server/__main__.py`). Direct script execution loses "
            "the package context and dies on `from . import __version__` with "
            f"`attempted relative import with no known parent package` (got {args!r})"
        )
    if "serve" not in args:
        errors.append(f"server.mcp_config.args must end with the `serve` subcommand (got {args!r})")

    env = mcp_config.get("env") or {}
    if env.get("PM_LENS") != "1":
        errors.append(
            "server.mcp_config.env.PM_LENS must be '1' so installs default to "
            "Lens (ADR-017 + ADR-018: policy, not a hard security boundary)"
        )

    compat = manifest.get("compatibility") or {}
    platforms = compat.get("platforms") or []
    bad_platforms = [p for p in platforms if p not in ALLOWED_PLATFORMS]
    if bad_platforms:
        errors.append(
            f"compatibility.platforms contains invalid values {bad_platforms!r}; "
            f"allowed: {sorted(ALLOWED_PLATFORMS)}. Use 'darwin' for macOS, "
            "'win32' for Windows, 'linux' for Linux (matches Node.js "
            "process.platform — Claude Desktop's Zod validator rejects "
            "'macos'/'windows' — PMSERV-106)."
        )

    for legacy in ("environment", "permissions"):
        if legacy in manifest:
            errors.append(
                f"top-level {legacy!r} is NOT in MCPB v0.4 schema (memory:143 / "
                "FINDING-D). Env vars belong under server.mcp_config.env; "
                "permissions are structurally enforced by code (RO_ALLOWLIST + "
                "SQLite mode=ro), not by a declarative manifest field."
            )

    if errors:
        raise SystemExit("manifest.json validation failed:\n  - " + "\n  - ".join(errors))


def _iter_package_sources(package_root: Path):
    """Yield each shipped file under ``src/pm_server/`` (skips caches/bytecode)."""
    for path in sorted(package_root.rglob("*")):
        if path.is_dir():
            continue
        if any(part == "__pycache__" for part in path.parts):
            continue
        if path.suffix in {".pyc", ".pyo"}:
            continue
        yield path


def build_bundle(version: str) -> Path:
    DIST_DIR.mkdir(parents=True, exist_ok=True)
    out_path = DIST_DIR / f"pm-server-{version}.mcpb"

    top_files = [
        MANIFEST_PATH,
        REPO_ROOT / "README.md",
        REPO_ROOT / "LICENSE",
        PYPROJECT_PATH,
        MCPBIGNORE_PATH,
    ]

    if not PACKAGE_SRC.exists():
        raise SystemExit(f"package source missing: {PACKAGE_SRC}")

    with zipfile.ZipFile(out_path, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        for path in top_files:
            if not path.exists():
                raise SystemExit(f"required bundle file missing: {path}")
            zf.write(path, arcname=str(path.relative_to(REPO_ROOT)))

        for src_path in _iter_package_sources(PACKAGE_SRC):
            zf.write(src_path, arcname=str(src_path.relative_to(REPO_ROOT)))

    return out_path


def main() -> int:
    manifest = _read_manifest()
    pyproject_version = _read_pyproject_version()
    validate(manifest, pyproject_version)
    out = build_bundle(pyproject_version)
    size_kib = out.stat().st_size / 1024
    print(f"Built {out.name} ({size_kib:.1f} KiB) at {out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
