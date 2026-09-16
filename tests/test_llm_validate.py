"""``llm.validate``: catching the concrete ways the model's output has actually been observed
to go wrong, using real bugs captured while testing this prompt against Qwen2.5-7B.
"""

from __future__ import annotations

import unittest

from addon.llm.response import ParsedResponse
from addon.llm.validate import validate_response


def _codes(problems):
    return {p.code for p in problems}


class TestValidResponsePassesClean(unittest.TestCase):
    def test_a_correct_response_has_no_problems(self):
        parsed = ParsedResponse(
            description="A vocab deck.",
            speak_text_from="ExampleSentence",
            front='<div class="word">{{Word}}</div>'
                  '{{#ExampleSentence}}<div>{{ExampleSentence}}</div>{{/ExampleSentence}}'
                  '{{#ddc-audio}}{{ddc-audio}}{{/ddc-audio}}',
            back='{{FrontSide}}<hr id=answer><div>{{Translation}}</div>',
            css=".card { font-size: 20px; }",
        )
        problems = validate_response(
            parsed,
            known_field_names=["Word", "Translation", "ExampleSentence"],
            new_front_fields=["Word", "ExampleSentence"],
            new_back_fields=["Translation"],
            audio_field_name="ddc-audio",
        )
        self.assertEqual(problems, [])


class TestUnknownFieldReferences(unittest.TestCase):
    def test_real_bug_hallucinated_per_word_fields_on_german_deck(self):
        """Real captured bug: instead of referencing the actual "Front" field, the model
        invented separate fields named after each sample verb."""
        parsed = ParsedResponse(
            description="d", speak_text_from="Back",
            front='<div class="verb">{{Back}}</div>',
            back='{{FrontSide}}<hr id=answer>'
                 '{{#stellen}}{{stellen}}{{/stellen}}'
                 '{{#stehen}}{{stehen}}{{/stehen}}',
            css=".card {}",
        )
        problems = validate_response(
            parsed,
            known_field_names=["Front", "Back"],
            new_front_fields=["Back"],
            new_back_fields=["Front"],
            audio_field_name="ddc-audio",
        )
        self.assertIn("unknown_field", _codes(problems))

    def test_a_typo_in_a_real_field_name_is_caught(self):
        parsed = ParsedResponse(
            description="d", speak_text_from="Word",
            front="<div>{{Wrod}}</div>",
            back="{{FrontSide}}<hr id=answer><div>{{Translation}}</div>",
            css=".card {}",
        )
        problems = validate_response(
            parsed,
            known_field_names=["Word", "Translation"],
            new_front_fields=["Word"],
            new_back_fields=["Translation"],
            audio_field_name="ddc-audio",
        )
        self.assertIn("unknown_field", _codes(problems))

    def test_referencing_the_audio_field_is_allowed(self):
        parsed = ParsedResponse(
            description="d", speak_text_from="Word",
            front="<div>{{Word}}</div>{{#ddc-audio}}{{ddc-audio}}{{/ddc-audio}}",
            back="{{FrontSide}}<hr id=answer><div>{{Translation}}</div>",
            css=".card {}",
        )
        problems = validate_response(
            parsed,
            known_field_names=["Word", "Translation"],
            new_front_fields=["Word"],
            new_back_fields=["Translation"],
            audio_field_name="ddc-audio",
        )
        self.assertEqual(problems, [])


class TestUnbalancedConditionals(unittest.TestCase):
    def test_real_bug_unclosed_inverted_conditional_on_korean_deck(self):
        """Real captured bug: {{^Picture}} was opened but never closed."""
        parsed = ParsedResponse(
            description="d", speak_text_from="English",
            front="<div>{{English}}</div>",
            back='{{FrontSide}}<hr id=answer>'
                 '<div class="picture">{{#Picture}}<img src="{{Picture}}">{{/Picture}}{{^Picture}}</div>',
            css=".card {}",
        )
        problems = validate_response(
            parsed,
            known_field_names=["English", "Korean", "Picture"],
            new_front_fields=["English"],
            new_back_fields=["Korean", "Picture"],
            audio_field_name="ddc-audio",
        )
        self.assertIn("unbalanced_conditional", _codes(problems))

    def test_close_tag_with_no_matching_open_is_caught(self):
        parsed = ParsedResponse(
            description="d", speak_text_from="Word",
            front="<div>{{Word}}</div>{{/Word}}",
            back="{{FrontSide}}<hr id=answer>",
            css=".card {}",
        )
        problems = validate_response(
            parsed, known_field_names=["Word"], new_front_fields=["Word"],
            new_back_fields=[], audio_field_name="ddc-audio",
        )
        self.assertIn("unbalanced_conditional", _codes(problems))

    def test_mismatched_close_tag_name_is_caught(self):
        parsed = ParsedResponse(
            description="d", speak_text_from="Word",
            front="{{#Word}}<div>{{Word}}</div>{{/Other}}",
            back="{{FrontSide}}<hr id=answer>",
            css=".card {}",
        )
        problems = validate_response(
            parsed, known_field_names=["Word", "Other"], new_front_fields=["Word"],
            new_back_fields=["Other"], audio_field_name="ddc-audio",
        )
        self.assertIn("unbalanced_conditional", _codes(problems))

    def test_properly_nested_conditionals_are_not_flagged(self):
        parsed = ParsedResponse(
            description="d", speak_text_from="Word",
            front="{{#Word}}<div>{{#Pos}}<b>{{Pos}}</b>{{/Pos}}{{Word}}</div>{{/Word}}",
            back="{{FrontSide}}<hr id=answer>",
            css=".card {}",
        )
        problems = validate_response(
            parsed, known_field_names=["Word", "Pos"], new_front_fields=["Word", "Pos"],
            new_back_fields=[], audio_field_name="ddc-audio",
        )
        self.assertNotIn("unbalanced_conditional", _codes(problems))


