"""Template generation: correctness, graceful degradation, and language-agnosticism."""

from __future__ import annotations

import json
import re
import unittest

from addon.core.role_schema import FieldBinding, Role, RoleMapping, ValidationError
from addon.core.template_generator import (
    GENERATED_CSS_MARKER,
    TemplateOptions,
    generate_templates,
    referenced_fields,
    split_render_order,
)

# A synthetic "rich" profile -- readings, a sentence pair, audio, POS/notes/cloze extras,
# and several hidden bookkeeping fields, the same shape a real deck notetype has -- but with
# deck-agnostic field names, so these tests exercise generation for a rich mapping without
# depending on any specific shipped deck.
GENERIC_LIVE_FIELDS = [
    (0, "Index"), (1, "Term"), (2, "TermReading"), (3, "TermKana"),
    (4, "Meaning"), (5, "Audio"), (6, "Pos"), (7, "Note"),
    (8, "Phrase"), (9, "PhraseReading"), (10, "PhraseKana"), (11, "SentenceMeaning"),
    (12, "SentenceClozed"), (13, "SentenceAudio"), (14, "Extra"), (15, "RefIndex"),
    (16, "SentIndex"), (17, "Freq"),
]

GENERIC_PROFILE_DATA = {
    "id": "generic_rich",
    "title": "Generic rich test profile",
    "version": 1,
    "target_language": "en",
    "native_language": "ja",
    "notetype": "Generic",
    "binds_to": {
        "notetype_names": ["Generic"],
        "field_fingerprint": [name for _, name in GENERIC_LIVE_FIELDS],
    },
    "fields": [
        {"name": "Index", "ord": 0, "hidden": True},
        {"name": "Term", "ord": 1, "hidden": True},
        {"name": "TermReading", "ord": 2, "filter": "furigana", "css_class": "japanese"},
        {"name": "TermKana", "ord": 3, "hidden": True},
        {"name": "Meaning", "ord": 4},
        {"name": "Audio", "ord": 5},
        {"name": "Pos", "ord": 6},
        {"name": "Note", "ord": 7},
        {"name": "Phrase", "ord": 8, "hidden": True},
        {"name": "PhraseReading", "ord": 9, "filter": "furigana", "css_class": "japanese"},
        {"name": "PhraseKana", "ord": 10, "hidden": True},
        {"name": "SentenceMeaning", "ord": 11},
        {"name": "SentenceClozed", "ord": 12},
        {"name": "SentenceAudio", "ord": 13},
        {"name": "Extra", "ord": 14, "hidden": True},
        {"name": "RefIndex", "ord": 15, "hidden": True},
        {"name": "SentIndex", "ord": 16, "hidden": True},
        {"name": "Freq", "ord": 17, "hidden": True},
    ],
    "roles": {
        "TargetTerm": ["Meaning"],
        "TargetSentence": ["SentenceMeaning"],
        "TargetAudio": ["Audio"],
        "TargetSentenceAudio": ["SentenceAudio"],
        "NativeTerm": ["TermReading"],
        "NativeSentence": ["PhraseReading"],
        "Pos": ["Pos"],
        "Notes": ["Note"],
        "ClozeText": ["SentenceClozed"],
    },
}


def _generic_mapping(live_fields=None):
    return RoleMapping.from_profile(GENERIC_PROFILE_DATA, live_fields=live_fields)


def simple_mapping(field_names, roles, **kw):
    """Build a mapping from plain names, for tests that don't care about a real deck."""
    fields = [FieldBinding(name=n, ord=i) for i, n in enumerate(field_names)]
    by_name = {f.name: f for f in fields}
    mapping = RoleMapping(notetype_name=kw.pop("notetype_name", "Test"), fields=fields, **kw)
    for role, names in roles.items():
        for n in ([names] if isinstance(names, str) else names):
            mapping.bind(role, by_name[n])
    return mapping


