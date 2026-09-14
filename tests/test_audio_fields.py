"""The audio policy: generated audio goes in a field this addon made, and audio the deck
already had is kept but never rendered.

These are regression tests for a bug that reached real use. A Korean deck was converted for
someone learning English, TTS produced nothing, and the cards played the original Korean
audio anyway -- because the target audio role pointed at the deck's *existing* audio field,
which still held that Korean audio, and the template got switched on regardless of whether
synthesis had actually written anything over it.
"""

from __future__ import annotations

import unittest

from addon.core.audio_fields import (
    AUDIO_DONE_TAG,
    generated_field_name,
    generated_field_role,
    is_generated_field,
    needed_audio_roles,
    plan_generated_fields,
    resolve_audio_fields,
    sound_field_names,
)
from addon.core.role_schema import FieldBinding, Role, RoleMapping
from addon.core.template_generator import TemplateOptions, generate_templates


def _mapping(field_names, roles, *, target_language="en", native_language="ko"):
    fields = [FieldBinding(name=n, ord=i) for i, n in enumerate(field_names)]
    by_name = {f.name: f for f in fields}
    mapping = RoleMapping(
        notetype_name="T",
        fields=fields,
        target_language=target_language,
        native_language=native_language,
    )
    for role, names in roles.items():
        for name in names if isinstance(names, (list, tuple)) else [names]:
            mapping.bind(role, by_name[name])
    return mapping


class TestNaming(unittest.TestCase):
    def test_a_generated_name_is_recognised_as_its_own_role(self):
        for role in (Role.TARGET_AUDIO, Role.TARGET_SENTENCE_AUDIO):
            name = generated_field_name(role, language="en")
            with self.subTest(role=role):
                self.assertTrue(is_generated_field(name))
                self.assertIs(generated_field_role(name), role)

    def test_word_and_sentence_audio_are_never_confused(self):
        """The sentence stem contains the word stem's shape; a sloppy prefix test would
        read ``ddc-sentence-audio`` as word audio and write the wrong speech into it."""
        word = generated_field_name(Role.TARGET_AUDIO, language="en")
        sentence = generated_field_name(Role.TARGET_SENTENCE_AUDIO, language="en")
        self.assertNotEqual(word, sentence)
        self.assertIs(generated_field_role(word), Role.TARGET_AUDIO)
        self.assertIs(generated_field_role(sentence), Role.TARGET_SENTENCE_AUDIO)

    def test_a_decks_own_fields_are_never_mistaken_for_generated_ones(self):
        for name in ("Audio", "Vocabulary-Audio", "Sentence-Audio", "Front", "ddc", ""):
            with self.subTest(name=name):
                self.assertFalse(is_generated_field(name))

    def test_the_name_avoids_colliding_with_a_field_that_is_already_there(self):
        first = generated_field_name(Role.TARGET_AUDIO, language="en")
        second = generated_field_name(Role.TARGET_AUDIO, language="en", taken=[first])
        self.assertNotEqual(first, second)
        self.assertIs(generated_field_role(second), Role.TARGET_AUDIO)

    def test_recognition_does_not_depend_on_the_declared_language(self):
        """The language is a label for humans. Changing it later must not orphan the field
        the audio already lives in."""
        named_for_english = generated_field_name(Role.TARGET_AUDIO, language="en")
        self.assertIs(generated_field_role(named_for_english), Role.TARGET_AUDIO)
        self.assertIs(
            generated_field_role(generated_field_name(Role.TARGET_AUDIO)), Role.TARGET_AUDIO
        )

    def test_the_done_tag_is_importable_from_here(self):
        """It moved so the conversion can strip it off duplicates; the batch still uses it."""
        from addon.ops.tts_batch import AUDIO_DONE_TAG as from_batch

        self.assertEqual(AUDIO_DONE_TAG, from_batch)


class TestWhichFieldsGetCreated(unittest.TestCase):
    def test_audio_is_planned_for_whatever_there_is_to_say(self):
        mapping = _mapping(
            ["En", "EnSent", "Ko"],
            {
                Role.TARGET_TERM: "En",
                Role.TARGET_SENTENCE: "EnSent",
                Role.NATIVE_TERM: "Ko",
            },
        )
        self.assertEqual(
            needed_audio_roles(mapping), [Role.TARGET_AUDIO, Role.TARGET_SENTENCE_AUDIO]
        )
        self.assertEqual([role for role, _n in plan_generated_fields(mapping)],
                         [Role.TARGET_AUDIO, Role.TARGET_SENTENCE_AUDIO])

    def test_a_deck_with_no_sentence_gets_no_sentence_audio_field(self):
        mapping = _mapping(
            ["En", "Ko"], {Role.TARGET_TERM: "En", Role.NATIVE_TERM: "Ko"}
        )
        self.assertEqual([role for role, _n in plan_generated_fields(mapping)],
                         [Role.TARGET_AUDIO])

    def test_converting_an_already_converted_deck_adds_nothing(self):
        """Idempotence: a second conversion must not pile up duplicate audio fields."""
        existing = generated_field_name(Role.TARGET_AUDIO, language="en")
        mapping = _mapping(
            ["En", "Ko", existing], {Role.TARGET_TERM: "En", Role.NATIVE_TERM: "Ko"}
        )
        self.assertEqual(plan_generated_fields(mapping), [])


