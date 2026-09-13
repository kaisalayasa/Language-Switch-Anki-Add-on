"""Role schema: binding integrity and the refusal to guess when a notetype has drifted."""

from __future__ import annotations

import os
import unittest

from addon.core.profiles import (
    BUILTIN_PROFILE_DIR,
    MatchQuality,
    load_profile_file,
    load_profiles,
    match_profile,
)
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

CORE2000_JSON = os.path.join(BUILTIN_PROFILE_DIR, "core2000.json")

LIVE = [
    (0, "Optimized-Voc-Index"), (1, "Vocabulary-Kanji"), (2, "Vocabulary-Furigana"),
    (3, "Vocabulary-Kana"), (4, "Vocabulary-English"), (5, "Vocabulary-Audio"),
    (6, "Vocabulary-Pos"), (7, "Caution"), (8, "Expression"), (9, "Reading"),
    (10, "Sentence-Kana"), (11, "Sentence-English"), (12, "Sentence-Clozed"),
    (13, "Sentence-Audio"), (14, "Notes"), (15, "Core-Index"),
    (16, "Optimized-Sent-Index"), (17, "Frequency"),
]


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
        """The native side needs readings and audio too -- see docs/deck-facts.md."""
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

    def test_core2000_profile_validates_against_the_real_notetype(self):
        mapping = load_profile_file(CORE2000_JSON).to_mapping(live_fields=LIVE)
        result = mapping.validate_against(LIVE)
        self.assertTrue(result.ok, result.errors)


class TestRefusesToGuess(unittest.TestCase):
    """A silently misaligned field map would write wrong content into every note."""

    def test_renamed_field_is_refused(self):
        drifted = [(o, "RENAMED" if o == 4 else n) for o, n in LIVE]
        mapping = load_profile_file(CORE2000_JSON).to_mapping()
        result = mapping.validate_against(drifted)
        self.assertFalse(result.ok)
        self.assertTrue(any("refusing to guess" in e for e in result.errors))

    def test_reordered_fields_are_refused(self):
        swapped = list(LIVE)
        swapped[4], swapped[5] = (4, LIVE[5][1]), (5, LIVE[4][1])
        mapping = load_profile_file(CORE2000_JSON).to_mapping()
        result = mapping.validate_against(swapped)
        self.assertFalse(result.ok)
        self.assertTrue(any("refusing to guess" in e for e in result.errors))

    def test_missing_field_is_refused(self):
        truncated = LIVE[:-1]
        mapping = load_profile_file(CORE2000_JSON).to_mapping()
        result = mapping.validate_against(truncated)
        self.assertFalse(result.ok)


class TestUnmapped(unittest.TestCase):
    def test_unmapped_excludes_assigned_and_hidden(self):
        mapping = load_profile_file(CORE2000_JSON).to_mapping(live_fields=LIVE)
        names = [b.name for b in mapping.unmapped()]
        self.assertNotIn("Vocabulary-English", names)   # assigned
        self.assertNotIn("Frequency", names)            # hidden
        self.assertIn("Frequency", [b.name for b in mapping.unmapped(include_hidden=True)])


class TestAssignmentRoundTrip(unittest.TestCase):
    """The pure helpers M3's RoleMapperDialog is built on: flat UI rows <-> RoleMapping."""

    def test_round_trips_the_core2000_profile_through_the_table_shape(self):
        original = load_profile_file(CORE2000_JSON).to_mapping(live_fields=LIVE)
        rebuilt = mapping_from_assignments(
            original.notetype_name,
            assignments_from_mapping(original),
            target_language=original.target_language,
            native_language=original.native_language,
        )
        self.assertEqual(rebuilt.to_profile(), original.to_profile())
        self.assertTrue(rebuilt.validate_against(LIVE).ok)

    def test_blank_assignments_produce_an_all_unassigned_mapping(self):
        assignments = [FieldAssignment(name=n, ord=o) for o, n in LIVE]
        mapping = mapping_from_assignments("T", assignments)
        self.assertEqual(mapping.assignments, {})
        self.assertEqual(len(mapping.unmapped(include_hidden=True)), len(LIVE))

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
        """Per claude.md's "audio is a replacement, not an addition" -- native audio is
        never (re)synthesized by this addon."""
        self.assertEqual(
            set(AUDIO_SOURCE_ROLES),
            {Role.TARGET_AUDIO, Role.TARGET_SENTENCE_AUDIO},
        )

    def test_pairs_audio_with_its_matching_content_role(self):
        self.assertIs(AUDIO_SOURCE_ROLES[Role.TARGET_AUDIO], Role.TARGET_TERM)
        self.assertIs(AUDIO_SOURCE_ROLES[Role.TARGET_SENTENCE_AUDIO], Role.TARGET_SENTENCE)


class TestProfileMatching(unittest.TestCase):
    def test_exact_fingerprint_match_is_usable(self):
        match = match_profile("Core 2000", LIVE)
        self.assertEqual(match.quality, MatchQuality.EXACT)
        self.assertTrue(match.usable)

    def test_name_match_with_wrong_fields_is_not_usable(self):
        """Same notetype name, different schema -- route to the mapper UI, don't force it."""
        match = match_profile("Core 2000", [(0, "Front"), (1, "Back")])
        self.assertEqual(match.quality, MatchQuality.NAME_ONLY)
        self.assertFalse(match.usable)

    def test_unknown_notetype_matches_nothing(self):
        match = match_profile("Some Random Deck", [(0, "Front"), (1, "Back")])
        self.assertEqual(match.quality, MatchQuality.NONE)
        self.assertIsNone(match.profile)
        self.assertFalse(match.usable)

    def test_builtin_profiles_all_load_and_validate(self):
        profiles = load_profiles()
        self.assertTrue(profiles, "no built-in profiles found")
        for profile in profiles:
            with self.subTest(profile=profile.id):
                mapping = profile.to_mapping()
                self.assertTrue(mapping.validate().ok, mapping.validate().errors)


if __name__ == "__main__":
    unittest.main()
