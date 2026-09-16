"""Register the addon's Tools-menu entries.

Kept separate from ``__init__`` so that importing the package outside Anki (for the pure
unit tests) never touches ``aqt``.

The old "Deck Direction Converter" submenu (Convert / Preview / Generate TTS audio, each its
own dialog built around the deleted role-mapping system) is gone -- ``ui/main_screen.py`` is
now the only user-facing entry point, exactly as this file used to say it eventually would be.
The Piper voice test stays as a small standalone dev/debug action; it shares no code with
anything that was deleted.
"""

from __future__ import annotations

__all__ = ["setup"]

_PIPER_ACTION_TEXT = "Test Piper voice… (dev)"
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

    piper_action = QAction(_PIPER_ACTION_TEXT, mw)
    piper_action.triggered.connect(_launch_piper_test)
    mw.form.menuTools.addAction(piper_action)


def _launch_piper_test() -> None:
    from aqt.utils import showWarning

    try:
        from .ui.piper_test_dialog import show_piper_test_dialog
    except Exception as exc:  # pragma: no cover - surfaced in the GUI
        showWarning("Piper voice test dialog failed to load:\n\n%r" % (exc,))
        return
    show_piper_test_dialog()


def _launch_main_screen() -> None:
    from aqt.utils import showWarning

    try:
        from .ui.main_screen import show_main_screen
    except Exception as exc:  # pragma: no cover - surfaced in the GUI
        showWarning("Deck Direction Converter failed to load:\n\n%r" % (exc,))
        return
    show_main_screen()
