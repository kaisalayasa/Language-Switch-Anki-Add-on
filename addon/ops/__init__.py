"""Anki-facing operations.

Everything that imports ``anki``/``aqt`` lives here. ``addon.core`` stays pure so it can be
unit-tested with stock Python; this package executes the plans ``core`` produces.

(``claude.md``'s architecture sketch put ``notetype_manager`` under ``/core``. It was moved
here so that "core is importable without Anki" can be a property the test suite enforces
rather than a convention that quietly rots. The responsibilities are unchanged.)
"""
