"""Content- and structure-based role guessing, for decks with no profile.

The deck shapes here are the two real ones this addon is developed against: a Korean/English
deck (the one that exposed every bug this module has had) and a Japanese/English deck with
ruby annotations and a pile of bookkeeping columns.
"""

from __future__ import annotations

import unittest

from addon.core.audio_fields import is_generated_field
from addon.core.role_detect import guess_role_mapping, is_converted_notetype
from addon.core.role_schema import Role
from addon.core.template_generator import (
    GENERATED_CSS_MARKER,
    TemplateOptions,
    generate_templates,
)

# A Korean deck as it ships: the Korean side is the prompt and carries pronunciation audio,
# the English side is the answer. Converting it is for someone whose Korean is native and
# who is studying English -- so English ends up on the front, and the Korean audio, which a
# Korean speaker has no use for, has to stop playing.
KO_FIELDS = [(0, "KoTerm"), (1, "KoAudio"), (2, "KoSentence"), (3, "EnTerm"), (4, "EnSentence")]
KO_FRONT = "{{KoTerm}}{{KoAudio}}<br>{{KoSentence}}"
KO_BACK = "{{FrontSide}}<hr id=answer>{{EnTerm}}<br>{{EnSentence}}"
KO_SAMPLES = {
    "KoTerm": ["물", "사과", "학교", "친구", "사랑", "시간"],
    "KoAudio": ["[sound:ko_1.mp3]", "[sound:ko_2.mp3]", "[sound:ko_3.mp3]",
                "[sound:ko_4.mp3]", "[sound:ko_5.mp3]", "[sound:ko_6.mp3]"],
    "KoSentence": [
        "저는 매일 아침에 물을 마십니다.",
        "그 사과는 정말 맛있어 보입니다.",
        "학교까지 걸어서 이십 분쯤 걸립니다.",
        "친구와 함께 영화를 보러 갔습니다.",
        "사랑은 말보다 행동으로 보여주는 것입니다.",
        "시간이 없어서 아침을 먹지 못했습니다.",
    ],
    "EnTerm": ["water", "apple", "school", "friend", "love", "time"],
    "EnSentence": [
        "I drink a glass of water every morning.",
        "That apple looks really delicious to me.",
        "It takes about twenty minutes to walk to school.",
        "I went to see a movie together with my friend.",
        "Love is shown through actions rather than words.",
        "I could not eat breakfast because there was no time.",
    ],
}


def _guess(fields, samples, front, back, css=""):
    return guess_role_mapping("T", fields, samples, front_html=front, back_html=back, css=css)


def _name(mapping, role):
    binding = mapping.first(role)
    return binding.name if binding else None


class TestKoreanDeckBeforeConversion(unittest.TestCase):
    """The side about to be promoted is Target; the side about to be demoted is Native."""

    def setUp(self):
        self.mapping = _guess(KO_FIELDS, KO_SAMPLES, KO_FRONT, KO_BACK)

    def test_the_back_becomes_the_target_side(self):
        self.assertEqual(_name(self.mapping, Role.TARGET_TERM), "EnTerm")
        self.assertEqual(_name(self.mapping, Role.TARGET_SENTENCE), "EnSentence")

    def test_the_front_becomes_the_native_side(self):
        self.assertEqual(_name(self.mapping, Role.NATIVE_TERM), "KoTerm")
        self.assertEqual(_name(self.mapping, Role.NATIVE_SENTENCE), "KoSentence")

    def test_the_languages_are_reported_the_right_way_round(self):
        self.assertEqual(self.mapping.target_language, "en")
        self.assertEqual(self.mapping.native_language, "ko")

    def test_the_decks_own_audio_never_becomes_target_audio(self):
        """The reported bug: this field holds Korean speech. Bound to the target audio role
        it would both be overwritten by English TTS and, until then, play on every card."""
        self.assertIsNone(self.mapping.first(Role.TARGET_AUDIO))
        self.assertEqual(_name(self.mapping, Role.NATIVE_AUDIO), "KoAudio")

    def test_the_old_audio_is_bound_rather_than_left_to_leak_onto_the_back(self):
        self.assertNotIn("KoAudio", [f.name for f in self.mapping.unmapped()])

    def test_the_mapping_is_usable_as_is(self):
        result = self.mapping.validate()
        self.assertTrue(result.ok, result.errors)


