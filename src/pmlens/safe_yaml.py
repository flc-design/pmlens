"""``yaml.safe_load`` whose failures never quote the text (PMSERV-266).

PyYAML reports a syntax error as a ``yaml.YAMLError``, whose text quotes the
offending line, so callers describe it by type and position only
(``lineage.error_summary``). But an explicit tag on a scalar that does not fit
it (``!!int``, ``!!float``, ``!!bool``, ``!!timestamp``) makes PyYAML's
constructor raise a plain ``ValueError``, ``KeyError`` or ``AttributeError``
whose message quotes the value, and that slips past every ``except
yaml.YAMLError``. :func:`safe_load` turns any such failure into
:class:`UnreadableValueError`, a ``YAMLError`` that names only the exception
type, raised ``from None`` so the original is never printed.
"""

from __future__ import annotations

import yaml


class UnreadableValueError(yaml.YAMLError):
    """A value PyYAML could not construct, described by exception type only."""

    def __init__(self, kind: str) -> None:
        super().__init__(
            f"a value could not be read ({kind}); check explicit tags such as !!int, "
            "!!float or !!bool"
        )


def safe_load(text: str) -> object:
    """``yaml.safe_load(text)``, raising only YAMLError subclasses.

    Raises:
        yaml.YAMLError: The text is not valid YAML (the original error), or a
            value could not be constructed (:class:`UnreadableValueError`).
    """
    try:
        return yaml.safe_load(text)
    except yaml.YAMLError:
        raise
    except Exception as exc:  # noqa: BLE001 - every constructor failure, whatever its type
        raise UnreadableValueError(type(exc).__name__) from None
