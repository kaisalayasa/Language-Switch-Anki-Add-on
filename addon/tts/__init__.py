"""Local, offline TTS support (Piper). No ``anki``/``aqt`` imports anywhere in this package --
see ``tests/test_purity.py``. Only ``addon/ui/piper_test_dialog.py`` is allowed to bridge into
Anki, the same split ``addon/core`` vs ``addon/ops`` already uses.
"""
