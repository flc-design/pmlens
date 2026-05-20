#!/usr/bin/env python3
"""Build the pm-server .mcpb bundle.

A .mcpb bundle (Claude Desktop/Cowork distribution format) is a ZIP archive
whose root contains ``manifest.json`` (MCPB spec v0.4). For ``server.type =
"uv"`` the host fetches the Python package from PyPI at install time, so the
bundle ships only the manifest plus a couple of human-facing files.

This script validates:
  * ``manifest.json`` is parseable JSON.
  * its ``version`` field matches the ``[project] version`` in pyproject.toml
    (catches the dogfooding drift seen with .pm/project.yaml; see memory:135).
  * ``server.type`` is ``"uv"`` and ``server.mcp_config.env.PM_LENS`` is ``"1"``
    (ADR-014/017 + ADR-018 amendment). The env lives under ``server.mcp_config``
    per the MCPB spec — host runtimes read env from there, not from a top-level
    ``environment`` block (which is not in the documented schema).

Then it writes ``dist/pm-server-<version>.mcpb`` containing manifest.json,
README.md, and LICENSE.

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
DIST_DIR = REPO_ROOT / "dist"


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
    """Raise SystemExit with a clear message if any invariant is violated."""
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
        errors.append(f"server.type must be 'uv' (MCPB v0.4 + ADR-014), got {server.get('type')!r}")
    uv = server.get("uv") or {}
    if uv.get("package") != "pm-server":
        errors.append(f"server.uv.package must be 'pm-server', got {uv.get('package')!r}")
    if uv.get("command") != "pm-server" or uv.get("args") != ["serve"]:
        errors.append(
            "server.uv must invoke `pm-server serve` "
            f"(got command={uv.get('command')!r} args={uv.get('args')!r})"
        )

    mcp_config = server.get("mcp_config") or {}
    env = mcp_config.get("env") or {}
    if env.get("PM_LENS") != "1":
        errors.append(
            "server.mcp_config.env.PM_LENS must be '1' to enforce Lens mode at bundle install "
            "(ADR-017 + ADR-018: this is policy, not a hard security boundary)"
        )

    if errors:
        raise SystemExit("manifest.json validation failed:\n  - " + "\n  - ".join(errors))


def build_bundle(version: str) -> Path:
    DIST_DIR.mkdir(parents=True, exist_ok=True)
    out_path = DIST_DIR / f"pm-server-{version}.mcpb"
    payload_files = [
        MANIFEST_PATH,
        REPO_ROOT / "README.md",
        REPO_ROOT / "LICENSE",
    ]
    with zipfile.ZipFile(out_path, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        for path in payload_files:
            if not path.exists():
                raise SystemExit(f"required bundle file missing: {path}")
            zf.write(path, arcname=path.name)
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
