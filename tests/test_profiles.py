"""Profile save/load: turning a hand-built RoleMapping into a reusable JSON profile, and
making sure a user's saved profile takes precedence over a shipped one describing the same
notetype (see claude.md / M6 -- "generalizes to arbitrary decks" only holds if a mapping
built once doesn't have to be rebuilt every time)."""

from __future__ import annotations

import json
import os
import tempfile
import unittest

from addon.core.profiles import (
    MatchQuality,
    Profile,
    load_profile_file,
    load_profiles,
    match_profile,
    save_profile,
    slugify,
)
from addon.core.role_schema import FieldBinding, Role, RoleMapping

LIVE_FIELDS = [(0, "Front"), (1, "Back")]


def _two_field_mapping() -> RoleMapping:
    front = FieldBinding(name="Front", ord=0)
    back = FieldBinding(name="Back", ord=1)
    mapping = RoleMapping(notetype_name="Basic", fields=[front, back])
    mapping.bind(Role.TARGET_TERM, front)
    mapping.bind(Role.NATIVE_TERM, back)
    return mapping


class TestSlugify(unittest.TestCase):
    def test_lowercases_and_joins_with_underscores(self):
        self.assertEqual(slugify("My Spanish Deck"), "my_spanish_deck")

    def test_collapses_runs_of_punctuation(self):
        self.assertEqual(slugify("Core 2000!!  (English Front)"), "core_2000_english_front")

    def test_strips_leading_and_trailing_underscores(self):
        self.assertEqual(slugify("--hello--"), "hello")

    def test_falls_back_to_profile_for_empty_or_punctuation_only_input(self):
        self.assertEqual(slugify(""), "profile")
        self.assertEqual(slugify("   "), "profile")
        self.assertEqual(slugify("!!!"), "profile")


class TestSaveProfile(unittest.TestCase):
    def test_writes_a_file_load_profile_file_can_read_back(self):
        mapping = _two_field_mapping()
        mapping.target_language = "es"
        mapping.native_language = "en"
        with tempfile.TemporaryDirectory() as tmp:
            path = save_profile(
                mapping, id="My Deck", title="My Deck", description="a test profile", directory=tmp
            )
            self.assertTrue(os.path.isfile(path))
            self.assertEqual(os.path.basename(path), "my_deck.json")

            profile = load_profile_file(path)
            self.assertEqual(profile.id, "My Deck")
            self.assertEqual(profile.title, "My Deck")
            self.assertEqual(profile.data["description"], "a test profile")
            self.assertEqual(profile.data["target_language"], "es")
            self.assertEqual(profile.data["native_language"], "en")
            self.assertEqual(profile.fingerprint, ["Front", "Back"])
            self.assertEqual(profile.notetype_names, ["Basic"])
            self.assertEqual(
                profile.data["roles"]["TargetTerm"], ["Front"]
            )

            rebuilt = profile.to_mapping(live_fields=LIVE_FIELDS)
            self.assertTrue(rebuilt.validate().ok)

    def test_overwrites_an_existing_file_at_the_same_path(self):
        mapping = _two_field_mapping()
        with tempfile.TemporaryDirectory() as tmp:
            first = save_profile(mapping, id="deck", title="First title", directory=tmp)
            second = save_profile(mapping, id="deck", title="Second title", directory=tmp)
            self.assertEqual(first, second)
            with open(second, "r", encoding="utf-8") as fh:
                data = json.load(fh)
            self.assertEqual(data["title"], "Second title")

    def test_creates_the_directory_if_missing(self):
        mapping = _two_field_mapping()
        with tempfile.TemporaryDirectory() as tmp:
            nested = os.path.join(tmp, "nested", "profiles")
            path = save_profile(mapping, id="deck", title="Deck", directory=nested)
            self.assertTrue(os.path.isfile(path))


class TestLoadProfilesMultiDirectory(unittest.TestCase):
    def test_merges_directories_in_the_order_given(self):
        with tempfile.TemporaryDirectory() as user_dir, tempfile.TemporaryDirectory() as builtin_dir:
            save_profile(_two_field_mapping(), id="user_one", title="User One", directory=user_dir)
            save_profile(_two_field_mapping(), id="builtin_one", title="Builtin One", directory=builtin_dir)

            profiles = load_profiles((user_dir, builtin_dir))
            self.assertEqual([p.id for p in profiles], ["user_one", "builtin_one"])

    def test_missing_directory_is_skipped_not_an_error(self):
        with tempfile.TemporaryDirectory() as builtin_dir:
            save_profile(_two_field_mapping(), id="only_one", title="Only One", directory=builtin_dir)
            missing = os.path.join(builtin_dir, "does-not-exist")
            profiles = load_profiles((missing, builtin_dir))
            self.assertEqual([p.id for p in profiles], ["only_one"])

    def test_accepts_a_single_directory_string_like_before(self):
        with tempfile.TemporaryDirectory() as tmp:
            save_profile(_two_field_mapping(), id="deck", title="Deck", directory=tmp)
            profiles = load_profiles(tmp)
            self.assertEqual([p.id for p in profiles], ["deck"])


class TestMatchProfilePrecedence(unittest.TestCase):
    def test_first_listed_profile_wins_on_an_exact_fingerprint_tie(self):
        user_profile = Profile(id="user", title="User", data=_two_field_mapping().to_profile())
        builtin_profile = Profile(
            id="builtin", title="Builtin", data=_two_field_mapping().to_profile()
        )

        match = match_profile("Basic", LIVE_FIELDS, profiles=[user_profile, builtin_profile])
        self.assertEqual(match.quality, MatchQuality.EXACT)
        self.assertIs(match.profile, user_profile)

        match_reversed = match_profile(
            "Basic", LIVE_FIELDS, profiles=[builtin_profile, user_profile]
        )
        self.assertIs(match_reversed.profile, builtin_profile)


if __name__ == "__main__":
    unittest.main()
