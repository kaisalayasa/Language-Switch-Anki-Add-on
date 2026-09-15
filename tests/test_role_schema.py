"""Role schema: binding integrity and the refusal to guess when a notetype has drifted."""

from __future__ import annotations

import unittest

from addon.core.role_schema import (
    AUDIO_SOURCE_ROLES,
    FieldAssignment,
    FieldBinding,
    Role,
    RoleMapping,
    Side,
    assignments_from_mapping,
    mapping_from_assignments,
)

# A synthetic "rich" profile -- same shape as a real deck notetype (readings, a sentence
# pair, audio, POS/notes/cloze extras, and several hidden bookkeeping fields) but with
# deck-agnostic field names, so these tests exercise the generic profile machinery without
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


class TestRoleKeys(unittest.TestCase):
    def test_round_trip_for_every_role(self):
        for role in Role:
            self.assertIs(Role.from_key(role.key), role)

    def test_accepts_forgiving_spellings(self):
        self.assertIs(Role.from_key("TargetTerm"), Role.TARGET_TERM)
        self.assertIs(Role.from_key("TARGET_TERM"), Role.TARGET_TERM)
        self.assertIs(Role.from_key("target_term"), Role.TARGET_TERM)
        self.assertIs(Role.from_key("TargetSentenceAudio"), Role.TARGET_SENTENCE_AUDIO)
        self.assertIs(Role.from_key("ClozeText"), Role.CLOZE_TEXT)
        self.assertIs(Role.from_key("Pos"), Role.POS)

    def test_unknown_role_is_rejected(self):
        with self.assertRaises(KeyError):
            Role.from_key("NotARole")

    def test_roles_are_symmetric_across_both_sides(self):
        """The native side needs readings and audio too -- a reading-heavy target language
        (e.g. Japanese) can end up on either side depending on conversion direction."""
        target = {r.kind for r in Role if r.side is Side.TARGET}
        native = {r.kind for r in Role if r.side is Side.NATIVE}
        self.assertEqual(target, native)

    def test_no_role_is_a_silent_alias_of_another(self):
        """Regression: Enum members sharing a value collapse into one another.

        POS/Notes/ClozeText/Image once all carried ``(NEUTRAL, META)``, which made them
        the *same* member -- so binding a field to Notes also bound it to POS and its
        content rendered three times on the card.
        """
        self.assertEqual(
            len(Role.__members__),
            len(set(Role)),
            "some Role members are aliases: %s"
            % sorted(n for n, m in Role.__members__.items() if m.name != n),
        )
        values = [r.value for r in Role.__members__.values()]
        self.assertEqual(len(values), len(set(values)), "duplicate Role values")

    def test_meta_roles_are_distinct_members(self):
        for a, b in [
            (Role.POS, Role.NOTES),
            (Role.POS, Role.CLOZE_TEXT),
            (Role.NOTES, Role.IMAGE),
            (Role.CLOZE_TEXT, Role.IMAGE),
        ]:
            self.assertIsNot(a, b)


class TestValidation(unittest.TestCase):
    def test_binding_to_a_missing_field_is_an_error(self):
        mapping = RoleMapping(notetype_name="T", fields=[FieldBinding("A", 0)])
        mapping.bind(Role.TARGET_TERM, FieldBinding("A", 0))
        mapping.bind(Role.NATIVE_TERM, FieldBinding("Ghost", 7))
        result = mapping.validate()
        self.assertFalse(result.ok)
        self.assertTrue(any("Ghost" in e for e in result.errors))

    def test_ord_disagreement_is_an_error(self):
        mapping = RoleMapping(notetype_name="T", fields=[FieldBinding("A", 0), FieldBinding("B", 1)])
        mapping.bind(Role.TARGET_TERM, FieldBinding("A", 0))
        mapping.bind(Role.NATIVE_TERM, FieldBinding("B", 5))  # wrong ord
        result = mapping.validate()
        self.assertFalse(result.ok)

    def test_a_rich_profile_validates_against_its_real_notetype(self):
        mapping = _generic_mapping(live_fields=GENERIC_LIVE_FIELDS)
        result = mapping.validate_against(GENERIC_LIVE_FIELDS)
        self.assertTrue(result.ok, result.errors)


class TestRefusesToGuess(unittest.TestCase):
    """A silently misaligned field map would write wrong content into every note."""

    def test_renamed_field_is_refused(self):
        drifted = [(o, "RENAMED" if o == 4 else n) for o, n in GENERIC_LIVE_FIELDS]
        mapping = _generic_mapping()
        result = mapping.validate_against(drifted)
        self.assertFalse(result.ok)
        self.assertTrue(any("refusing to guess" in e for e in result.errors))

    def test_reordered_fields_are_refused(self):
        swapped = list(GENERIC_LIVE_FIELDS)
        swapped[4], swapped[5] = (4, GENERIC_LIVE_FIELDS[5][1]), (5, GENERIC_LIVE_FIELDS[4][1])
        mapping = _generic_mapping()
        result = mapping.validate_against(swapped)
        self.assertFalse(result.ok)
        self.assertTrue(any("refusing to guess" in e for e in result.errors))

    def test_missing_field_is_refused(self):
        truncated = GENERIC_LIVE_FIELDS[:-1]
        mapping = _generic_mapping()
        result = mapping.validate_against(truncated)
        self.assertFalse(result.ok)


class TestUnmapped(unittest.TestCase):
    def test_unmapped_excludes_assigned_and_hidden(self):
        mapping = _generic_mapping(live_fields=GENERIC_LIVE_FIELDS)
        names = [b.name for b in mapping.unmapped()]
        self.assertNotIn("Meaning", names)  # assigned
        self.assertNotIn("Freq", names)     # hidden
        self.assertIn("Freq", [b.name for b in mapping.unmapped(include_hidden=True)])


