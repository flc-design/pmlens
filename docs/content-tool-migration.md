# Content tool names: compatibility stage

Status: unreleased development change after v0.15.1 (PMSERV-182, stage 1).

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

## Existing drafts

Both names access the same project-local `.pm/x_drafts.db`. This stage does not
rename, copy, or merge databases. Existing draft IDs, source references, states,
redacted content, and the append-only triggers remain intact. Switching tool
names does not create another store or bypass deduplication or redaction.

The later stage of PMSERV-182 will address the database/module/test file names
with a separately tested data migration. It is not required to use the new MCP
names. Do not manually rename the database as part of this stage.
