"""Register the addon's Tools-menu entries.

Kept separate from ``__init__`` so that importing the package outside Anki (for the pure
unit tests) never touches ``aqt``.

The old "Deck Direction Converter" submenu (Convert / Preview / Generate TTS audio, each its
own dialog built around the deleted role-mapping system) is gone -- ``ui/main_screen.py`` is
now the only user-facing entry point, exactly as this file used to say it eventually would be.

The "Test Piper voice... (dev)" action that used to sit alongside it has been removed from the
menu (2026-09-20, ahead of the AnkiWeb release) -- it was only ever a standalone dev/debug tool
for sampling a voice in isolation, and had no place in a shipped addon's Tools menu.
``ui/piper_test_dialog.py`` itself is untouched and still importable for local debugging
(``from addon.ui.piper_test_dialog import show_piper_test_dialog``); only its menu entry point
is gone.
"""

from __future__ import annotations

__all__ = ["setup"]

_MAIN_SCREEN_ACTION_TEXT = "Deck Direction Converter…"
_installed = False


def setup() -> None:
    """Attach the menu actions once Anki's main window exists."""
    global _installed
    if _installed:
        return
    _installed = True

    from aqt import gui_hooks

    gui_hooks.main_window_did_init.append(_add_menu_actions)


def _add_menu_actions() -> None:
    from aqt import mw
    from aqt.qt import QAction

    main_screen_action = QAction(_MAIN_SCREEN_ACTION_TEXT, mw)
    main_screen_action.triggered.connect(_launch_main_screen)
    mw.form.menuTools.addAction(main_screen_action)


def _launch_main_screen() -> None:
    from aqt.utils import showWarning

    try:
        from .ui.main_screen import show_main_screen
    except Exception as exc:  # pragma: no cover - surfaced in the GUI
        showWarning("Deck Direction Converter failed to load:\n\n%r" % (exc,))
        return
    show_main_screen()
