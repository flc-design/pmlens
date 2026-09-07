"""Compatibility imports for :mod:`pmlens.draft_store` (PMSERV-182).

The old names use the same class, path resolver and factory cache. Existing
x_drafts.db files stay in place; new projects use drafts.db through either
import path. Prefer pmlens.draft_store for new integrations.
"""

from .draft_store import (
    DraftKind as XDraftKind,
)
from .draft_store import (
    DraftSignalType as XDraftSignalType,
)
from .draft_store import (
    DraftStatus as XDraftStatus,
)
from .draft_store import (
    DraftStore as XDraftStore,
)
from .draft_store import (
    RedactionStatus,
    normalize_source_refs,
)
from .draft_store import (
    clear_draft_store as clear_x_draft_store,
)
from .draft_store import (
    default_draft_db_path as default_x_draft_db_path,
)
from .draft_store import (
    get_draft_store as get_x_draft_store,
)

__all__ = [
    "XDraftKind",
    "XDraftSignalType",
    "XDraftStatus",
    "XDraftStore",
    "RedactionStatus",
    "clear_x_draft_store",
    "default_x_draft_db_path",
    "get_x_draft_store",
    "normalize_source_refs",
]
