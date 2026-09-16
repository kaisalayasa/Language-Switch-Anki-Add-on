"""``llm.direction``: deterministic direction/placement resolution, which front fields get their
own generated audio field, and the ``known_state`` parameter that keeps re-analyzing an
already-converted notetype idempotent.

Sample text is deliberately unambiguous (whole Spanish/English sentences, not single words) so
``detect_field_language`` is confident every time -- see ``direction.py``'s own docstring on why
short text is unreliable, and ``test_llm_analyze.py`` for the same convention.
"""

from __future__ import annotations

import unittest

from addon.core.deck_state import ConversionState
from addon.llm.direction import AudioTarget, resolve_direction
from addon.llm.prompt import FieldSample

_WORD_ES = FieldSample(
    name="Word", samples=("Buenos dias amigo", "Hola como estas", "Adios y buena suerte")
)
_TRANSLATION_EN = FieldSample(
    name="Translation",
    samples=("Good morning friend", "Hello how are you", "Goodbye and good luck"),
)

# A never-converted deck: Spanish term on front (native side, to be demoted), English
# translation on back (target side, to be promoted).
_QFMT_UNCONVERTED = "{{Word}}"
_AFMT_UNCONVERTED = "{{FrontSide}}<hr id=answer>{{Translation}}"

# The SAME deck's templates as they'd look immediately AFTER a correct first conversion:
# Translation (English, now the target) is on the front, Word (Spanish, now the native side)
# is on the back -- swapped relative to the unconverted templates above.
_QFMT_CONVERTED = "{{Translation}}"
_AFMT_CONVERTED = "{{FrontSide}}<hr id=answer>{{Word}}"


class TestFreshDeckNoKnownState(unittest.TestCase):
    """The normal, first-ever-Analyze case: nothing is known yet, so target/native language
    comes from which side each field is currently on -- current front is native (to be
    demoted), current back is target (to be promoted)."""

    def test_current_back_language_becomes_target_and_moves_to_new_front(self):
        direction = resolve_direction(
            [_WORD_ES, _TRANSLATION_EN], _QFMT_UNCONVERTED, _AFMT_UNCONVERTED
        )
        self.assertEqual(direction.target_language, "en")
        self.assertEqual(direction.native_language, "es")
        self.assertEqual(direction.new_front_fields, ("Translation",))
        self.assertEqual(direction.new_back_fields, ("Word",))

    def test_every_new_front_field_gets_its_own_audio_target(self):
        direction = resolve_direction(
            [_WORD_ES, _TRANSLATION_EN], _QFMT_UNCONVERTED, _AFMT_UNCONVERTED
        )
        self.assertEqual(
            direction.audio_targets, (AudioTarget("Translation", "ddc-audio-Translation"),)
        )


class TestReanalyzingWithoutKnownStateDoubleFlips(unittest.TestCase):
    """Documents the exact bug ``known_state`` exists to prevent. Fed the templates as they'd
    really look right after a correct first conversion (target-language field now on front),
    resolve_direction with no known_state still reads "currently on front" as native
    unconditionally -- true the first time, wrong on every re-analysis. The result: the fields
    a first conversion correctly placed get flipped right back to the original direction.
    """

    def test_an_already_converted_structure_gets_read_backwards(self):
        direction = resolve_direction(
            [_WORD_ES, _TRANSLATION_EN], _QFMT_CONVERTED, _AFMT_CONVERTED
        )
        # Translation (English) was correctly on the front after conversion; without
        # known_state this reads it as the native side and demotes it back to the back,
        # while promoting Word (Spanish) back to the front -- exactly undoing conversion #1.
        self.assertEqual(direction.target_language, "es")
        self.assertEqual(direction.native_language, "en")
        self.assertEqual(direction.new_front_fields, ("Word",))
        self.assertEqual(direction.new_back_fields, ("Translation",))


class TestKnownStateMakesReanalyzingIdempotent(unittest.TestCase):
    """The fix: given the recorded target/native language directly, placement no longer
    depends on (mis)reading current template structure at all."""

    def test_fields_already_on_the_target_side_stay_there(self):
        known_state = ConversionState(target_language="en", native_language="es")
        direction = resolve_direction(
            [_WORD_ES, _TRANSLATION_EN], _QFMT_CONVERTED, _AFMT_CONVERTED, known_state=known_state
        )
        self.assertEqual(direction.target_language, "en")
        self.assertEqual(direction.native_language, "es")
        # Matches _QFMT_CONVERTED/_AFMT_CONVERTED exactly -- no flip.
        self.assertEqual(direction.new_front_fields, ("Translation",))
        self.assertEqual(direction.new_back_fields, ("Word",))

    def test_known_state_is_used_even_if_current_structure_disagrees(self):
        """known_state is authoritative, not just a tie-breaker -- even fed the *unconverted*
        qfmt/afmt, a given known_state wins. Realistic case: a css marker survived while the
        card layout itself was hand-edited into some transitional state."""
        known_state = ConversionState(target_language="en", native_language="es")
        direction = resolve_direction(
            [_WORD_ES, _TRANSLATION_EN], _QFMT_UNCONVERTED, _AFMT_UNCONVERTED,
            known_state=known_state,
        )
        self.assertEqual(direction.target_language, "en")
        self.assertEqual(direction.native_language, "es")


class TestGeneratedAudioFieldsAreExcludedFromPlacement(unittest.TestCase):
    """A field this addon already created for audio (recognised via
    ``core.audio_fields.is_generated_field``) is real, sampled data once a notetype has been
    converted and re-analyzed -- but it isn't content to place, and the model is never told
    about it at all (see direction.py's module docstring)."""

    def test_a_pre_existing_generated_audio_field_is_excluded_from_both_sides(self):
        audio_sample = FieldSample(name="ddc-audio-Word", samples=("", "", ""))
        known_state = ConversionState(target_language="en", native_language="es")
        direction = resolve_direction(
            [_WORD_ES, _TRANSLATION_EN, audio_sample], _QFMT_CONVERTED, _AFMT_CONVERTED,
            known_state=known_state,
        )
        self.assertNotIn("ddc-audio-Word", direction.new_front_fields)
        self.assertNotIn("ddc-audio-Word", direction.new_back_fields)

    def test_its_source_field_still_gets_the_exact_same_audio_target_recomputed(self):
        """Re-analyzing must propose reusing the field that's already there, not a new one --
        this is what makes it safe to run repeatedly without ever accumulating
        "ddc-audio-Word 2"."""
        audio_sample = FieldSample(name="ddc-audio-Word", samples=("", "", ""))
        known_state = ConversionState(target_language="es", native_language="en")
        direction = resolve_direction(
            [_WORD_ES, _TRANSLATION_EN, audio_sample], _QFMT_CONVERTED, _AFMT_CONVERTED,
            known_state=known_state,
        )
        # target=es this time, so Word (Spanish) is the real front-content field paired with
        # the audio field that's already there.
        self.assertIn(AudioTarget("Word", "ddc-audio-Word"), direction.audio_targets)


if __name__ == "__main__":
    unittest.main()