def referenced(html):
    """Field names referenced by a chunk of template HTML, ignoring Anki specials."""
    out = set()
    for raw in re.findall(r"\{\{([^}]+)\}\}", html):
        name = raw.strip()
        if name.startswith(("#", "/", "^")):
            name = name[1:].strip()
        elif ":" in name:
            name = name.rsplit(":", 1)[1].strip()
        if name and name != "FrontSide":
            out.add(name)
    return out


class TestNoDanglingReferences(unittest.TestCase):
    def test_every_reference_exists_on_the_notetype(self):
        mapping = _generic_mapping(live_fields=GENERIC_LIVE_FIELDS)
        result = generate_templates(mapping)
        known = {name for _, name in GENERIC_LIVE_FIELDS}
        self.assertTrue(referenced(result.front_html) <= known)
        self.assertTrue(referenced(result.back_html) <= known)

    def test_unassigned_roles_emit_nothing(self):
        mapping = simple_mapping(
            ["Word", "Meaning"],
            {Role.TARGET_TERM: "Word", Role.NATIVE_TERM: "Meaning"},
        )
        result = generate_templates(mapping)
        combined = result.front_html + result.back_html
        # Nothing was bound to POS/Notes/audio, so nothing may appear for them.
        self.assertNotIn("ddc-pos", combined)
        self.assertNotIn("ddc-notes", combined)
        self.assertEqual(referenced(combined), {"Word", "Meaning"})

    def test_refuses_a_mapping_with_an_empty_front(self):
        mapping = simple_mapping(["A", "B"], {Role.NATIVE_TERM: "A"})
        with self.assertRaises(ValidationError):
            generate_templates(mapping)

    def test_refuses_a_mapping_with_an_empty_back(self):
        mapping = simple_mapping(["A", "B"], {Role.TARGET_TERM: "A"})
        with self.assertRaises(ValidationError):
            generate_templates(mapping)


class TestGracefulDegradation(unittest.TestCase):
    def test_two_field_deck_yields_a_minimal_card(self):
        mapping = simple_mapping(
            ["Front", "Back"],
            {Role.TARGET_TERM: "Front", Role.NATIVE_TERM: "Back"},
        )
        result = generate_templates(mapping)
        self.assertIn("{{Front}}", result.front_html)
        self.assertIn("{{Back}}", result.back_html)
        self.assertIn("{{FrontSide}}", result.back_html)
        # The prompt must be unconditional or Anki refuses to build the card.
        self.assertNotIn("{{#Front}}", result.front_html)

    def test_opaque_field_names_work(self):
        """No code path may depend on a field being *named* anything meaningful."""
        mapping = simple_mapping(
            ["Field 1", "Field 2", "Field 3"],
            {
                Role.TARGET_TERM: "Field 1",
                Role.NATIVE_TERM: "Field 2",
                Role.POS: "Field 3",
            },
        )
        result = generate_templates(mapping)
        self.assertIn("{{Field 1}}", result.front_html)
        self.assertIn("{{Field 2}}", result.back_html)
        self.assertIn("{{Field 3}}", result.front_html)

    def test_rich_mapping_yields_more_sections_than_a_minimal_one(self):
        minimal = generate_templates(
            simple_mapping(["A", "B"], {Role.TARGET_TERM: "A", Role.NATIVE_TERM: "B"})
        )
        rich = generate_templates(
            _generic_mapping(live_fields=GENERIC_LIVE_FIELDS)
        )
        self.assertGreater(len(rich.referenced_fields), len(minimal.referenced_fields))


class TestConditionalSections(unittest.TestCase):
    def test_optional_fields_are_guarded(self):
        """Empty values must not leave stray empty boxes.

        Real data: 1980 of 1983 notes have an empty Caution field, and 3 notes have empty
        audio.
        """
        mapping = simple_mapping(
            ["Term", "Gloss", "Maybe"],
            {Role.TARGET_TERM: "Term", Role.NATIVE_TERM: "Gloss", Role.NOTES: "Maybe"},
        )
        result = generate_templates(mapping)
        self.assertIn("{{#Maybe}}", result.back_html)
        self.assertIn("{{/Maybe}}", result.back_html)


