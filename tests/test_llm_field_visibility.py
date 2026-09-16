"""``llm.field_visibility``: the "Hide fields" panel's underlying logic -- a non-technical,
reversible way to turn an already-placed field's display on/off without touching HTML.
"""

from __future__ import annotations

import unittest

from addon.llm.field_visibility import (
    HIDDEN_FIELD_CSS_CLASS,
    ensure_hidden_field_css,
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

    def test_an_already_hidden_field_still_appears_not_removed(self):
        """Hiding wraps the reference, it doesn't delete it -- the checkbox must stay in the
        list (just unchecked), not vanish once toggled off."""
        front = '<div><span class="%s">{{Word}}</span></div>' % HIDDEN_FIELD_CSS_CLASS
        front_fields, _back = visible_fields(front, "{{FrontSide}}")
        self.assertEqual(front_fields, ("Word",))

    def test_empty_templates_list_nothing(self):
        self.assertEqual(visible_fields("", ""), ((), ()))


class TestSetFieldHidden(unittest.TestCase):
    def test_hides_a_plain_reference(self):
        html = '<div class="word">{{Word}}</div>'
        result = set_field_hidden(html, "Word", hidden=True)
        self.assertTrue(is_field_hidden(result, "Word"))
        self.assertIn('<span class="%s">{{Word}}</span>' % HIDDEN_FIELD_CSS_CLASS, result)

    def test_hides_a_filtered_reference(self):
        html = '<div class="t">{{text:Vocabulary-Audio}}</div>'
        result = set_field_hidden(html, "Vocabulary-Audio", hidden=True)
        self.assertIn(
            '<span class="%s">{{text:Vocabulary-Audio}}</span>' % HIDDEN_FIELD_CSS_CLASS, result
        )

    def test_unhide_restores_the_exact_original_text(self):
        html = '<div class="word">{{Word}}</div>'
        hidden = set_field_hidden(html, "Word", hidden=True)
        shown = set_field_hidden(hidden, "Word", hidden=False)
        self.assertEqual(shown, html)

    def test_hiding_twice_does_not_nest_the_wrapper(self):
        html = "{{Word}}"
        once = set_field_hidden(html, "Word", hidden=True)
        twice = set_field_hidden(once, "Word", hidden=True)
        self.assertEqual(once, twice)
        self.assertEqual(twice.count("ddc-hidden"), 1)

    def test_unhiding_an_already_visible_field_is_a_no_op(self):
        html = "{{Word}}"
        self.assertEqual(set_field_hidden(html, "Word", hidden=False), html)

    def test_hiding_does_not_touch_a_conditional_guard(self):
        html = "{{#Word}}{{Word}}{{/Word}}"
        result = set_field_hidden(html, "Word", hidden=True)
        self.assertIn("{{#Word}}", result)
        self.assertIn("{{/Word}}", result)
        self.assertIn('<span class="%s">{{Word}}</span>' % HIDDEN_FIELD_CSS_CLASS, result)

    def test_every_occurrence_is_toggled_together(self):
        """A field referenced more than once must not end up half-hidden."""
        html = "{{Kanji}} ... {{Kanji}} ... {{Kanji}}"
        result = set_field_hidden(html, "Kanji", hidden=True)
        self.assertEqual(result.count(HIDDEN_FIELD_CSS_CLASS), 3)
        shown = set_field_hidden(result, "Kanji", hidden=False)
        self.assertEqual(shown, html)

    def test_a_different_field_with_a_shared_prefix_is_not_touched(self):
        """{{FrontSide}} must never be affected by toggling a field named "Front"."""
        html = "{{Front}}{{FrontSide}}"
        result = set_field_hidden(html, "Front", hidden=True)
        self.assertIn("{{FrontSide}}", result)
        self.assertNotIn(
            '<span class="%s">{{FrontSide}}</span>' % HIDDEN_FIELD_CSS_CLASS, result
        )


class TestEnsureHiddenFieldCss(unittest.TestCase):
    def test_appends_the_rule_when_missing(self):
        result = ensure_hidden_field_css(".card { color: red; }")
        self.assertIn(".%s { display: none; }" % HIDDEN_FIELD_CSS_CLASS, result)
        self.assertIn(".card { color: red; }", result)

    def test_idempotent_does_not_duplicate(self):
        once = ensure_hidden_field_css(".card {}")
        twice = ensure_hidden_field_css(once)
        self.assertEqual(once, twice)

    def test_empty_css_gets_just_the_rule(self):
        result = ensure_hidden_field_css("")
        self.assertEqual(result.strip(), ".%s { display: none; }" % HIDDEN_FIELD_CSS_CLASS)


if __name__ == "__main__":
    unittest.main()