class TestEmptyFront(unittest.TestCase):
    def test_front_with_only_conditional_content_is_flagged(self):
        parsed = ParsedResponse(
            description="d", speak_text_from="Word",
            front="{{#Word}}<div>{{Word}}</div>{{/Word}}",
            back="{{FrontSide}}<hr id=answer><div>{{Translation}}</div>",
            css=".card {}",
        )
        problems = validate_response(
            parsed, known_field_names=["Word", "Translation"], new_front_fields=["Word"],
            new_back_fields=["Translation"], audio_field_name="ddc-audio",
        )
        self.assertIn("empty_front", _codes(problems))

    def test_front_with_at_least_one_unconditional_reference_is_fine(self):
        parsed = ParsedResponse(
            description="d", speak_text_from="Word",
            front="<div>{{Word}}</div>{{#Pos}}<b>{{Pos}}</b>{{/Pos}}",
            back="{{FrontSide}}<hr id=answer>",
            css=".card {}",
        )
        problems = validate_response(
            parsed, known_field_names=["Word", "Pos"], new_front_fields=["Word", "Pos"],
            new_back_fields=[], audio_field_name="ddc-audio",
        )
        self.assertNotIn("empty_front", _codes(problems))


class TestPlacementViolations(unittest.TestCase):
    def test_real_bug_inverted_direction_on_german_deck(self):
        """Real captured bug: field placement inverted outright -- the given BACK field
        rendered on the FRONT template and vice versa."""
        parsed = ParsedResponse(
            description="d", speak_text_from="Front",
            front='<div class="verb">{{Front}}</div>',
            back='{{FrontSide}}<hr id=answer><div class="english">{{Back}}</div>',
            css=".card {}",
        )
        problems = validate_response(
            parsed,
            known_field_names=["Front", "Back"],
            new_front_fields=["Back"],
            new_back_fields=["Front"],
            audio_field_name="ddc-audio",
        )
        codes = _codes(problems)
        self.assertIn("misplaced_field", codes)
        self.assertIn("invalid_speak_text_from", codes)

    def test_a_front_field_referenced_directly_on_back_is_caught(self):
        parsed = ParsedResponse(
            description="d", speak_text_from="Word",
            front="<div>{{Word}}</div>",
            back="{{FrontSide}}<hr id=answer><div>{{Word}}</div>",
            css=".card {}",
        )
        problems = validate_response(
            parsed, known_field_names=["Word", "Translation"], new_front_fields=["Word"],
            new_back_fields=["Translation"], audio_field_name="ddc-audio",
        )
        self.assertIn("misplaced_field", _codes(problems))

    def test_frontside_pulling_front_content_into_back_is_not_a_violation(self):
        """{{FrontSide}} is how every real Back template legitimately repeats the front --
        must never be mistaken for a direct field reference."""
        parsed = ParsedResponse(
            description="d", speak_text_from="Word",
            front="<div>{{Word}}</div>",
            back="{{FrontSide}}<hr id=answer><div>{{Translation}}</div>",
            css=".card {}",
        )
        problems = validate_response(
            parsed, known_field_names=["Word", "Translation"], new_front_fields=["Word"],
            new_back_fields=["Translation"], audio_field_name="ddc-audio",
        )
        self.assertEqual(problems, [])


class TestSpeakTextFrom(unittest.TestCase):
    def test_speak_text_from_naming_a_back_field_is_caught(self):
        parsed = ParsedResponse(
            description="d", speak_text_from="Translation",
            front="<div>{{Word}}</div>",
            back="{{FrontSide}}<hr id=answer><div>{{Translation}}</div>",
            css=".card {}",
        )
        problems = validate_response(
            parsed, known_field_names=["Word", "Translation"], new_front_fields=["Word"],
            new_back_fields=["Translation"], audio_field_name="ddc-audio",
        )
        self.assertIn("invalid_speak_text_from", _codes(problems))


if __name__ == "__main__":
    unittest.main()