class TestAudioPlacement(unittest.TestCase):
    def setUp(self):
        self.mapping = simple_mapping(
            ["Term", "Gloss", "Sound"],
            {
                Role.TARGET_TERM: "Term",
                Role.NATIVE_TERM: "Gloss",
                Role.TARGET_AUDIO: "Sound",
            },
        )

    def test_audio_is_not_referenced_at_all_by_default(self):
        """M1 default: the field still holds the demoted language's audio.

        Not on the front, not on the back -- referencing it anywhere would play the
        wrong language.
        """
        result = generate_templates(self.mapping)
        self.assertNotIn("{{Sound}}", result.front_html)
        self.assertNotIn("{{Sound}}", result.back_html)

    def test_audio_lands_on_the_front_once_included(self):
        result = generate_templates(self.mapping, options=TemplateOptions(include_audio=True))
        self.assertIn("{{Sound}}", result.front_html)
        self.assertNotIn("{{Sound}}", result.back_html)

    def test_audio_can_be_moved_to_the_back(self):
        result = generate_templates(
            self.mapping,
            options=TemplateOptions(include_audio=True, audio_on_front=False),
        )
        self.assertNotIn("{{Sound}}", result.front_html)
        self.assertIn("{{Sound}}", result.back_html)


class TestFiltersAndClasses(unittest.TestCase):
    def test_filter_comes_from_the_binding_not_from_code(self):
        fields = [
            FieldBinding("Prompt", 0),
            FieldBinding("Answer", 1, filter="furigana", css_class="japanese"),
        ]
        mapping = RoleMapping(notetype_name="T", fields=fields)
        mapping.bind(Role.TARGET_TERM, fields[0])
        mapping.bind(Role.NATIVE_TERM, fields[1])
        result = generate_templates(mapping)
        self.assertIn("{{furigana:Answer}}", result.back_html)
        self.assertIn("japanese", result.back_html)
        # A field with no declared filter is emitted bare.
        self.assertIn("{{Prompt}}", result.front_html)

    def test_arbitrary_filter_names_pass_through(self):
        """The generator must not whitelist filters -- Anki has many and adds more."""
        fields = [FieldBinding("A", 0, filter="text"), FieldBinding("B", 1, filter="hint")]
        mapping = RoleMapping(notetype_name="T", fields=fields)
        mapping.bind(Role.TARGET_TERM, fields[0])
        mapping.bind(Role.NATIVE_TERM, fields[1])
        result = generate_templates(mapping)
        self.assertIn("{{text:A}}", result.front_html)
        self.assertIn("{{hint:B}}", result.back_html)


class TestUnmappedBucket(unittest.TestCase):
    def test_unmapped_fields_render_on_the_back(self):
        mapping = simple_mapping(
            ["Term", "Gloss", "Extra"],
            {Role.TARGET_TERM: "Term", Role.NATIVE_TERM: "Gloss"},
        )
        result = generate_templates(mapping)
        self.assertIn("{{Extra}}", result.back_html)

    def test_hidden_fields_stay_out_of_the_bucket(self):
        fields = [
            FieldBinding("Term", 0),
            FieldBinding("Gloss", 1),
            FieldBinding("Bookkeeping", 2, hidden=True),
        ]
        mapping = RoleMapping(notetype_name="T", fields=fields)
        mapping.bind(Role.TARGET_TERM, fields[0])
        mapping.bind(Role.NATIVE_TERM, fields[1])
        result = generate_templates(mapping)
        self.assertNotIn("{{Bookkeeping}}", result.back_html)

    def test_bucket_can_be_disabled(self):
        mapping = simple_mapping(
            ["Term", "Gloss", "Extra"],
            {Role.TARGET_TERM: "Term", Role.NATIVE_TERM: "Gloss"},
        )
        result = generate_templates(mapping, options=TemplateOptions(include_unmapped=False))
        self.assertNotIn("{{Extra}}", result.back_html)


