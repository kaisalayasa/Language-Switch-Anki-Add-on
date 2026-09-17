"""``llm.field_visibility``: the "Hide fields" panel's underlying logic -- a non-technical,
reversible way to turn an already-placed field's display on/off without touching HTML.
"""

from __future__ import annotations

import unittest

from addon.llm.field_visibility import (
    HIDDEN_MARKER_PREFIX,
    is_field_hidden,
    set_field_hidden,
    visible_fields,
)


class TestVisibleFields(unittest.TestCase):
    def test_lists_front_and_back_fields_separately(self):
        front = '<div class="word">{{Word}}</div>'
        back = '{{FrontSide}}<hr id=answer><div>{{Translation}}</div>'
        self.assertEqual(visible_fields(front, back), (("Word",), ("Translation",)))

    def test_a_field_on_both_sides_counts_as_front_only(self):
        front = "{{Word}}"
        back = "{{FrontSide}}<hr id=answer>{{Word}}{{Translation}}"
        front_fields, back_fields = visible_fields(front, back)
        self.assertEqual(front_fields, ("Word",))
        self.assertEqual(back_fields, ("Translation",))

    def test_generated_audio_fields_are_never_listed(self):
        front = "{{Word}}{{#ddc-audio-Word}}{{ddc-audio-Word}}{{/ddc-audio-Word}}"
        front_fields, _back = visible_fields(front, "{{FrontSide}}")
        self.assertEqual(front_fields, ("Word",))

    def test_a_field_never_referenced_anywhere_is_not_listed(self):
        """Matches direction.py's own exclusion of never-shown deck fields -- nothing to
        toggle for a field that was never placed on the card in the first place."""
        front_fields, back_fields = visible_fields("{{Word}}", "{{FrontSide}}")
        self.assertNotIn("Core-Index", front_fields + back_fields)

    def test_an_already_hidden_field_still_appears_not_removed_from_the_list(self):
        """Hiding replaces the reference with a marker comment naming the field -- the
        checkbox must stay in the list (just unchecked), not vanish once toggled off, or
        there would be no way to show it again."""
        front = set_field_hidden("<div>{{Word}}</div>", "Word", hidden=True)
        front_fields, _back = visible_fields(front, "{{FrontSide}}")
        self.assertEqual(front_fields, ("Word",))

    def test_empty_templates_list_nothing(self):
        self.assertEqual(visible_fields("", ""), ((), ()))


class TestSetFieldHidden(unittest.TestCase):
    def test_hiding_removes_the_reference_entirely(self):
        """Not wrapped, not CSS-hidden -- actually gone, leaving only an inert marker comment
        (see module docstring for why this is safe now that pre-existing audio is stripped
        from note data at Convert time rather than suppressed per-reference here)."""
        html = '<div class="word">{{Word}}</div>'
        result = set_field_hidden(html, "Word", hidden=True)
        self.assertTrue(is_field_hidden(result, "Word"))
        self.assertNotIn("{{Word}}", result)
        self.assertEqual(result, '<div class="word"><!--ddc-hidden:Word:--></div>')

    def test_hiding_leaves_no_span_or_css_class_reference_behind(self):
        html = "{{Audio}}"
        result = set_field_hidden(html, "Audio", hidden=True)
        self.assertNotIn("<span", result)
        self.assertNotIn("display", result)

    def test_unhide_restores_a_bare_reference_exactly(self):
        html = '<div class="word">{{Word}}</div>'
        hidden = set_field_hidden(html, "Word", hidden=True)
        shown = set_field_hidden(hidden, "Word", hidden=False)
        self.assertEqual(shown, html)

    def test_unhide_restores_a_filtered_reference_exactly(self):
        """The exact example that prompted this design: {{text:Audio}} in a div, hidden then
        shown again, byte for byte."""
        html = '<div class="audio">{{text:Audio}}</div>'
        hidden = set_field_hidden(html, "Audio", hidden=True)
        self.assertEqual(hidden, '<div class="audio"><!--ddc-hidden:Audio:text--></div>')
        shown = set_field_hidden(hidden, "Audio", hidden=False)
        self.assertEqual(shown, html)

    def test_unhide_restores_a_non_text_filter_exactly(self):
        html = "<div>{{furigana:Reading}}</div>"
        hidden = set_field_hidden(html, "Reading", hidden=True)
        shown = set_field_hidden(hidden, "Reading", hidden=False)
        self.assertEqual(shown, html)

    def test_hiding_twice_does_not_duplicate_or_lose_the_marker(self):
        html = "{{Word}}"
        once = set_field_hidden(html, "Word", hidden=True)
        twice = set_field_hidden(once, "Word", hidden=True)
        self.assertEqual(once, twice)
        self.assertEqual(twice.count(HIDDEN_MARKER_PREFIX), 1)

    def test_unhiding_an_already_visible_field_is_a_no_op(self):
        html = "{{Word}}"
        self.assertEqual(set_field_hidden(html, "Word", hidden=False), html)

    def test_hiding_does_not_touch_a_conditional_guard(self):
        html = "{{#Word}}{{Word}}{{/Word}}"
        result = set_field_hidden(html, "Word", hidden=True)
        self.assertIn("{{#Word}}", result)
        self.assertIn("{{/Word}}", result)
        self.assertIn("<!--%s:Word:-->" % HIDDEN_MARKER_PREFIX, result)

    def test_every_occurrence_is_toggled_together(self):
        """A field referenced more than once must not end up half-hidden."""
        html = "{{Kanji}} ... {{Kanji}} ... {{Kanji}}"
        result = set_field_hidden(html, "Kanji", hidden=True)
        self.assertEqual(result.count(HIDDEN_MARKER_PREFIX), 3)
        shown = set_field_hidden(result, "Kanji", hidden=False)
        self.assertEqual(shown, html)

    def test_a_different_field_with_a_shared_prefix_is_not_touched(self):
        """{{FrontSide}} must never be affected by toggling a field named "Front"."""
        html = "{{Front}}{{FrontSide}}"
        result = set_field_hidden(html, "Front", hidden=True)
        self.assertIn("{{FrontSide}}", result)
        self.assertFalse(is_field_hidden(result, "FrontSide"))


if __name__ == "__main__":
    unittest.main()
