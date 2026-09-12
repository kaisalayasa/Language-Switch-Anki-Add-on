"""Deck Direction Converter — Anki addon entrypoint.

Importing this package must stay safe **outside** Anki, because ``addon.core.*`` is pure
Python and is unit-tested with stock Python (``python -m unittest discover tests``). So the
Qt/Anki wiring is imported lazily and only when ``aqt`` is actually present.
"""

from __future__ import annotations

__version__ = "0.1.0"
__all__ = ["__version__"]


def _running_inside_anki() -> bool:
    try:
        import aqt  # noqa: F401
    except Exception:
        return False
    return True


if _running_inside_anki():  # pragma: no cover - only exercised inside Anki
    from . import entrypoint

    entrypoint.setup()
