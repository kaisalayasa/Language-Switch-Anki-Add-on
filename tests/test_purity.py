"""Enforce the two structural promises made in claude.md.

1. ``addon/core/`` never imports anki or aqt, so it stays unit-testable with stock Python.
2. ``role_schema`` and ``template_generator`` contain no language name, no script name and
   no deck-specific field name -- all of that belongs in profile JSON.

These are asserted rather than left to discipline, because they are exactly the properties
that quietly rot once a second deck shows up.
"""

from __future__ import annotations

import os
import re
import unittest

CORE_DIR = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "addon", "core"
)


def _core_sources():
    for name in sorted(os.listdir(CORE_DIR)):
        if name.endswith(".py"):
            path = os.path.join(CORE_DIR, name)
            with open(path, "r", encoding="utf-8") as fh:
                yield name, fh.read()


class TestNoAnkiImports(unittest.TestCase):
    def test_core_never_imports_anki_or_aqt(self):
        pattern = re.compile(r"^\s*(?:from|import)\s+(anki|aqt)\b", re.MULTILINE)
        for name, source in _core_sources():
            with self.subTest(module=name):
                self.assertIsNone(
                    pattern.search(source),
                    "addon/core/%s imports anki/aqt; core must stay Anki-free" % name,
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

    # The one permitted exception: "furigana" names an Anki *template filter*, and appears
    # only in prose explaining that filters are opaque strings supplied by the profile.
    PURE_MODULES = ["role_schema.py", "template_generator.py"]

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

    def test_furigana_appears_only_as_an_example_filter_name(self):
        """Guard the exception so it cannot silently widen into real logic."""
        for name, source in _core_sources():
            if name not in self.PURE_MODULES:
                continue
            for lineno, line in enumerate(source.splitlines(), 1):
                if "furigana" not in line.lower():
                    continue
                stripped = line.strip()
                is_comment_or_doc = (
                    stripped.startswith("#")
                    or stripped.startswith("*")
                    or '"""' in source[: source.find(line)].rsplit("\n", 1)[0]
                )
                with self.subTest(module=name, line=lineno):
                    self.assertTrue(
                        is_comment_or_doc or stripped.startswith("``"),
                        "addon/core/%s:%d uses 'furigana' outside prose: %s"
                        % (name, lineno, stripped),
                    )


if __name__ == "__main__":
    unittest.main()
