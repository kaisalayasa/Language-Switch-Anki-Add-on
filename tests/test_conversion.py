"""Conversion planning: scoping, mode safety rules, and the preflight summary.

A plan carries plain front/back/css strings (what the LLM produced) rather than a role mapping
-- see ``addon/core/conversion.py``'s module docstring.
"""

from __future__ import annotations

import unittest

from addon.core.conversion import (
    ConversionMode,
    build_plan,
    escape_search_term,
    scope_query,
)

# An arbitrary but fixed note count, standing in for "however many notes a real deck has".
_SAMPLE_NOTE_COUNT = 500


def plan(mode=ConversionMode.NEW_DECK, **kw):
    kw.setdefault("front", "<div>{{Word}}</div>")
    kw.setdefault("back", "{{FrontSide}}<hr id=answer><div>{{Translation}}</div>")
    kw.setdefault("css", ".card { font-size: 20px; }")
    kw.setdefault("target_language", "es")
    kw.setdefault("native_language", "en")
    kw.setdefault("source_notetype", "Generic")
    kw.setdefault("source_deck", "Generic")
    kw.setdefault("note_ids", list(range(_SAMPLE_NOTE_COUNT)))
    return build_plan(mode=mode, **kw)


class TestScoping(unittest.TestCase):
    """Getting this wrong is the one mistake with collection-wide blast radius."""

    def test_scope_is_narrowed_by_both_notetype_and_deck(self):
        q = scope_query("Generic", "Generic")
        self.assertIn('note:"Generic"', q)
        self.assertIn('deck:"Generic"', q)

    def test_search_metacharacters_are_escaped(self):
        for raw, must_contain in [
            ('My "Deck"', '\\"'),
            ("Deck*", "\\*"),
            ("a_b", "\\_"),
            ("a:b", "\\:"),
            ("a-b", "\\-"),
            ("a(b)", "\\("),
        ]:
            with self.subTest(raw=raw):
                self.assertIn(must_contain, escape_search_term(raw))

    def test_wildcard_cannot_widen_the_scope(self):
        """An unescaped * would match every deck starting with that prefix."""
        self.assertNotIn('deck:"Core*"', scope_query("N", "Core*"))


class TestModeSafetyRules(unittest.TestCase):
    def test_new_deck_mode_refuses_to_write_into_the_source_deck(self):
        p = plan(ConversionMode.NEW_DECK, target_deck="Generic")
        self.assertFalse(p.validate().ok)

    def test_new_deck_mode_refuses_media_cleanup(self):
        """The original notes still reference those files."""
        p = plan(ConversionMode.NEW_DECK)
        p.media_cleanup = True
        result = p.validate()
        self.assertFalse(result.ok)
        self.assertTrue(any("never run in new-deck mode" in e for e in result.errors))

    def test_clone_may_not_reuse_the_source_notetype_name(self):
        p = plan(clone_notetype="Generic")
        result = p.validate()
        self.assertFalse(result.ok)
        self.assertTrue(any("never modified" in e for e in result.errors))

    def test_scheduling_reset_cannot_be_switched_off(self):
        p = plan()
        p.reset_scheduling = False
        result = p.validate()
        self.assertFalse(result.ok)
        self.assertTrue(any("not optional" in e for e in result.errors))

    def test_an_empty_front_template_is_rejected(self):
        p = plan(front="")
        result = p.validate()
        self.assertFalse(result.ok)
        self.assertTrue(any("front template is empty" in e for e in result.errors))

    def test_an_empty_back_template_is_rejected(self):
        p = plan(back="")
        result = p.validate()
        self.assertFalse(result.ok)
        self.assertTrue(any("back template is empty" in e for e in result.errors))

    def test_a_valid_new_deck_plan_passes(self):
        self.assertTrue(plan(ConversionMode.NEW_DECK).validate().ok)

    def test_a_valid_flip_plan_passes(self):
        self.assertTrue(plan(ConversionMode.FLIP_IN_PLACE).validate().ok)

    def test_only_flip_is_marked_destructive(self):
        self.assertTrue(ConversionMode.FLIP_IN_PLACE.is_destructive)
        self.assertFalse(ConversionMode.NEW_DECK.is_destructive)


class TestDefaultNaming(unittest.TestCase):
    def test_new_deck_gets_its_own_deck_by_default(self):
        p = plan(ConversionMode.NEW_DECK)
        self.assertNotEqual(p.target_deck, p.source_deck)
        self.assertTrue(p.validate().ok)

    def test_flip_stays_in_the_source_deck(self):
        p = plan(ConversionMode.FLIP_IN_PLACE)
        self.assertEqual(p.target_deck, p.source_deck)

    def test_suffix_is_caller_supplied_not_baked_in(self):
        p = plan(suffix="Produccion")
        self.assertIn("Produccion", p.clone_notetype)


class TestPlanRecordsTheConversionDirection(unittest.TestCase):
    """target_language/native_language ride along on the plan so convert_op.py can write
    core.deck_state's marks without needing anything beyond the plan itself."""

    def test_languages_are_carried_on_the_plan(self):
        p = plan(target_language="ja", native_language="en")
        self.assertEqual(p.target_language, "ja")
        self.assertEqual(p.native_language, "en")


class TestPreflight(unittest.TestCase):
    def test_summary_states_the_note_count_and_reset(self):
        text = plan(ConversionMode.NEW_DECK).preflight().as_text()
        self.assertIn(str(_SAMPLE_NOTE_COUNT), text)
        self.assertIn("WILL BE RESET", text)
        self.assertIn("untouched", text)

    def test_flip_summary_says_cards_are_replaced(self):
        text = plan(ConversionMode.FLIP_IN_PLACE).preflight().as_text()
        self.assertIn("replaced", text)

    def test_summary_warns_when_nothing_matched(self):
        p = plan(note_ids=[])
        self.assertTrue(any("no notes matched" in w for w in p.preflight().warnings))

    def test_media_cleanup_is_reported_as_off_by_default(self):
        self.assertIn("nothing deleted", plan().preflight().as_text())


if __name__ == "__main__":
    unittest.main()