class TestCss(unittest.TestCase):
    SOURCE = "@font-face { font-family: stroke; src: url('_stroke.ttf'); }\n.card { color: White; }"

    def _mapping(self):
        return simple_mapping(["A", "B"], {Role.TARGET_TERM: "A", Role.NATIVE_TERM: "B"})

    def test_source_css_is_preserved_verbatim(self):
        """Source CSS routinely points at media-folder fonts; losing it breaks the card."""
        result = generate_templates(self._mapping(), source_css=self.SOURCE)
        self.assertIn("_stroke.ttf", result.css)
        self.assertIn(".card { color: White; }", result.css)

    def test_regeneration_is_idempotent(self):
        once = generate_templates(self._mapping(), source_css=self.SOURCE).css
        twice = generate_templates(self._mapping(), source_css=once).css
        self.assertEqual(once, twice)
        self.assertEqual(twice.count(GENERATED_CSS_MARKER), 1)


class TestGeneralisesBeyondTheStartingDeck(unittest.TestCase):
    """A different language pair, different field names, no readings, no audio."""

    def test_latin_to_latin_deck(self):
        mapping = simple_mapping(
            ["Palabra", "Traduccion", "Ejemplo", "Ejemplo EN"],
            {
                Role.TARGET_TERM: "Palabra",
                Role.TARGET_SENTENCE: "Ejemplo",
                Role.NATIVE_TERM: "Traduccion",
                Role.NATIVE_SENTENCE: "Ejemplo EN",
            },
            target_language="es",
            native_language="en",
        )
        result = generate_templates(mapping)
        self.assertIn("{{Palabra}}", result.front_html)
        self.assertIn("{{Traduccion}}", result.back_html)
        self.assertNotIn("furigana", result.front_html + result.back_html)

    def test_sentence_only_deck_puts_the_sentence_on_the_front(self):
        mapping = simple_mapping(
            ["TargetSent", "NativeSent"],
            {Role.TARGET_SENTENCE: "TargetSent", Role.NATIVE_SENTENCE: "NativeSent"},
        )
        result = generate_templates(mapping)
        self.assertIn("{{TargetSent}}", result.front_html)
        self.assertNotIn("{{#TargetSent}}", result.front_html)


class TestGoldenRichProfile(unittest.TestCase):
    """Pin the actual output for a rich, real-shaped mapping, so layout changes are
    deliberate rather than accidental."""

    def setUp(self):
        self.result = generate_templates(
            _generic_mapping(live_fields=GENERIC_LIVE_FIELDS)
        )

    def test_front_is_the_target_language_prompt(self):
        front = self.result.front_html
        self.assertIn("{{Meaning}}", front)
        self.assertIn("{{Pos}}", front)
        self.assertIn("{{SentenceMeaning}}", front)
        # Nothing from the native side may leak onto the prompt.
        for leaked in ("TermReading", "Term", "PhraseReading", "Phrase"):
            self.assertNotIn(leaked, front)

    def test_back_reveals_the_native_side(self):
        back = self.result.back_html
        self.assertIn("{{furigana:TermReading}}", back)
        self.assertIn("{{furigana:PhraseReading}}", back)
        self.assertIn("{{FrontSide}}", back)

    def test_demoted_language_audio_is_not_referenced_anywhere(self):
        """M1 drops the old audio from the template; M5 refills the fields with TTS."""
        combined = self.result.front_html + self.result.back_html
        self.assertNotIn("{{Audio}}", combined)
        self.assertNotIn("{{SentenceAudio}}", combined)

    def test_bookkeeping_fields_are_not_dumped_onto_the_card(self):
        back = self.result.back_html
        for noise in ("Index", "RefIndex", "Freq", "SentIndex"):
            self.assertNotIn(noise, back)

    def test_unbound_hidden_field_is_excluded(self):
        self.assertNotIn("{{Extra}}", self.result.back_html)

    def test_note_is_guarded(self):
        """An optional field must not leave a stray empty box when blank."""
        self.assertIn("{{#Note}}", self.result.back_html)


