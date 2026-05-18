"""GitHub Actions full-SHA-pin guardrail (PMSERV-074, ADR-013).

Every `uses:` in `.github/workflows/*.yml` must reference a third-party
action by a full 40-hex commit SHA, not a mutable tag like `@v4`. A
mutable tag lets the action owner -- or an account compromise -- change
the code a workflow runs; closing that supply-chain attack surface on
the pipeline that publishes to PyPI is what KR-008 Phase C did.

Without this test a future PR (or a careless Dependabot config change)
could reintroduce `@v4` and CI would stay green -- the pin would rot
silently, which is exactly the failure mode PMSERV-056's smoke tests
were created to prevent for packaging. Same philosophy, applied to the
release pipeline's supply chain.

Local actions (`./...`) and `docker://` refs are exempt: they have no
mutable upstream tag to pin. The dependabot check guards the
pin-freshness mechanism itself -- a SHA pin with no Dependabot watching
it stops receiving upstream security fixes the moment it lands.
"""

from __future__ import annotations

import re
from pathlib import Path

import yaml

REPO_ROOT = Path(__file__).resolve().parents[1]
WORKFLOWS_DIR = REPO_ROOT / ".github" / "workflows"
DEPENDABOT = REPO_ROOT / ".github" / "dependabot.yml"

_FULL_SHA = re.compile(r"^[0-9a-f]{40}$")


def _iter_uses(node: object) -> list[str]:
    """Collect every `uses` string anywhere in a parsed workflow tree.

    Walks the structure generically rather than assuming the
    job/steps shape, and sidesteps the YAML 1.1 ``on:`` -> ``True``
    key coercion (the value under any key is still recursed into;
    only ``uses`` keys are yielded).
    """
    found: list[str] = []
    if isinstance(node, dict):
        for key, value in node.items():
            if key == "uses" and isinstance(value, str):
                found.append(value)
            else:
                found.extend(_iter_uses(value))
    elif isinstance(node, list):
        for item in node:
            found.extend(_iter_uses(item))
    return found


def _workflow_files() -> list[Path]:
    return sorted(WORKFLOWS_DIR.glob("*.yml")) + sorted(WORKFLOWS_DIR.glob("*.yaml"))


def test_workflows_directory_is_non_empty() -> None:
    assert WORKFLOWS_DIR.is_dir(), f"missing {WORKFLOWS_DIR}"
    assert _workflow_files(), "no workflow files found to audit"


def test_every_action_is_full_sha_pinned() -> None:
    """No `uses:` may reference a mutable tag/branch -- only a 40-hex SHA."""
    offenders: list[str] = []
    for wf in _workflow_files():
        tree = yaml.safe_load(wf.read_text())
        for ref in _iter_uses(tree):
            if ref.startswith("./") or ref.startswith("docker://"):
                continue  # local / docker refs have no mutable upstream tag
            if "@" not in ref:
                offenders.append(f"{wf.name}: {ref!r} (no @ref)")
                continue
            _, _, rev = ref.partition("@")
            if not _FULL_SHA.match(rev):
                offenders.append(f"{wf.name}: {ref!r} (not a 40-hex commit SHA)")
    assert not offenders, "unpinned actions (KR-008 Phase C regression):\n" + "\n".join(offenders)


def test_pinned_actions_carry_version_comment() -> None:
    """Each SHA-pinned `uses:` line should carry a `# vX.Y.Z` comment.

    The comment is what lets Dependabot bump the opaque SHA and the
    human-readable version together (PMSERV-074, ADR-013). A pin
    without it still runs but rots without a readable trail.
    """
    has_comment = re.compile(r"uses:\s*[^@\s]+@[0-9a-f]{40}\s+#\s*v\d")
    bare_pin = re.compile(r"uses:\s*[^@\s]+@[0-9a-f]{40}\s*$")
    offenders: list[str] = []
    for wf in _workflow_files():
        for lineno, line in enumerate(wf.read_text().splitlines(), 1):
            if bare_pin.search(line) and not has_comment.search(line):
                offenders.append(f"{wf.name}:{lineno}: {line.strip()!r}")
    assert not offenders, "SHA pins missing a `# vX.Y.Z` comment:\n" + "\n".join(offenders)


def test_dependabot_watches_github_actions() -> None:
    """The pin-freshness mechanism must not be silently dropped."""
    assert DEPENDABOT.is_file(), f"missing {DEPENDABOT} (pin-freshness mechanism)"
    cfg = yaml.safe_load(DEPENDABOT.read_text())
    ecosystems = {u.get("package-ecosystem") for u in cfg.get("updates", [])}
    assert "github-actions" in ecosystems, (
        "dependabot.yml no longer watches the github-actions ecosystem; "
        "the SHA pins will stop receiving upstream security fixes"
    )
