"""Deterministic audio-safety enforcement on the LLM's generated templates.

Regression test for a bug caught in the very first real run of the prompt (see
docs/llm-notes.md and the commit history): given a deck where one field mixes real text with an
embedded `[sound:...]` reference (`"stellen [sound:oddcast-....mp3]"`), the model referenced that
field with a bare `{{Front}}` instead of `{{text:Front}}` -- which would play the deck's own,
original-direction audio on the newly-converted card. Rather than trying to prompt a 1.5B model
into reliably making this judgment call, whether a field's audio may ever play is enforced here,
deterministically, after the model responds -- so the model's mistake can never reach a real card.
"""

from __future__ import annotations

import unittest

from addon.llm.audio_safety import enforce_audio_safety, field_has_sound, sound_field_names
from addon.llm.prompt import FieldSample


class TestTheCardNeverPlaysTheOldAudio(unittest.TestCase):
    """The exact real bug: a real, captured model response for the German test deck."""

    def test_bare_reference_to_a_mixed_text_and_audio_field_is_rewritten(self):
        # Exactly what the model produced for the "stellen [sound:...mp3]" field, before
        # prompt.py pre-sanitized the samples it was shown.
        front = (
            '<div class="word">{{Front}}</div>\n'
            "{{#ddc-audio}}{{ddc-audio}}{{/ddc-audio}}\n"
            '{{#Front}}<hr id=answer>{{FrontSide}}</div>{{/Front}}'
        )

        fixed = enforce_audio_safety(front, sound_fields={"Front"}, keep="ddc-audio")

        self.assertIn("{{text:Front}}", fixed)
        self.assertNotIn("{{Front}}", fixed)

    def test_the_model_still_wrote_a_bare_reference_after_being_told_explicitly_not_to(self):
        # After prompt.py started pre-stripping [sound:...] from the shown samples AND telling
        # the model outright which fields need {{text:Field}}, the model *still* wrote a bare
        # {{Front}}. This is why the backstop exists unconditionally rather than only as a
        # fallback for cases the model wasn't told about: it isn't reliable even when told.
        front = '<div class="word">{{Front}}</div>\n{{#ddc-audio}}{{ddc-audio}}{{/ddc-audio}}'

        fixed = enforce_audio_safety(front, sound_fields={"Front"}, keep="ddc-audio")

        self.assertIn("{{text:Front}}", fixed)
        self.assertNotIn("{{Front}}", fixed)

    def test_conditional_guards_are_left_alone(self):
        front = "{{#Front}}{{Front}}{{/Front}}"
        fixed = enforce_audio_safety(front, sound_fields={"Front"}, keep="ddc-audio")
        self.assertIn("{{#Front}}", fixed)
        self.assertIn("{{/Front}}", fixed)
        self.assertIn("{{text:Front}}", fixed)

    def test_inverted_conditional_guard_is_left_alone(self):
        html = "{{^Front}}<i>no audio yet</i>{{/Front}}"
        fixed = enforce_audio_safety(html, sound_fields={"Front"}, keep="ddc-audio")
        self.assertEqual(html, fixed)

    def test_the_generated_audio_field_is_never_rewritten(self):
        html = "{{#ddc-audio}}{{ddc-audio}}{{/ddc-audio}}"
        # Even if ddc-audio somehow ended up in sound_fields, `keep` must win.
        fixed = enforce_audio_safety(html, sound_fields={"Front", "ddc-audio"}, keep="ddc-audio")
        self.assertIn("{{ddc-audio}}", fixed)
        self.assertNotIn("{{text:ddc-audio}}", fixed)

    def test_a_field_with_no_audio_is_untouched(self):
        html = "<div>{{Back}}</div>"
        fixed = enforce_audio_safety(html, sound_fields={"Front"}, keep="ddc-audio")
        self.assertEqual(html, fixed)

    def test_already_text_filtered_is_idempotent(self):
        html = "<div>{{text:Front}}</div>"
        fixed = enforce_audio_safety(html, sound_fields={"Front"}, keep="ddc-audio")
        self.assertEqual("<div>{{text:Front}}</div>", fixed)

    def test_a_different_filter_is_overridden_to_text(self):
        html = "<div>{{furigana:Front}}</div>"
        fixed = enforce_audio_safety(html, sound_fields={"Front"}, keep="ddc-audio")
        self.assertEqual("<div>{{text:Front}}</div>", fixed)

    def test_applies_equally_to_the_back_template(self):
        back = "{{FrontSide}}<hr id=answer>{{Front}}"
        fixed = enforce_audio_safety(back, sound_fields={"Front"}, keep="ddc-audio")
        self.assertIn("{{text:Front}}", fixed)


class TestFrontFieldNameNeverCollidesWithFrontSide(unittest.TestCase):
    """A field literally named "Front" must never cause {{FrontSide}} (the template-only
    keyword, unrelated to any field) to be mistaken for a reference to it -- the real German
    test deck has exactly this field name, which is what caused the model's own confusion in
    the first place; the enforcement layer must not repeat that confusion structurally."""

    def test_frontside_keyword_is_never_matched_as_the_front_field(self):
        html = "{{FrontSide}}<hr id=answer>{{Front}}"
        fixed = enforce_audio_safety(html, sound_fields={"Front"}, keep="ddc-audio")
        self.assertIn("{{FrontSide}}", fixed)
        self.assertIn("{{text:Front}}", fixed)

    def test_a_field_name_that_is_a_prefix_of_another_field_is_not_confused(self):
        html = "{{Term}}{{TermExtended}}"
        fixed = enforce_audio_safety(html, sound_fields={"Term"}, keep="ddc-audio")
        self.assertEqual("{{text:Term}}{{TermExtended}}", fixed)


class TestSoundFieldDetection(unittest.TestCase):
    def test_field_has_sound_true_for_mixed_text_and_audio(self):
        self.assertTrue(field_has_sound(["stellen [sound:oddcast-abc123.mp3]"]))

    def test_field_has_sound_false_for_plain_text(self):
        self.assertFalse(field_has_sound(["to place, set"]))

    def test_sound_field_names_scans_real_samples(self):
        fields = [
            FieldSample(name="Front", samples=("stellen [sound:x.mp3]", "stehen [sound:y.mp3]")),
            FieldSample(name="Back", samples=("to place, set", "to stand")),
        ]
        self.assertEqual(sound_field_names(fields), frozenset({"Front"}))


if __name__ == "__main__":
    unittest.main()