class TestProfileRoundTrip(unittest.TestCase):
    def test_to_profile_then_from_profile_is_stable(self):
        original = _generic_mapping(live_fields=GENERIC_LIVE_FIELDS)
        round_tripped = RoleMapping.from_profile(
            json.loads(json.dumps(original.to_profile())), live_fields=GENERIC_LIVE_FIELDS
        )
        self.assertEqual(
            generate_templates(original).front_html,
            generate_templates(round_tripped).front_html,
        )
        self.assertEqual(
            generate_templates(original).back_html,
            generate_templates(round_tripped).back_html,
        )


class TestCustomOrder(unittest.TestCase):
    """front_order/back_order (the single-screen redesign's drag-reorderable field list)."""

    def _rich_mapping(self):
        return simple_mapping(
            ["Word", "Kana", "Type", "Sentence", "Meaning", "MeaningReading", "MeaningSentence", "Pic", "Extra"],
            {
                Role.TARGET_TERM: "Word",
                Role.TARGET_READING: "Kana",
                Role.POS: "Type",
                Role.TARGET_SENTENCE: "Sentence",
                Role.NATIVE_TERM: "Meaning",
                Role.NATIVE_READING: "MeaningReading",
                Role.NATIVE_SENTENCE: "MeaningSentence",
                Role.IMAGE: "Pic",
                Role.NOTES: "Extra",
            },
        )

    def test_default_order_is_unchanged_when_no_custom_order_given(self):
        mapping = self._rich_mapping()
        default = generate_templates(mapping)
        explicit_none = generate_templates(
            mapping, options=TemplateOptions(front_order=None, back_order=None)
        )
        self.assertEqual(default.front_html, explicit_none.front_html)
        self.assertEqual(default.back_html, explicit_none.back_html)

    def test_front_blocks_follow_a_custom_order(self):
        mapping = self._rich_mapping()
        result = generate_templates(
            mapping,
            options=TemplateOptions(
                front_order=[Role.POS, Role.TARGET_SENTENCE, Role.TARGET_TERM, Role.TARGET_READING]
            ),
        )
        front = result.front_html
        self.assertLess(front.index("Type"), front.index("Sentence"))
        self.assertLess(front.index("Sentence"), front.index("{{Word}}"))
        self.assertLess(front.index("{{Word}}"), front.index("Kana"))

    def test_back_blocks_follow_a_custom_order(self):
        mapping = self._rich_mapping()
        result = generate_templates(
            mapping,
            options=TemplateOptions(
                back_order=[Role.NOTES, Role.NATIVE_TERM, Role.IMAGE, Role.NATIVE_SENTENCE, Role.NATIVE_READING]
            ),
        )
        back = result.back_html
        self.assertLess(back.index("Extra"), back.index("{{Meaning}}"))
        self.assertLess(back.index("{{Meaning}}"), back.index("Pic"))
        self.assertLess(back.index("Pic"), back.index("MeaningSentence"))

    def test_a_role_missing_from_a_partial_order_still_renders(self):
        """A partial custom order can reorder blocks but must never drop one."""
        mapping = self._rich_mapping()
        result = generate_templates(
            mapping, options=TemplateOptions(front_order=[Role.TARGET_SENTENCE])
        )
        front = result.front_html
        # Sentence was pulled to the front of the order, but Term/Reading/Pos still appear.
        self.assertIn("{{Word}}", front)
        self.assertIn("Kana", front)
        self.assertIn("Type", front)
        self.assertLess(front.index("Sentence"), front.index("{{Word}}"))

    def test_primary_prompt_stays_unconditional_regardless_of_position(self):
        """The front must never be able to render fully empty, no matter the order."""
        mapping = self._rich_mapping()
        result = generate_templates(
            mapping, options=TemplateOptions(front_order=[Role.POS, Role.TARGET_TERM])
        )
        self.assertNotIn("{{#Word}}", result.front_html)
        self.assertIn("{{Word}}</div>", result.front_html)

    def test_custom_order_cannot_move_a_role_to_the_other_side(self):
        mapping = self._rich_mapping()
        result = generate_templates(
            mapping, options=TemplateOptions(front_order=[Role.NATIVE_TERM, Role.TARGET_TERM])
        )
        # NATIVE_TERM isn't a valid front role, so it's ignored there, not relocated.
        self.assertNotIn("{{Meaning}}", result.front_html)
        self.assertIn("{{Meaning}}", result.back_html)


