# Content pipeline names and database compatibility

Status: unreleased development change after v0.15.1 (PMSERV-182).

The content pipeline prepares drafts for human review and publication to any
destination. New integrations should use these destination-neutral names:

| Preferred name | Supported legacy name |
| --- | --- |
| `pm_draft_content` | `pm_draft_x` |
| `pm_drafts_pending` | `pm_x_drafts_pending` |

`pm_redact_draft` and `pm_reject_draft` keep their existing names. The arguments,
defaults, results, errors, deduplication, debounce, and redaction behavior stay
the same. Each legacy tool delegates to the corresponding preferred tool.
Legacy responses have no added deprecation fields or warnings; the tool
descriptions identify the preferred name.

## Existing installations

Both names remain registered throughout this compatibility stage. There is no
scheduled removal version: removing the legacy names requires a separately
announced breaking change and migration instructions. Full mode exposes 46 MCP
tool names for 44 operations, including the two legacy aliases. Lens mode still
exposes 16 names, or 18 with Desktop outbox writes enabled; neither name of these
content tools is registered in either Lens configuration.

Existing host permissions referring to `mcp__pmlens__pm_draft_x` or
`mcp__pmlens__pm_x_drafts_pending` continue to apply to those names. Permission
entries are not rewritten or copied to the new names. Approve the new names
through the host's normal permission flow when adopting them; an existing
permission for an old name does not imply permission for a new name.

Rule template v14 and newly created built-in `content-pipeline` workflows use
the preferred names. Existing copied/custom workflows can continue using the
legacy names. Update them when convenient after installing a version that
exposes the preferred names. v0.15.1 and earlier expose only the legacy names.

## Database selection

Every content tool, including both legacy aliases, uses the same project-local
store. The filename is chosen on each tool call, before opening a database:

| Files already present in `.pm/` | Behavior |
| --- | --- |
| Neither database | Create `drafts.db` when a content tool first needs a store |
| Only `drafts.db` | Use `drafts.db` |
| Only `x_drafts.db` | Keep using `x_drafts.db` in place |
| Both databases | Return `status: error`, `code: draft_store_conflict`; open neither |

This is a compatibility rollout, with no automatic rename, copy, merge, or
deletion. Retaining existing legacy files lets v0.15.1 and older installations
continue using the same store. Their `-wal` and `-shm` files stay beside the
database; committed data still in the WAL remains accessible. Existing IDs,
source references, timestamps, states, redacted content and rejection reasons
remain intact. Switching tool names does not bypass deduplication or redaction.

The SQLite schema stays at `user_version=1`. Its `x_drafts` table, indexes and
append-only / posted-content triggers keep their original identifiers and
behavior. These names are deliberately retained for database compatibility.
Both filenames and their WAL/SHM sidecars have explicit Git ignore entries;
the `.mcpb` bundle continues excluding the entire `.pm/` directory.

`pm_status` checks only existing stores and never creates a draft database.
Its `diagnostics.x_drafts_pending` key remains compatible. If both databases
exist, the count is `null` (unknown) and a `draft_store_conflict` warning gives
recovery guidance. Lens mode does not probe either filename.

## Upgrading, older clients and conflicts

For a project with `x_drafts.db`, install the newer version and continue using
the pipeline. No database operation or filename change is needed. Older clients
using that project continue seeing the same drafts.

For a project first using the pipeline on the newer version, use clients that
understand `drafts.db`. v0.15.1 and older versions do not discover that filename
and would create a separate `x_drafts.db`. Do not alternate versions on such a
project or downgrade a client that needs those drafts. This release does not
provide a downgrade conversion command or coordinate simultaneous first use by
old and new clients. Later calls on the newer version detect a pair of files
as a conflict, including when a store was previously cached in that process.

If both files already exist, stop all clients accessing the project and back
up **both databases and their WAL/SHM sidecars** before taking recovery action.
Have a maintainer inspect the copies and reconcile their histories; row IDs
can overlap, so neither file can be assumed redundant. No automatic merge is
provided. Do not delete one database, overwrite it with the other, or rename
only the main database while clients are running. After reconciliation, retain
one authoritative database in `.pm/` and keep the backups separately.

## Python imports and test files

The implementation now lives in `pmlens.draft_store`, with `DraftStore`,
`default_draft_db_path`, `get_draft_store` and `clear_draft_store`. The old
`pmlens.x_draft_store` module remains a thin import compatibility layer: its
public class, enum, normalizer and factory names refer to the same objects.
`default_x_draft_db_path` follows the same selection rules above, so using old
and new imports does not by itself create two stores. New code should import
`pmlens.draft_store`.

The five pipeline test files now use `test_draft_store.py`,
`test_drafts_tools.py`, `test_drafts_status.py`, `test_content_golden.py` and
`test_drafts_gitignore.py`. Coverage includes legacy databases with active WAL
data, all persisted states, shared aliases and caches, conflict handling, and
unchanged redaction / append-only guarantees.
