"""Enforce the structural promises made in claude.md.

1. ``addon/core/`` never imports anki or aqt, so it stays unit-testable with stock Python.
2. ``audio_fields`` and ``deck_state`` contain no language name, no script name and no
   deck-specific field name -- languages are opaque values callers pass in, never named here.
3. ``addon/tts/`` never imports anki or aqt either (M2) -- same reasoning as ``core/``: the
   binary/voice managers and the sanitizer should be testable without a running Anki process,
   and the only Anki-facing bridge is ``addon/ui/piper_test_dialog.py``.

These are asserted rather than left to discipline, because they are exactly the properties
that quietly rot once a second deck shows up.
"""

from __future__ import annotations

import os
import re
import unittest

_ADDON_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "addon")
CORE_DIR = os.path.join(_ADDON_DIR, "core")
TTS_DIR = os.path.join(_ADDON_DIR, "tts")


def _sources(directory):
    for name in sorted(os.listdir(directory)):
        if name.endswith(".py"):
            path = os.path.join(directory, name)
            with open(path, "r", encoding="utf-8") as fh:
                yield name, fh.read()


def _core_sources():
    return _sources(CORE_DIR)


class TestNoAnkiImports(unittest.TestCase):
    def test_core_never_imports_anki_or_aqt(self):
        pattern = re.compile(r"^\s*(?:from|import)\s+(anki|aqt)\b", re.MULTILINE)
        for name, source in _core_sources():
            with self.subTest(module=name):
                self.assertIsNone(
                    pattern.search(source),
                    "addon/core/%s imports anki/aqt; core must stay Anki-free" % name,
                )

    def test_tts_never_imports_anki_or_aqt(self):
        pattern = re.compile(r"^\s*(?:from|import)\s+(anki|aqt)\b", re.MULTILINE)
        for name, source in _sources(TTS_DIR):
            with self.subTest(module=name):
                self.assertIsNone(
                    pattern.search(source),
                    "addon/tts/%s imports anki/aqt; tts/ must stay Anki-free like core/, "
                    "with only addon/ui/piper_test_dialog.py bridging to Anki" % name,
                )


class TestNoLanguageKnowledge(unittest.TestCase):
    """No language, script, or Core-2000 field name may appear in the pure modules.

    Docstrings count. If a term is genuinely needed it belongs in a profile, in docs/, or
    in the Anki-facing layer -- not here.
    """

    FORBIDDEN = [
        "japanese", "english", "spanish", "korean", "russian", "french",
        "kanji", "kana", "hiragana", "katakana", "hangul", "cyrillic", "romaji",
        "vocabulary-", "sentence-", "core 2000", "core2000",
    ]

    # ``audio_fields.py`` matches on a field name -- but the prefix it chose itself
    # (AUDIO_FIELD_PREFIX), not one that came from a deck, which is why that prefix must
    # stay free of any language: it is written into real collections, and a deck converted
    # to one language must still be recognised after being re-converted to another.
    # ``deck_state.py`` is held to the same bar for the same reason -- its tag/css marker are
    # also written into real collections and read back regardless of which languages a given
    # conversion involved. ``addon/llm/`` is deliberately exempt -- naming languages is its
    # job (see ``addon/llm/__init__.py``).
    PURE_MODULES = ["audio_fields.py", "deck_state.py"]

    def test_pure_modules_are_language_agnostic(self):
        for name, source in _core_sources():
            if name not in self.PURE_MODULES:
                continue
            lines = source.lower().splitlines()
            for term in self.FORBIDDEN:
                hits = [i for i, line in enumerate(lines, 1) if term in line]
                with self.subTest(module=name, term=term):
                    self.assertFalse(
                        hits,
                        "addon/core/%s mentions %r on line(s) %s; language-specific "
                        "knowledge belongs in a profile, not in the pure layer"
                        % (name, term, hits),
                    )

if __name__ == "__main__":
    unittest.main()