class TestSplitRenderOrder(unittest.TestCase):
    def test_none_round_trips_to_none(self):
        self.assertEqual(split_render_order(None), (None, None))

    def test_splits_by_side(self):
        order = [Role.NOTES, Role.TARGET_SENTENCE, Role.NATIVE_TERM, Role.POS]
        front, back = split_render_order(order)
        self.assertEqual(front, [Role.TARGET_SENTENCE, Role.POS])
        self.assertEqual(back, [Role.NOTES, Role.NATIVE_TERM])

    def test_audio_roles_follow_audio_on_front(self):
        order = [Role.TARGET_AUDIO, Role.TARGET_TERM, Role.TARGET_SENTENCE_AUDIO]
        front, back = split_render_order(order, audio_on_front=True)
        self.assertEqual(front, [Role.TARGET_AUDIO, Role.TARGET_TERM, Role.TARGET_SENTENCE_AUDIO])
        self.assertEqual(back, [])

        front, back = split_render_order(order, audio_on_front=False)
        self.assertEqual(front, [Role.TARGET_TERM])
        self.assertEqual(back, [Role.TARGET_AUDIO, Role.TARGET_SENTENCE_AUDIO])

    def test_result_feeds_generate_templates_directly(self):
        mapping = simple_mapping(
            ["Word", "Type", "Meaning"],
            {Role.TARGET_TERM: "Word", Role.POS: "Type", Role.NATIVE_TERM: "Meaning"},
        )
        front_order, back_order = split_render_order([Role.POS, Role.TARGET_TERM])
        result = generate_templates(
            mapping, options=TemplateOptions(front_order=front_order, back_order=back_order)
        )
        self.assertLess(result.front_html.index("Type"), result.front_html.index("{{Word}}"))


class TestReferencedFields(unittest.TestCase):
    """Promoted from a private helper to public API (``ui/main_screen.py`` uses it to check
    whether a *live*, on-disk template already shows a given field -- the "has this deck
    actually been converted yet" gate on the "Generate TTS audio" button)."""

    def test_plain_reference(self):
        self.assertEqual(referenced_fields("{{Word}}"), ["Word"])

    def test_filtered_reference(self):
        self.assertEqual(referenced_fields("{{furigana:Reading}}"), ["Reading"])

    def test_conditional_sections_count_as_references(self):
        self.assertEqual(referenced_fields("{{#Sentence}}{{Sentence}}{{/Sentence}}"), ["Sentence"])

    def test_builtin_anki_fields_are_excluded(self):
        self.assertEqual(referenced_fields("{{FrontSide}}{{Tags}}{{Word}}"), ["Word"])

    def test_absent_field_is_not_reported(self):
        self.assertNotIn("Meaning", referenced_fields("{{Word}}"))

    def test_multiple_html_chunks_are_combined_without_duplicates(self):
        self.assertEqual(
            referenced_fields("{{Word}}", "{{Word}}{{Meaning}}"), ["Word", "Meaning"]
        )


if __name__ == "__main__":
    unittest.main()
