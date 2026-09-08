# Releasing PM Lens

How a version of `pmlens` (and its `pm-server` compatibility wrapper) reaches
PyPI, the GitHub Release page, and the Claude Code plugin.

> **The one thing to remember.** Pushing a `vX.Y.Z` tag stops at **one**
> approval, which publishes `pmlens` and the `pm-server` wrapper together, in
> that order. Push the tag **by name** — `--follow-tags` also sends unrelated
> stale tags, and every tag on the remote starts a release run (§1 step 5).
>
> Until PMSERV-174 a tag started two runs with two independent approvals, and
> approving only one shipped a broken plugin with every job green. If you are
> reading an older checkout, that is the flow it describes.

---

## 1. Pre-flight (on `main`, before tagging)

`pyproject.toml [project].version` is the single source of truth. Everything
below repeats that number and must move in the same commit.

1. **Bump `pyproject.toml`**, then bump every surface:

   | File | What to change |
   |---|---|
   | `src/pmlens/__init__.py` | `__version__` |
   | `manifest.json` | `version` (the `.mcpb` bundle) |
   | `.claude-plugin/marketplace.json` | `metadata.version` |
   | `plugin/.claude-plugin/plugin.json` | `version` |
   | `plugin/.mcp.json` | the `pm-server@X.Y.Z` uvx pin |
   | `plugin/README.md` | three pins: prerequisite, committed pin, floor form |
   | `packaging/pm-server-wrapper/pyproject.toml` | `project.version` **and** the `pmlens>=X.Y.Z` floor (plus the version in its header comments) |
   | `uv.lock`, `requirements.lock` | run `make lock-sync` — do **not** hand-edit |
   | `docs/cheatsheet.md`, `docs/cheatsheet.ja.md` | the `> Version X.Y.Z \|` header |
   | `docs/architecture.html` | three stamps (header, tool-count caption, footer). **Leave the dates alone** — `Generated` is frozen; `counts refreshed` moves only when counts are actually re-derived |
   | `docs/README.md` | the `architecture.html は vX.Y.Z 時点` clause **only**. The `user-guide / workflow-guide は v0.12.0` clause on the same line is deliberately frozen |

2. **Let the guard check you** rather than the table above:

   ```bash
   pytest tests/test_version_lockstep.py -q
   ```

   It owns the authoritative registry, asserts an exact pin *count* per file so
   a newly added pin cannot hide, and scans the whole release surface for
   version strings that disagree. If you add a version pin to a new file, add
   that file to `SCOPE_FILES` there. (This guard is `tests/test_version_lockstep.py`;
   it replaced `test_plugin.py::TestPluginVersionSync` in PMSERV-172.)

3. **`CHANGELOG.md`** — move `[Unreleased]` to `[X.Y.Z] - YYYY-MM-DD`. *No test
   asserts this.* Re-read the entries against the code as it is **now**: an
   entry written months ago can have been made false by a later design change —
   the 0.13.0 CHANGELOG advertised a warning code that had since been deleted.
   The CHANGELOG goes out on the Release page, so it is public.

