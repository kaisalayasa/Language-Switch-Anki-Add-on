"""Text sanitization ahead of TTS synthesis.

The messy sample below is real ``Vocabulary-English`` content, copied verbatim from
``docs/deck-facts.md`` -- not a synthetic fixture. A second case proves the script filter is
generic (works for any codepoint-range allowlist), not hardcoded to English/Latin, matching
the same generalization rule already enforced on ``addon/core`` by ``test_purity.py``.
"""

from __future__ import annotations

import unittest

from addon.tts.sanitize import LATIN_RANGES, sanitize_text

# Basic CJK Unified Ideographs, used only to prove the filter isn't Latin-specific.
_CJK_RANGES = ((0x4E00, 0x9FFF),)


class TestSanitizeMarkup(unittest.TestCase):
    def test_strips_html_tags_and_decodes_entities(self):
        raw = "processing,&nbsp;management<div>(unlike 加工, a new thing is not created)</div>"
        clean = sanitize_text(raw, allowed_ranges=LATIN_RANGES)
        self.assertNotIn("<div>", clean)
        self.assertNotIn("&nbsp;", clean)
        self.assertNotIn("加工", clean)
        self.assertIn("processing", clean)
        self.assertIn("management", clean)
        self.assertIn("a new thing is not created", clean)

    def test_strips_html_comment(self):
        raw = "<!--anki-->cease, stop,&nbsp;let up"
        clean = sanitize_text(raw, allowed_ranges=LATIN_RANGES)
        self.assertNotIn("anki", clean)
        self.assertIn("cease, stop", clean)

    def test_strips_br_tag(self):
        raw = "be in time, serve (my) purpose<br>なくても〜 do without"
        clean = sanitize_text(raw, allowed_ranges=LATIN_RANGES)
        self.assertNotIn("<br>", clean)
        self.assertIn("be in time, serve (my) purpose", clean)

    def test_strips_sound_tag(self):
        raw = "hello [sound:foo.mp3] world"
        clean = sanitize_text(raw, allowed_ranges=LATIN_RANGES)
        self.assertNotIn("sound:", clean)
        self.assertNotIn(".mp3", clean)

    def test_strips_ruby_reading_bracket(self):
        raw = "term[reading] more text"
        clean = sanitize_text(raw, allowed_ranges=LATIN_RANGES)
        self.assertNotIn("[", clean)
        self.assertNotIn("reading", clean)
        self.assertIn("term", clean)


class TestSanitizeIsScriptAgnostic(unittest.TestCase):
    """Proves the allowlist is a caller-supplied parameter, not a hardcoded language."""

    def test_latin_allowlist_drops_cjk(self):
        clean = sanitize_text("hello 世界", allowed_ranges=LATIN_RANGES)
        self.assertIn("hello", clean)
        self.assertNotIn("世", clean)
        self.assertNotIn("界", clean)

    def test_cjk_allowlist_drops_latin(self):
        clean = sanitize_text("hello 世界", allowed_ranges=_CJK_RANGES)
        self.assertNotIn("hello", clean)
        self.assertIn("世", clean)
        self.assertIn("界", clean)

    def test_empty_allowed_ranges_keeps_every_script_but_still_strips_markup(self):
        clean = sanitize_text("<b>hello 世界</b>", allowed_ranges=())
        self.assertEqual(clean, "hello 世界")


if __name__ == "__main__":
    unittest.main()
