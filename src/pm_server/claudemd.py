"""Backward-compat re-export shim for v0.4.x callers.

The real implementation moved to :mod:`pm_server.rules` (PMSERV-043,
ADR-008). This module is preserved so existing imports continue to
work unchanged::

    from pm_server.claudemd import ensure_claudemd, update_claudemd, ...

The shim is currently transparent — every symbol is the same object as
in :mod:`pm_server.rules` (verifiable via ``is`` identity). It is
deprecated since v0.6.0 and slated for removal in v1.0.0.
"""

from pm_server.rules import (
    BEGIN_MARKER,
    BEGIN_PATTERN,
    CLAUDEMD_TEMPLATE,
    END_MARKER,
    OTHER_SECTION_PATTERN,
    TEMPLATE_VERSION,
    ensure_claudemd,
    get_claudemd_status,
    update_claudemd,
)

__all__ = [
    "BEGIN_MARKER",
    "BEGIN_PATTERN",
    "CLAUDEMD_TEMPLATE",
    "END_MARKER",
    "OTHER_SECTION_PATTERN",
    "TEMPLATE_VERSION",
    "ensure_claudemd",
    "get_claudemd_status",
    "update_claudemd",
]
