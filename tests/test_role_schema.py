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
from addon.core.role_schema import FieldBinding, Role, RoleMapping, Side

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
