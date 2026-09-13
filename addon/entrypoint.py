"""Register the addon's Tools-menu entry.

Kept separate from ``__init__`` so that importing the package outside Anki (for the pure
unit tests) never touches ``aqt``.

Everything lives under one Tools-menu submenu rather than four top-level entries -- the
main user-facing flow (Convert, Preview, Generate TTS audio) first, a separator, then the
standalone dev/debug check ("Test Piper voice") last, since it isn't part of the main path.
"""

from __future__ import annotations

__all__ = ["setup"]

_MENU_TEXT = "Deck Direction Converter"
_ACTION_TEXT = "Convert deck language direction…"
_PREVIEW_ACTION_TEXT = "Preview converted card…"
_TTS_BATCH_ACTION_TEXT = "Generate TTS audio… (M5)"
_PIPER_ACTION_TEXT = "Test Piper voice… (M2)"
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

    submenu = mw.form.menuTools.addMenu(_MENU_TEXT)

    convert_action = QAction(_ACTION_TEXT, mw)
    convert_action.triggered.connect(_launch_convert)
    submenu.addAction(convert_action)

    preview_action = QAction(_PREVIEW_ACTION_TEXT, mw)
    preview_action.triggered.connect(_launch_card_preview)
    submenu.addAction(preview_action)

    tts_batch_action = QAction(_TTS_BATCH_ACTION_TEXT, mw)
    tts_batch_action.triggered.connect(_launch_tts_batch)
    submenu.addAction(tts_batch_action)

    submenu.addSeparator()

    piper_action = QAction(_PIPER_ACTION_TEXT, mw)
    piper_action.triggered.connect(_launch_piper_test)
    submenu.addAction(piper_action)


def _launch_convert() -> None:
    from aqt.utils import showWarning

    try:
        from .ui.convert_dialog import show_convert_dialog
    except Exception as exc:  # pragma: no cover - surfaced in the GUI
        showWarning("Deck Direction Converter failed to load:\n\n%r" % (exc,))
        return
    show_convert_dialog()


def _launch_piper_test() -> None:
    from aqt.utils import showWarning

    try:
        from .ui.piper_test_dialog import show_piper_test_dialog
    except Exception as exc:  # pragma: no cover - surfaced in the GUI
        showWarning("Piper voice test dialog failed to load:\n\n%r" % (exc,))
        return
    show_piper_test_dialog()


def _launch_card_preview() -> None:
    from aqt.utils import showWarning

    try:
        from .ui.card_preview import show_card_preview
    except Exception as exc:  # pragma: no cover - surfaced in the GUI
        showWarning("Card preview failed to load:\n\n%r" % (exc,))
        return
    show_card_preview()


def _launch_tts_batch() -> None:
    from aqt.utils import showWarning

    try:
        from .ui.tts_batch_dialog import show_tts_batch_dialog
    except Exception as exc:  # pragma: no cover - surfaced in the GUI
        showWarning("TTS batch dialog failed to load:\n\n%r" % (exc,))
        return
    show_tts_batch_dialog()