class TestReopeningAConvertedDeck(unittest.TestCase):
    """A full round trip: guess, convert, then guess again off the conversion's own output.

    This is the regression that matters most. The structural rule ("what's on the front is
    being demoted") is only true of a deck that hasn't been converted yet. Applied blindly to
    this addon's own output it reads the flip a second time and reports both languages
    backwards -- which then sends English TTS at the Korean fields and makes a
    just-converted deck claim it still needs converting.
    """

    def setUp(self):
        original = _guess(KO_FIELDS, KO_SAMPLES, KO_FRONT, KO_BACK)
        templates = generate_templates(original, options=TemplateOptions(include_audio=False))

        # The clone: same fields, plus the generated audio field, carrying the generated
        # templates and CSS.
        generated_audio = "ddc-audio (EN)"
        self.clone_fields = KO_FIELDS + [(5, generated_audio)]
        clone_samples = dict(KO_SAMPLES)
        clone_samples[generated_audio] = ["", "", "", "", "", ""]

        self.original = original
        self.generated_audio = generated_audio
        self.reopened = _guess(
            self.clone_fields,
            clone_samples,
            templates.front_html,
            templates.back_html,
            css=templates.css,
        )

    def test_the_conversions_own_output_is_recognised_as_converted(self):
        self.assertTrue(
            is_converted_notetype(GENERATED_CSS_MARKER, [n for _o, n in self.clone_fields])
        )

    def test_reopening_agrees_with_the_mapping_the_conversion_used(self):
        for role in (
            Role.TARGET_TERM,
            Role.TARGET_SENTENCE,
            Role.NATIVE_TERM,
            Role.NATIVE_SENTENCE,
        ):
            with self.subTest(role=role.key):
                self.assertEqual(_name(self.reopened, role), _name(self.original, role))

    def test_the_languages_are_not_inverted_on_the_second_read(self):
        self.assertEqual(self.reopened.target_language, "en")
        self.assertEqual(self.reopened.native_language, "ko")

    def test_the_generated_field_is_found_again_even_though_it_is_empty(self):
        """Nothing about its *content* identifies it -- it holds nothing until the TTS run.
        If it isn't recognised, the batch has nowhere to write and does nothing."""
        found = _name(self.reopened, Role.TARGET_AUDIO)
        self.assertEqual(found, self.generated_audio)
        self.assertTrue(is_generated_field(found))

    def test_the_original_audio_is_still_held_on_the_native_side(self):
        self.assertEqual(_name(self.reopened, Role.NATIVE_AUDIO), "KoAudio")
        self.assertIsNone(self.reopened.first(Role.TARGET_SENTENCE_AUDIO))

    def test_a_notetype_with_neither_mark_is_not_treated_as_converted(self):
        self.assertFalse(is_converted_notetype("", ["Front", "Back", "Audio"]))

    def test_a_renamed_generated_field_still_leaves_the_css_mark(self):
        """Either mark alone is enough, because either can be lost on its own."""
        self.assertTrue(is_converted_notetype(GENERATED_CSS_MARKER, ["Front", "Back"]))
        self.assertTrue(is_converted_notetype("", ["Front", "ddc-audio (EN)"]))


class TestLanguageIsDecidedByTheFieldWithEnoughTextToJudge(unittest.TestCase):
    def test_a_short_word_cannot_outvote_a_real_sentence(self):
        """``langdetect`` is confidently wrong on single words -- "water" comes back as
        Afrikaans. One field one vote would let that decide the side's language, and an
        "af" target sends the whole deck at the wrong voice."""
        mapping = _guess(KO_FIELDS, KO_SAMPLES, KO_FRONT, KO_BACK)
        self.assertEqual(mapping.target_language, "en")

    def test_audio_markup_is_kept_away_from_the_detector(self):
        """``[sound:ko_1.mp3]`` is ASCII filenames. Fed to a language detector it votes for
        a Latin language, on the side that is emphatically not Latin."""
        mapping = _guess(KO_FIELDS, KO_SAMPLES, KO_FRONT, KO_BACK)
        self.assertEqual(mapping.native_language, "ko")


class TestTheFrontEndsUpShowingOneLanguage(unittest.TestCase):
    def test_an_off_language_field_on_the_target_side_is_left_off_the_card(self):
        """A stray field in the other language, sitting on the side being promoted, would
        otherwise put two languages on the new front -- the thing the conversion exists to
        stop. It stays unmapped, so it still renders on the back and nothing is lost."""
        fields = [(0, "Ko"), (1, "En"), (2, "EnSentence"), (3, "KoStray")]
        samples = {
            "Ko": KO_SAMPLES["KoTerm"],
            "En": KO_SAMPLES["EnTerm"],
            "EnSentence": KO_SAMPLES["EnSentence"],
            "KoStray": KO_SAMPLES["KoSentence"],
        }
        mapping = _guess(
            fields, samples, "{{Ko}}", "{{FrontSide}}{{En}}{{EnSentence}}{{KoStray}}"
        )

        self.assertEqual(mapping.target_language, "en")
        target_names = {
            _name(mapping, Role.TARGET_TERM),
            _name(mapping, Role.TARGET_SENTENCE),
            _name(mapping, Role.TARGET_READING),
        }
        self.assertNotIn("KoStray", target_names)


