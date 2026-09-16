"""``core.deck_state``: recording and reading back whether a notetype was converted by this
addon, and in which direction -- via a note tag and a css comment, each independently readable.
"""

from __future__ import annotations

import unittest

from addon.core.deck_state import (
    ConversionState,
    append_css_marker,
    conversion_tag,
    state_from_css,
    state_from_notetype,
    state_from_tags,
)


class TestConversionTag(unittest.TestCase):
    def test_tag_is_namespaced_and_encodes_both_languages(self):
        tag = conversion_tag("en", "ja")
        self.assertEqual(tag, "ddc-converted::en::ja")

    def test_tag_round_trips_through_state_from_tags(self):
        tag = conversion_tag("es", "en")
        state = state_from_tags(["some-other-tag", tag])
        self.assertEqual(state, ConversionState(target_language="es", native_language="en"))

    def test_no_matching_tag_returns_none(self):
        self.assertIsNone(state_from_tags(["marked-for-review", "leech"]))

    def test_empty_tag_list_returns_none(self):
        self.assertIsNone(state_from_tags([]))

    def test_unrelated_tag_containing_the_prefix_as_a_substring_is_not_matched(self):
        """A user tag that merely contains the prefix text must never be mistaken for the
        real, exact-format mark."""
        self.assertIsNone(state_from_tags(["not-ddc-converted::en::ja-really"]))


class TestCssMarker(unittest.TestCase):
    def test_marker_round_trips_through_state_from_css(self):
        css = append_css_marker(".card { font-size: 20px; }", "en", "ja")
        state = state_from_css(css)
        self.assertEqual(state, ConversionState(target_language="en", native_language="ja"))

    def test_original_css_is_preserved_verbatim_before_the_marker(self):
        original = ".card { font-size: 20px; }\n.word { color: red; }"
        css = append_css_marker(original, "en", "ja")
        self.assertTrue(css.startswith(original))

    def test_css_with_no_marker_returns_none(self):
        self.assertIsNone(state_from_css(".card { font-size: 20px; }"))

    def test_empty_css_returns_none(self):
        self.assertIsNone(state_from_css(""))

    def test_none_css_returns_none_rather_than_raising(self):
        self.assertIsNone(state_from_css(None))

    def test_re_marking_replaces_the_old_marker_instead_of_accumulating(self):
        """Re-analyzing/re-converting an already-marked notetype must not pile up stale
        comments -- only the latest mark should ever be present."""
        css = append_css_marker(".card {}", "en", "ja")
        css = append_css_marker(css, "es", "en")
        self.assertEqual(css.count("ddc-converted:"), 1)
        self.assertEqual(state_from_css(css), ConversionState(target_language="es", native_language="en"))


class TestStateFromNotetype(unittest.TestCase):
    def test_prefers_the_css_marker_when_both_are_present(self):
        css = append_css_marker(".card {}", "en", "ja")
        tags = [conversion_tag("es", "de")]
        state = state_from_notetype(css, tags)
        self.assertEqual(state.target_language, "en")

    def test_falls_back_to_tags_when_css_marker_was_stripped(self):
        """Real scenario this exists for: the user hand-edited the CSS in Anki's card-layout
        screen, wiping the comment, but the note tags survive untouched."""
        css = ".card { font-size: 22px; }"  # hand-edited, marker gone
        tags = [conversion_tag("en", "ja")]
        state = state_from_notetype(css, tags)
        self.assertEqual(state, ConversionState(target_language="en", native_language="ja"))

    def test_returns_none_when_neither_mark_is_present(self):
        """A notetype this addon never touched must not be mistaken for a converted one."""
        self.assertIsNone(state_from_notetype(".card {}", ["unrelated-tag"]))

    def test_returns_none_with_no_sample_tags_and_no_css_marker(self):
        self.assertIsNone(state_from_notetype(".card {}"))


if __name__ == "__main__":
    unittest.main()
