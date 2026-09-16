"""Naming and recognition for the fields this addon creates to hold newly-generated audio.

The actual audio-safety policy (old audio is never played, generated audio is never stripped)
now lives in ``addon/llm/audio_safety.py`` and is covered by
``tests/test_llm_audio_safety.py::TestTheCardNeverPlaysTheOldAudio`` -- including the real
regression this whole area of the addon exists because of. This file covers only what's left
here: naming the fields this addon creates (one per real front-content source field), and
recognising them again later.
"""

from __future__ import annotations

import unittest

from addon.core.audio_fields import (
    AUDIO_DONE_TAG,
    AUDIO_FIELD_PREFIX,
    generated_audio_field_name,
    is_generated_field,
)


class TestNaming(unittest.TestCase):
    def test_name_is_prefixed_by_the_source_field_name(self):
        self.assertEqual(generated_audio_field_name("Word"), "ddc-audio-Word")
        self.assertTrue(is_generated_field(generated_audio_field_name("Word")))

    def test_different_source_fields_get_different_names(self):
        self.assertNotEqual(
            generated_audio_field_name("Word"), generated_audio_field_name("ExampleSentence")
        )

    def test_calling_it_again_for_the_same_source_field_returns_the_same_name(self):
        """Deterministic and idempotent -- re-analyzing an already-converted notetype must
        propose reusing the exact same field, never mint a new one."""
        self.assertEqual(generated_audio_field_name("Word"), generated_audio_field_name("Word"))

    def test_a_decks_own_fields_are_never_mistaken_for_generated_ones(self):
        for name in ("Audio", "Vocabulary-Audio", "Sentence-Audio", "Front", "ddc", ""):
            with self.subTest(name=name):
                self.assertFalse(is_generated_field(name))

    def test_the_bare_prefix_alone_is_still_recognised(self):
        """Defensive: every name this module actually mints has a source-field suffix, but
        recognition itself must not depend on one being present."""
        self.assertTrue(is_generated_field(AUDIO_FIELD_PREFIX))

    def test_recognition_is_case_insensitive(self):
        self.assertTrue(is_generated_field(generated_audio_field_name("Word").upper()))

    def test_the_done_tag_is_importable_from_here(self):
        """It moved so the conversion can strip it off duplicates; the batch still uses it."""
        from addon.ops.tts_batch import AUDIO_DONE_TAG as from_batch

        self.assertEqual(AUDIO_DONE_TAG, from_batch)


if __name__ == "__main__":
    unittest.main()
