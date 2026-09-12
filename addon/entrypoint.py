"""Register the addon's Tools-menu action.

Kept separate from ``__init__`` so that importing the package outside Anki (for the pure
unit tests) never touches ``aqt``.
"""

from __future__ import annotations

__all__ = ["setup"]

_ACTION_TEXT = "Convert deck language direction…"
_installed = False


def setup() -> None:
    """Attach the menu action once Anki's main window exists."""
    global _installed
    if _installed:
        return
    _installed = True

    from aqt import gui_hooks

    gui_hooks.main_window_did_init.append(_add_menu_action)


def _add_menu_action() -> None:
    from aqt import mw
    from aqt.qt import QAction

    action = QAction(_ACTION_TEXT, mw)
    action.triggered.connect(_launch)
    mw.form.menuTools.addAction(action)


def _launch() -> None:
    from aqt.utils import showWarning

    try:
        from .ui.convert_dialog import show_convert_dialog
    except Exception as exc:  # pragma: no cover - surfaced in the GUI
        showWarning("Deck Direction Converter failed to load:\n\n%r" % (exc,))
        return
    show_convert_dialog()
