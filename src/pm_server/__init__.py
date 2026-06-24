"""Deprecated import alias — ``pm_server`` is now :mod:`pmlens`.

The Phase-3 rename (PMSERV-137 / ADR-034) moved the package to ``pmlens``.
This zero-logic shim aliases the legacy import name to the real package via
``sys.modules`` so existing imports of the old name — including submodule and
``python -m`` invocations — keep resolving to ``pmlens``. It is shipped on PyPI
only and is explicitly excluded from the ``.mcpb`` Desktop bundle, so the bundle
can never fall back to the old namespace.
"""

import sys

import pmlens

sys.modules[__name__] = pmlens