class TestExistingAudioIsDemoted(unittest.TestCase):
    def test_an_existing_field_is_never_left_as_target_audio(self):
        """The bug, at its narrowest. A profile written before this policy still names the
        deck's own audio field for TargetAudio; TTS would overwrite the original recording,
        and until it did, the card would play it."""
        mapping = _mapping(
            ["En", "Ko", "KoAudio"],
            {
                Role.TARGET_TERM: "En",
                Role.NATIVE_TERM: "Ko",
                Role.TARGET_AUDIO: "KoAudio",
            },
        )
        resolved = resolve_audio_fields(mapping)

        self.assertIsNone(resolved.first(Role.TARGET_AUDIO))
        self.assertEqual(resolved.first(Role.NATIVE_AUDIO).name, "KoAudio")

    def test_the_generated_field_takes_over_and_the_old_one_stays_put(self):
        mapping = _mapping(
            ["En", "Ko", "KoAudio"],
            {
                Role.TARGET_TERM: "En",
                Role.NATIVE_TERM: "Ko",
                Role.TARGET_AUDIO: "KoAudio",
            },
        )
        planned = plan_generated_fields(mapping)
        resolved = resolve_audio_fields(mapping, extra_fields=planned)

        generated = resolved.first(Role.TARGET_AUDIO)
        self.assertIsNotNone(generated)
        self.assertTrue(is_generated_field(generated.name))
        self.assertEqual(resolved.first(Role.NATIVE_AUDIO).name, "KoAudio")
        self.assertTrue(resolved.validate().ok, resolved.validate().errors)

    def test_an_unmapped_audio_field_is_bound_rather_than_left_loose(self):
        """An unmapped field is dumped onto the back by the generator so nothing is lost --
        which for an audio field means it still plays. Binding it to a native role is what
        actually hides it."""
        mapping = _mapping(
            ["En", "Ko", "KoAudio"], {Role.TARGET_TERM: "En", Role.NATIVE_TERM: "Ko"}
        )
        self.assertIn("KoAudio", [f.name for f in mapping.unmapped()])

        resolved = resolve_audio_fields(mapping, sound_fields={"KoAudio"})
        self.assertEqual(resolved.first(Role.NATIVE_AUDIO).name, "KoAudio")
        self.assertNotIn("KoAudio", [f.name for f in resolved.unmapped()])

    def test_a_field_that_already_has_a_real_role_is_not_stolen(self):
        """Some decks put audio in the same field as the text. Whatever the field was for
        wins; this policy only claims fields nothing else has claimed."""
        mapping = _mapping(
            ["En", "Ko"], {Role.TARGET_TERM: "En", Role.NATIVE_TERM: "Ko"}
        )
        resolved = resolve_audio_fields(mapping, sound_fields={"Ko"})
        self.assertEqual(resolved.first(Role.NATIVE_TERM).name, "Ko")
        self.assertIsNone(resolved.first(Role.NATIVE_AUDIO))

    def test_the_input_mapping_is_left_alone(self):
        mapping = _mapping(
            ["En", "Ko", "KoAudio"],
            {Role.TARGET_TERM: "En", Role.NATIVE_TERM: "Ko", Role.TARGET_AUDIO: "KoAudio"},
        )
        resolve_audio_fields(mapping, extra_fields=plan_generated_fields(mapping))
        self.assertEqual(mapping.first(Role.TARGET_AUDIO).name, "KoAudio")
        self.assertEqual(len(mapping.fields), 3)


class TestTheCardNeverPlaysTheOldAudio(unittest.TestCase):
    """The end-to-end shape of the reported bug, checked against the real generator."""

    def _converted_templates(self, *, include_audio):
        mapping = _mapping(
            ["KoTerm", "KoAudio", "EnTerm", "EnSent"],
            {
                Role.TARGET_TERM: "EnTerm",
                Role.TARGET_SENTENCE: "EnSent",
                Role.NATIVE_TERM: "KoTerm",
                Role.TARGET_AUDIO: "KoAudio",  # what the old design would have bound
            },
        )
        resolved = resolve_audio_fields(
            mapping,
            sound_fields={"KoAudio"},
            extra_fields=plan_generated_fields(mapping),
        )
        return resolved, generate_templates(
            resolved, options=TemplateOptions(include_audio=include_audio)
        )

    def test_the_original_audio_field_is_on_neither_side_of_the_card(self):
        for include_audio in (False, True):
            with self.subTest(include_audio=include_audio):
                _resolved, templates = self._converted_templates(include_audio=include_audio)
                self.assertNotIn("KoAudio", templates.front_html)
                self.assertNotIn("KoAudio", templates.back_html)
                self.assertNotIn("KoAudio", templates.referenced_fields)

    def test_the_generated_field_is_what_the_card_plays_once_audio_is_on(self):
        resolved, templates = self._converted_templates(include_audio=True)
        generated = resolved.first(Role.TARGET_AUDIO).name
        self.assertIn(generated, templates.referenced_fields)

    def test_nothing_plays_before_the_audio_has_been_generated(self):
        """With audio off, no audio field of any kind is referenced -- and the field that
        will hold it is empty anyway, which is the whole point of creating a new one."""
        resolved, templates = self._converted_templates(include_audio=False)
        generated = resolved.first(Role.TARGET_AUDIO).name
        self.assertNotIn(generated, templates.referenced_fields)


class TestSoundDetection(unittest.TestCase):
    def test_only_fields_that_really_hold_audio_are_reported(self):
        found = sound_field_names(
            {
                "A": ["[sound:a.mp3]", ""],
                "B": ["plain text", "more text"],
                "C": ["", ""],
                "D": ["word [sound:d.wav] after"],
            }
        )
        self.assertEqual(found, {"A", "D"})


if __name__ == "__main__":
    unittest.main()