class TestTwoFieldDeckFloor(unittest.TestCase):
    """claude.md: "two-field decks are the floor" -- a bare Front/Back deck has to work."""

    def setUp(self):
        self.mapping = _guess(
            [(0, "Front"), (1, "Back")],
            {"Front": KO_SAMPLES["KoTerm"], "Back": KO_SAMPLES["EnTerm"]},
            "{{Front}}",
            "{{FrontSide}}<hr id=answer>{{Back}}",
        )

    def test_it_produces_a_valid_card(self):
        result = self.mapping.validate()
        self.assertTrue(result.ok, result.errors)

    def test_the_back_is_promoted_to_the_front(self):
        self.assertEqual(_name(self.mapping, Role.TARGET_TERM), "Back")
        self.assertEqual(_name(self.mapping, Role.NATIVE_TERM), "Front")

    def test_a_deck_whose_templates_reference_nothing_still_maps(self):
        """Some notetypes have been hand-edited into templates that reference no field at
        all. Falling over on deck selection would be worse than a guess the user can fix."""
        mapping = _guess(
            [(0, "Front"), (1, "Back")],
            {"Front": KO_SAMPLES["KoTerm"], "Back": KO_SAMPLES["EnTerm"]},
            "",
            "",
        )
        self.assertTrue(mapping.validate().ok, mapping.validate().errors)


class TestBookkeepingFieldsStayOffTheCard(unittest.TestCase):
    def setUp(self):
        fields = [(0, "Index"), (1, "Ko"), (2, "En"), (3, "Rare")]
        samples = {
            "Index": ["1", "2", "3", "4", "5", "6"],
            "Ko": KO_SAMPLES["KoTerm"],
            "En": KO_SAMPLES["EnTerm"],
            "Rare": ["", "", "", "", "", "note"],
        }
        self.mapping = _guess(fields, samples, "{{Ko}}", "{{FrontSide}}{{En}}")

    def test_a_numeric_column_carries_no_role(self):
        self.assertEqual(self.mapping.roles_for("Index"), [])

    def test_a_nearly_always_empty_field_carries_no_role(self):
        self.assertEqual(self.mapping.roles_for("Rare"), [])

    def test_they_are_hidden_so_they_do_not_clutter_the_back(self):
        by_name = {f.name: f for f in self.mapping.fields}
        self.assertTrue(by_name["Index"].hidden)
        self.assertTrue(by_name["Rare"].hidden)
        self.assertFalse(by_name["Ko"].hidden)
        self.assertNotIn("Index", [f.name for f in self.mapping.unmapped()])


class TestRubyAnnotatedDeck(unittest.TestCase):
    """A Japanese-shaped deck: the reading is a ruby-annotated duplicate of the term."""

    def setUp(self):
        fields = [(0, "Expression"), (1, "Reading"), (2, "Meaning")]
        samples = {
            "Expression": ["新しい", "食べる", "学校", "時間", "友達", "水"],
            "Reading": ["新[あたら]しい", "食[た]べる", "学校[がっこう]",
                        "時間[じかん]", "友達[ともだち]", "水[みず]"],
            "Meaning": KO_SAMPLES["EnTerm"],
        }
        self.mapping = _guess(
            fields, samples, "{{Expression}}{{Reading}}", "{{FrontSide}}{{Meaning}}"
        )

    def test_the_ruby_field_is_read_as_a_reading_not_as_the_term(self):
        self.assertEqual(_name(self.mapping, Role.NATIVE_READING), "Reading")
        self.assertEqual(_name(self.mapping, Role.NATIVE_TERM), "Expression")

    def test_the_promoted_side_is_still_found(self):
        self.assertEqual(_name(self.mapping, Role.TARGET_TERM), "Meaning")
        self.assertTrue(self.mapping.validate().ok, self.mapping.validate().errors)


class TestNothingMatchesOnFieldNames(unittest.TestCase):
    def test_renaming_every_field_changes_nothing(self):
        """Field names are untrusted input. The same content in fields called Field 1..5
        has to produce the same mapping as the descriptively-named version."""
        renames = {
            "KoTerm": "Field 1", "KoAudio": "Field 2", "KoSentence": "Field 3",
            "EnTerm": "Field 4", "EnSentence": "Field 5",
        }
        fields = [(o, renames[n]) for o, n in KO_FIELDS]
        samples = {renames[k]: v for k, v in KO_SAMPLES.items()}
        front = "{{Field 1}}{{Field 2}}<br>{{Field 3}}"
        back = "{{FrontSide}}<hr id=answer>{{Field 4}}<br>{{Field 5}}"

        mapping = _guess(fields, samples, front, back)

        self.assertEqual(_name(mapping, Role.TARGET_TERM), "Field 4")
        self.assertEqual(_name(mapping, Role.TARGET_SENTENCE), "Field 5")
        self.assertEqual(_name(mapping, Role.NATIVE_TERM), "Field 1")
        self.assertEqual(_name(mapping, Role.NATIVE_AUDIO), "Field 2")

    def test_a_field_called_audio_that_holds_text_is_not_treated_as_audio(self):
        mapping = _guess(
            [(0, "Ko"), (1, "Audio")],
            {"Ko": KO_SAMPLES["KoTerm"], "Audio": KO_SAMPLES["EnTerm"]},
            "{{Ko}}",
            "{{FrontSide}}{{Audio}}",
        )
        self.assertEqual(_name(mapping, Role.TARGET_TERM), "Audio")
        self.assertIsNone(mapping.first(Role.NATIVE_AUDIO))


if __name__ == "__main__":
    unittest.main()
