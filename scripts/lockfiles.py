#!/usr/bin/env python3
"""Check, synchronize or refresh the dependency lockfiles (PMSERV-186).

uv.lock owns the resolution; requirements.lock is its hash-bearing export for
pip. Checking is offline and never changes either file or installs packages.
Refreshing explicitly permits upgrades; synchronizing retains existing pins
unless the project metadata requires a change. Requires Python 3.11+ and uv.
"""

from __future__ import annotations

import argparse
import subprocess
import sys
from pathlib import Path

UV_VERSION = "0.11.25"
REPO_ROOT = Path(__file__).resolve().parents[1]
HEADER = (
    "# Generated from uv.lock by: python scripts/lockfiles.py sync\n"
    "# Refresh versions with: python scripts/lockfiles.py refresh\n"
    "# Do not edit by hand. All runtime dependencies, extras and groups are included.\n"
)


def _run(uv: str, args: list[str], root: Path) -> str:
    result = subprocess.run([uv, *args], cwd=root, check=True, capture_output=True, text=True)
    return result.stdout


def _check_uv(uv: str, root: Path) -> None:
    version = _run(uv, ["--version"], root).split()
    if len(version) < 2 or version[:2] != ["uv", UV_VERSION]:
        raise ValueError(
            f"Use uv=={UV_VERSION} (the CI/exporter version); found {' '.join(version)}"
        )


def _export(uv: str, root: Path) -> str:
    return HEADER + _run(
        uv,
        [
            "export",
            "--locked",
            "--offline",
            "--no-cache",
            "--no-python-downloads",
            "--python",
            sys.executable,
            "--all-extras",
            "--all-groups",
            "--no-emit-project",
            "--no-header",
            "--no-annotate",
            "--format",
            "requirements.txt",
        ],
        root,
    )


def main(argv: list[str] | None = None, *, root: Path = REPO_ROOT) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--uv", default="uv", help=f"Path to uv {UV_VERSION}")
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("check", help="Offline, read-only check of both locks against the project")
    commands.add_parser("sync", help="Synchronize both locks, preserving compatible existing pins")
    refresh = commands.add_parser("refresh", help="Upgrade dependencies and regenerate both locks")
    refresh.add_argument(
        "--package",
        action="append",
        help="Upgrade only this package (repeatable; accepts name==version)",
    )
    args = parser.parse_args(argv)
    root = root.resolve()
    try:
        _check_uv(args.uv, root)
        if args.command != "check":
            lock_args = ["lock", "--no-python-downloads", "--python", sys.executable]
            if args.command == "refresh":
                if args.package:
                    for package in args.package:
                        lock_args.extend(["--upgrade-package", package])
                else:
                    lock_args.append("--upgrade")
            _run(args.uv, lock_args, root)
        expected = _export(args.uv, root)
        target = root / "requirements.lock"
        if args.command == "check":
            if not target.exists() or target.read_text(encoding="utf-8") != expected:
                print(
                    "requirements.lock differs from the uv.lock export. "
                    "Run `python scripts/lockfiles.py sync`, review and commit both lockfiles.",
                    file=sys.stderr,
                )
                return 1
            print("Lockfiles match pyproject.toml and each other (offline check).")
        else:
            target.write_text(expected, encoding="utf-8", newline="\n")
            print("Lockfiles synchronized. Review both files and run the hash-verified tests.")
    except subprocess.CalledProcessError as exc:
        detail = (exc.stderr or exc.stdout or str(exc)).strip()
        print(f"Lockfile command failed: {detail}", file=sys.stderr)
        return 1
    except (OSError, ValueError) as exc:
        print(f"Lockfile command failed: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