4. **Full suite + lint green on `main`**: `pytest -q`, `ruff check src/ tests/`,
   `ruff format --check src/ tests/`. `ci.yml` does not run on tags, so `main`
   being green is the only pre-tag signal you get from CI (the `verify` job
   re-runs both on the tagged commit — see below).

   **Refresh and validate the dependency locks before this release check.**
   Use Python 3.11+ and uv 0.11.25 (the same exporter as CI):

   ```bash
   make lock-refresh
   make lock-check
   # Review both lockfile changes, then in a fresh disposable environment:
   pip install --require-hashes -r requirements.lock
   pip install -e . --no-deps
   pip check
   pytest -q
   ruff check src/ tests/ scripts/lockfiles.py
   ruff format --check src/ tests/ scripts/lockfiles.py
   ```

   `uv.lock` owns the resolution and `requirements.lock` is its hashed export.
   Refresh explicitly upgrades dependencies; if a full upgrade is unsuitable,
   use `python scripts/lockfiles.py refresh --package NAME` and document the
   scope in the release change. After any later version/metadata change, run
   `make lock-sync` again. CI and the tag's `verify` job both run the offline
   consistency check before publishing. This guard proves agreement with the
   selected resolution; it does not prove that every upstream version is the
   newest. See [the dependency workflow](../CONTRIBUTING.md#dependency-lockfiles).

5. **Commit, then tag with an *annotated* tag.** The Release page's title and
   body come from the tag message:

   ```bash
   git tag -a vX.Y.Z -m "vX.Y.Z — one-line title" -m "Body that becomes the release notes."
   git push origin main
   git push origin vX.Y.Z          # name the tag — see the warning below
   ```

   A **lightweight** tag is detected and falls back to a CHANGELOG pointer —
   without that detection, `git tag --format=%(contents:*)` silently resolves to
   the *commit* message and would publish its `Co-Authored-By` trailers as your
   public release notes.

   > **Push the tag by name. Do not use `--follow-tags` or `--tags`.**
   > The trigger is `push: tags: 'v*'`, so *every* tag that lands on the remote
   > starts a release run — including ones you did not mean to send.
   > `--follow-tags` sweeps up every unpushed **annotated** tag reachable from
   > the commit, and `--tags` sweeps up lightweight ones too. Releasing 0.14.0
   > this way also pushed a stale local `v0.8.0`, which immediately started a
   > second "Release to PyPI" run for a version that was never published.
   >
   > Nothing shipped — the approval gate held it at the `pypi` environment and
   > the run was cancelled before anyone approved it — but that was the gate
   > doing a job it was not designed for. Do not rely on it: an old tag whose
   > `pyproject.toml` version matches its own name passes `verify` and reaches
   > the approval prompt looking exactly like the release you meant to make.
   >
   > Check before pushing. Anything listed here would be sent by `--follow-tags`:
   >
   > ```bash
   > comm -23 <(git tag | sort) \
   >          <(git ls-remote --tags origin | sed 's|.*refs/tags/||; s|\^{}||' | sort -u)
   > ```

---

## 2. The single approval

Pushing `vX.Y.Z` fires **one** workflow, `.github/workflows/release.yml`, which
stops once at the `pypi` GitHub environment:

| Job | Publishes | Gate |
|---|---|---|
| **Publish pmlens + pm-server wrapper to PyPI (single approval)** | `pmlens`, then `pm-server` | the only approval |

Both uploads are **steps in one job**, in that order. That ordering is not a
rule anyone follows any more — if the pmlens upload fails, the wrapper step is
never reached.

Why the order matters at all: the wrapper is a metapackage declaring
`pmlens>=X.Y.Z`. Publishing it first creates a window where
`uvx pm-server@X.Y.Z` cannot resolve — and if pmlens then fails to publish,
that window is **permanent**, because PyPI never allows re-uploading a
filename.

**Why one job and not two chained with `needs:`** (which earlier drafts of §6
proposed): GitHub creates a pending deployment per *job*, so two jobs on the
`pypi` environment means two approval prompts — the thing PMSERV-174 set out to
remove — and a `needs:` chain makes them sequential rather than batchable.
Step order inside one job is also a stronger guarantee than `needs:`, which
only requires the previous job to *succeed*; with `skip-existing: true`, success
is not evidence that anything was uploaded.

The announcement is still gated separately: `github-release` polls PyPI for
`pm-server X.Y.Z` before creating the Release page. That poll is not redundant
— it is the only observation that distinguishes "uploaded" from "skipped as
already existing".

`publish-wrapper.yml` still exists as the **rollback** and manual backfill, but
no longer fires on a tag — and since PMSERV-174 step 5 it can no longer publish
at all until its PyPI publisher entry is re-added. See §4 and §6 before
touching it.

---

## 3. Job graph and what each red means

**`release.yml`**: `verify` → (`build` ∥ `build-wrapper`) → (`publish` ∥ `pack-mcpb`) → `github-release`

| Gate | Red means |
|---|---|
| `verify` › *Tag name must match the tagged tree's version* | The tag says `vX.Y.Z` but the tagged tree's `pyproject.toml` says something else — the version bump is probably uncommitted. Delete the tag, fix, re-tag. |
| `verify` › *Tag name must match the wrapper's version* | `packaging/pm-server-wrapper/pyproject.toml` was not bumped. This is the v0.12.1 incident. It is checked **here**, before the approval, because after the approval pmlens is already on PyPI and this version can no longer be fixed. |
| `verify` › ruff + pytest | The **tagged commit** is broken. This job exists because `ci.yml` has no tag trigger, so before it nothing tested what was actually being published. |
| `pack-mcpb` › `scripts/build_mcpb.py` | `manifest.json` disagrees with `pyproject.toml`, or the MCPB v0.4 shape is violated. |
| `publish` › *Publish pmlens…* | Upload failed. The wrapper step below it did **not** run, which is the intended behaviour — nothing is half-published. Fix and re-run the job. |
| `publish` › *Publish pm-server wrapper…* | pmlens uploaded but the wrapper did not. `github-release` will refuse to announce (below). Re-run failed jobs; both uploads are idempotent. |
| `github-release` › *Require the pm-server wrapper on PyPI* | See §2. |

**`publish-wrapper.yml`** (dispatch-only rollback): `build` → `publish`

| Gate | Red means |
|---|---|
| `build` › *Tag name must match the wrapper's version* | Skipped on dispatch — `github.ref_type` is not `tag`. |
| `publish` › *Require pmlens on PyPI* | On this path nothing else orders the two uploads, so this poll is load-bearing. See §2. |
| `publish` › *pypa/gh-action-pypi-publish* | Since 2026-08-06 there is **no `publish-wrapper.yml` publisher entry on PyPI** (§6 step 5), so the OIDC exchange fails here no matter how green everything above it is. Re-add the entry before using this workflow — §4. |

---

## 4. Recovery

- **PyPI filenames are immutable.** You cannot replace an uploaded artifact.
- **A green publish job is not evidence that anything was uploaded.** Both use
  `skip-existing: true`, so re-firing a tag republishes nothing and still goes
  green. Confirm on pypi.org, not in the Actions UI.
- **Wrapper-only miss** (pmlens published, wrapper not): tag
  `vX.Y.Z-wrapper.N`. Both workflows accept it (`${TAG#v}` then `%%-*` strips
  the suffix), the pmlens side no-ops on `skip-existing`, and `github-release`
  correctly skips it — its `if:` excludes any tag containing a hyphen.
- **Rolling back to `publish-wrapper.yml` now takes two steps, not one.** Its
  PyPI publisher entry was removed on 2026-08-06 (§6 step 5), so restoring
  `push: tags: 'v*'` in the workflow is no longer sufficient — the OIDC exchange
  fails with no matching publisher. **Re-add the entry on pypi.org first**
  (`pm-server` → *Manage* → *Publishing*: `flc-design` / `pmlens` /
  `publish-wrapper.yml` / `pypi`), *then* restore the trigger and re-tag. The
  workflow file is deliberately kept in the repo for exactly this path — do not
  delete it.
- **`workflow_dispatch` on `publish-wrapper.yml`** is advertised as a backfill
  path, but it now has two obstacles: the missing publisher entry above, and the
  `pypi` environment restricting deployments to `v*` tags — a dispatch from
  `main` builds successfully and is then rejected at the environment gate
  (observed 2026-06-30: *Branch main is not allowed to deploy*). The 0.12.0
  backfill worked around this by temporarily adding `main` to the environment
  and removing it again afterwards. Clear both before relying on it; otherwise
  use the `-wrapper.N` tag above.

---

## 5. Known gaps (deliberate, documented rather than fixed)

- **Pre-release tags publish as final.** Both version checks strip everything
  after the first hyphen, so `v0.14.0-rc1` on a 0.14.0 tree passes `verify` and
  publishes a *real* 0.14.0 — while silently skipping the Release page. Do not
  use `-rc` style tags until this is handled.
- **The tagged commit is tested on one Python only.** `verify` runs 3.12;
  `ci.yml`'s 3.11/3.13 matrix never sees the tag.
- **`pack-mcpb` runs parallel to `publish`, not before it.** A bundle-packing
  failure can therefore land *after* PyPI already has the artifacts.
- **Two runs, two approvals** — **fixed in PMSERV-174**; see §2. Kept here as a
  pointer because older tags still carry the two-approval workflows, so a
  re-run of an old release run behaves the old way.

---

## 6. PyPI trusted publishers

No PyPI API token exists anywhere in this project — publishing is OIDC-only.
The bindings, which must be kept in sync with the workflow filenames:

| PyPI project | Owner | Repository | Workflow | Environment | Status |
|---|---|---|---|---|---|
| `pmlens` | `flc-design` | `pmlens` | `release.yml` | `pypi` | live |
| `pm-server` | `flc-design` | `pmlens` | `release.yml` | `pypi` | live since PMSERV-174 |
| `pm-server` | `flc-design` | `pmlens` | `publish-wrapper.yml` | `pypi` | **removed 2026-08-06** (step 5) |

Each PyPI project therefore has exactly **one** trusted publisher, `release.yml`
— the target state of PMSERV-174. The third row is kept as a record rather than
deleted, because `publish-wrapper.yml` is still in the repo and re-adding that
entry is the first move in any rollback (§4).

**Renaming or moving a workflow file breaks publishing**, because the workflow
filename is part of the OIDC claim.

### Consolidating the wrapper into `release.yml` (PMSERV-174)

Done **between releases**, in this order. PyPI allows a project to have several
trusted publishers at once, so this is add-then-verify-then-remove with no
window in which the wrapper is unpublishable.

1. ✅ **Done 2026-07-27.** On pypi.org → `pm-server` → *Manage* → *Publishing* →
   add a second GitHub publisher: `flc-design` / `pmlens` / `release.yml` /
   `pypi`. Note the Environment field's placeholder reads `release`; it must be
   **`pypi`**, matching `environment: name: pypi` in the workflow. **Leave the
   `publish-wrapper.yml` entry in place.**
2. ✅ **Done 2026-07-27.** In the repo: build the wrapper in `release.yml`
   (`build-wrapper`), publish both from the **single** `publish` job as two
   ordered steps, move the wrapper's tag↔version assertion into `verify`, and
   keep the `pm-server` presence poll before `github-release`.

   > Earlier drafts of this step said "add a wrapper-publish job with
   > `needs: publish`". That does **not** produce one approval — GitHub gates
   > per job, so a second job on the `pypi` environment is a second prompt. See
   > §2 for why the two uploads are steps in one job instead.

3. ✅ **Done 2026-07-27.** Change `publish-wrapper.yml`'s trigger to
   `workflow_dispatch:` only — this removes the second automatic run and the
   second approval while preserving a manual recovery path. **Do not delete the
   file.**
4. ✅ **Done 2026-08-01, with v0.15.0.** Ship one real release through the merged
   workflow. Confirm on pypi.org that **both** projects show the new version,
   and that the provenance for **both** now names `release.yml`:

   ```bash
   curl -s https://pypi.org/integrity/pm-server/X.Y.Z/pm_server-X.Y.Z-py3-none-any.whl/provenance \
     | python3 -c "import json,sys; print(json.load(sys.stdin)['attestation_bundles'][0]['publisher'])"
   ```

   Before PMSERV-174 that printed `publish-wrapper.yml`. Printing `release.yml`
   is the machine-checkable evidence that the merged path actually published,
   rather than a maintainer's impression that the approval felt like one click.
   For v0.15.0 both projects printed `release.yml`, on a single approval. The
   `publish` job going green is *not* part of this evidence — `skip-existing:
   true` makes it green whether or not anything was uploaded (§4).
5. ✅ **Done 2026-08-06 — after step 4 succeeded.** Removed the
   `publish-wrapper.yml` publisher entry on pypi.org. `publish-wrapper.yml`
   itself stays in the repo, `workflow_dispatch`-only. **This is the change that
   made rollback a two-step operation — see §4.**

**Rollback**, now that step 5 has run: re-add the `publish-wrapper.yml` publisher
entry on pypi.org **first**, then restore `push: tags: 'v*'` in the workflow and
re-tag. Two moves, not one — the workflow alone cannot authenticate.

Until 2026-08-06 it *was* one move, because the entry had never been removed.
That is the entire reason step 5 came last: sequencing the migration as
add → verify → remove kept the cheap rollback available through the only window
where it was likely to be needed, and paid for it afterwards with a rollback
that is one step more expensive but no longer on the hot path.
