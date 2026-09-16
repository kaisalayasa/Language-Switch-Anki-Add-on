"""Naming and recognition for the one field this addon creates to hold newly-generated audio.

The actual audio-safety policy (old audio is never played, generated audio is never stripped)
now lives in ``addon/llm/audio_safety.py`` and is covered by
``tests/test_llm_audio_safety.py::TestTheCardNeverPlaysTheOldAudio`` -- including the real
regression this whole area of the addon exists because of. This file covers only what's left
here: naming the field this addon creates, and recognising it again later.
"""

from __future__ import annotations

import unittest

from addon.core.audio_fields import (
    AUDIO_DONE_TAG,
    GENERATED_AUDIO_FIELD,
    generated_field_name,
    is_generated_field,
)


class TestNaming(unittest.TestCase):
    def test_the_default_name_is_the_fixed_constant(self):
        self.assertEqual(generated_field_name(), GENERATED_AUDIO_FIELD)
        self.assertTrue(is_generated_field(GENERATED_AUDIO_FIELD))

    def test_a_decks_own_fields_are_never_mistaken_for_generated_ones(self):
        for name in ("Audio", "Vocabulary-Audio", "Sentence-Audio", "Front", "ddc", ""):
            with self.subTest(name=name):
                self.assertFalse(is_generated_field(name))

    def test_the_name_avoids_colliding_with_a_field_that_is_already_there(self):
        """Only matters if a notetype somehow already has a field with this exact name --
        e.g. converting an already-converted notetype a second time."""
        first = generated_field_name()
        second = generated_field_name(taken=[first])
        self.assertNotEqual(first, second)
        self.assertTrue(is_generated_field(second))

    def test_recognition_is_case_insensitive(self):
        self.assertTrue(is_generated_field(GENERATED_AUDIO_FIELD.upper()))

    def test_the_done_tag_is_importable_from_here(self):
        """It moved so the conversion can strip it off duplicates; the batch still uses it."""
        from addon.ops.tts_batch import AUDIO_DONE_TAG as from_batch

        self.assertEqual(AUDIO_DONE_TAG, from_batch)


if __name__ == "__main__":
    unittest.main()
