"""Deterministic current-front/current-back extraction.

Regression coverage for the second real bug found testing the prompt: given a notetype whose
field is literally named "Front" (colliding with the Front/Back template-side vocabulary), the
model inverted which side is currently which and produced an unconverted card while claiming
success. Direction is computed here from the real template text instead of asked of the model.
"""

from __future__ import annotations

import unittest

from addon.llm.template_fields import current_sides, referenced_fields


class TestReferencedFields(unittest.TestCase):
    def test_plain_reference(self):
        self.assertEqual(referenced_fields("{{Front}}"), ["Front"])

    def test_conditional_references_strip_their_sigil(self):
        self.assertEqual(referenced_fields("{{#Front}}{{Front}}{{/Front}}"), ["Front"])

    def test_inverted_conditional(self):
        self.assertEqual(referenced_fields("{{^Front}}x{{/Front}}"), ["Front"])

    def test_filtered_reference_strips_the_filter(self):
        self.assertEqual(referenced_fields("{{text:Front}}"), ["Front"])

    def test_meta_keywords_are_excluded(self):
        self.assertEqual(referenced_fields("{{FrontSide}}{{Tags}}{{Front}}"), ["Front"])

    def test_first_seen_order_and_no_duplicates(self):
        self.assertEqual(referenced_fields("{{B}}{{A}}{{B}}"), ["B", "A"])

    def test_empty_input(self):
        self.assertEqual(referenced_fields(""), [])


class TestCurrentSides(unittest.TestCase):
    """Uses the real German test deck's actual templates -- see tests/fixtures/german.json --
    the exact case that fooled the model."""

    def test_the_german_deck_direction_the_model_got_backwards(self):
        front, back = current_sides(
            ["Front", "Back"],
            qfmt="{{Front}}",
            afmt="{{FrontSide}}\n\n<hr id=answer>\n\n{{Back}}",
        )
        self.assertEqual(front, ["Front"])
        self.assertEqual(back, ["Back"])

    def test_a_field_shown_on_both_sides_counts_as_front_only(self):
        front, back = current_sides(
            ["Word", "Note"],
            qfmt="{{Word}}",
            afmt="{{FrontSide}}<hr id=answer>{{Word}}{{Note}}",
        )
        self.assertEqual(front, ["Word"])
        self.assertEqual(back, ["Note"])

    def test_result_order_matches_known_field_order_not_template_order(self):
        front, back = current_sides(
            ["A", "B", "C"],
            qfmt="{{C}}{{A}}",
            afmt="{{FrontSide}}{{B}}",
        )
        self.assertEqual(front, ["A", "C"])
        self.assertEqual(back, ["B"])

    def test_an_unknown_field_referenced_in_the_template_is_ignored(self):
        front, back = current_sides(
            ["Front"],
            qfmt="{{Front}}{{SomeOtherFieldNotOnThisNotetype}}",
            afmt="",
        )
        self.assertEqual(front, ["Front"])
        self.assertEqual(back, [])


if __name__ == "__main__":
    unittest.main()