class TestAssignmentRoundTrip(unittest.TestCase):
    """The pure helpers M3's RoleMapperDialog is built on: flat UI rows <-> RoleMapping."""

    def test_round_trips_a_rich_profile_through_the_table_shape(self):
        original = _generic_mapping(live_fields=GENERIC_LIVE_FIELDS)
        rebuilt = mapping_from_assignments(
            original.notetype_name,
            assignments_from_mapping(original),
            target_language=original.target_language,
            native_language=original.native_language,
        )
        self.assertEqual(rebuilt.to_profile(), original.to_profile())
        self.assertTrue(rebuilt.validate_against(GENERIC_LIVE_FIELDS).ok)

    def test_blank_assignments_produce_an_all_unassigned_mapping(self):
        assignments = [FieldAssignment(name=n, ord=o) for o, n in GENERIC_LIVE_FIELDS]
        mapping = mapping_from_assignments("T", assignments)
        self.assertEqual(mapping.assignments, {})
        self.assertEqual(len(mapping.unmapped(include_hidden=True)), len(GENERIC_LIVE_FIELDS))

    def test_assigning_a_role_makes_the_field_show_up_under_that_role(self):
        assignments = [
            FieldAssignment(name="Front", ord=0, role=Role.TARGET_TERM),
            FieldAssignment(name="Back", ord=1, role=Role.NATIVE_TERM),
        ]
        mapping = mapping_from_assignments("Basic", assignments)
        self.assertEqual(mapping.first(Role.TARGET_TERM).name, "Front")
        self.assertEqual(mapping.first(Role.NATIVE_TERM).name, "Back")
        self.assertTrue(mapping.validate().ok)

    def test_hidden_and_passthrough_fields_survive_the_round_trip(self):
        assignments = [
            FieldAssignment(name="Front", ord=0, role=Role.TARGET_TERM),
            FieldAssignment(name="Junk", ord=1, hidden=True),
            FieldAssignment(name="Reading", ord=2, role=Role.NATIVE_TERM,
                             filter="furigana", css_class="japanese"),
        ]
        mapping = mapping_from_assignments("T", assignments)
        junk = next(f for f in mapping.fields if f.name == "Junk")
        self.assertTrue(junk.hidden)
        self.assertNotIn("Junk", [b.name for b in mapping.unmapped()])
        reading = mapping.first(Role.NATIVE_TERM)
        self.assertEqual(reading.filter, "furigana")
        self.assertEqual(reading.css_class, "japanese")

    def test_multi_role_field_keeps_first_role_only_when_flattened(self):
        """Documented v1 limitation: the table shows one role per field, so flattening a
        (data-model-legal but currently unused) multi-role field picks one deterministically
        rather than silently dropping the field or crashing.
        """
        mapping = RoleMapping(notetype_name="T", fields=[FieldBinding("A", 0)])
        binding = FieldBinding("A", 0)
        mapping.bind(Role.TARGET_TERM, binding)
        mapping.bind(Role.TARGET_SENTENCE, binding)
        rows = assignments_from_mapping(mapping)
        self.assertEqual(len(rows), 1)
        self.assertIn(rows[0].role, (Role.TARGET_TERM, Role.TARGET_SENTENCE))


class TestAudioSourceRoles(unittest.TestCase):
    """M5's synthesis pairing: which text role backs which audio role."""

    def test_only_target_side_audio_is_synthesized(self):
        """Per CLAUDE.md's "audio is a replacement, not an addition" -- native audio is
        never (re)synthesized by this addon."""
        self.assertEqual(
            set(AUDIO_SOURCE_ROLES),
            {Role.TARGET_AUDIO, Role.TARGET_SENTENCE_AUDIO},
        )

    def test_pairs_audio_with_its_matching_content_role(self):
        self.assertIs(AUDIO_SOURCE_ROLES[Role.TARGET_AUDIO], Role.TARGET_TERM)
        self.assertIs(AUDIO_SOURCE_ROLES[Role.TARGET_SENTENCE_AUDIO], Role.TARGET_SENTENCE)


class TestRenderOrder(unittest.TestCase):
    """The single-screen redesign's drag order, persisted through a saved profile (M6)."""

    def _mapping(self):
        word = FieldBinding(name="Word", ord=0)
        meaning = FieldBinding(name="Meaning", ord=1)
        mapping = RoleMapping(notetype_name="Basic", fields=[word, meaning])
        mapping.bind(Role.TARGET_TERM, word)
        mapping.bind(Role.NATIVE_TERM, meaning)
        return mapping

    def test_defaults_to_none(self):
        self.assertIsNone(self._mapping().render_order)

    def test_round_trips_through_to_profile_and_from_profile(self):
        mapping = self._mapping()
        mapping.render_order = [Role.POS, Role.TARGET_TERM, Role.NOTES]

        data = mapping.to_profile()
        self.assertEqual(data["render_order"], ["Pos", "TargetTerm", "Notes"])

        rebuilt = RoleMapping.from_profile(data)
        self.assertEqual(rebuilt.render_order, [Role.POS, Role.TARGET_TERM, Role.NOTES])

    def test_none_round_trips_to_none(self):
        data = self._mapping().to_profile()
        self.assertIsNone(data["render_order"])
        rebuilt = RoleMapping.from_profile(data)
        self.assertIsNone(rebuilt.render_order)

    def test_missing_key_in_hand_written_profile_json_defaults_to_none(self):
        """A profile written before render_order existed has no such key at all."""
        data = self._mapping().to_profile()
        del data["render_order"]
        rebuilt = RoleMapping.from_profile(data)
        self.assertIsNone(rebuilt.render_order)


if __name__ == "__main__":
    unittest.main()
