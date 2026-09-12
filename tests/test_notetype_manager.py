"""Execution of a conversion plan, against a fake collection.

These cover the destructive paths -- repointing notes, duplicating notes, resetting
scheduling -- without needing a real Anki profile, so a regression shows up here rather
than in someone's collection.
"""

from __future__ import annotations

import unittest

from addon.core.conversion import ConversionMode, build_plan
from addon.core.role_schema import FieldBinding, Role, RoleMapping
from addon.ops.notetype_manager import ApiMismatch, apply_plan, build_preview_note

from tests.fake_collection import FakeCollection

FIELDS = ["Word", "Meaning", "Sound", "Bookkeeping"]


def make_collection():
    col = FakeCollection()
    src = col.add_notetype("Starter", FIELDS, css=".card { color: White; }")
    deck = col.add_deck("Starter")
    other_nt = col.add_notetype("Unrelated", ["A", "B"])
    other_deck = col.add_deck("Unrelated")
    for i in range(5):
        col.seed_note(src, deck, ["word%d" % i, "meaning%d" % i, "[sound:s%d.mp3]" % i, str(i)],
                      tags=["leech", "keepme"] if i == 0 else [])
    # Notes that must never be touched: same notetype elsewhere, and a different notetype.
    col.seed_note(src, other_deck, ["elsewhere", "x", "", ""])
    col.seed_note(other_nt, other_deck, ["a", "b"])
    return col, src, deck


def make_mapping():
    fields = [FieldBinding(n, i) for i, n in enumerate(FIELDS)]
    fields[3] = FieldBinding("Bookkeeping", 3, hidden=True)
    mapping = RoleMapping(notetype_name="Starter", fields=fields,
                          target_language="xx", native_language="yy")
    mapping.bind(Role.TARGET_TERM, fields[0])
    mapping.bind(Role.NATIVE_TERM, fields[1])
    mapping.bind(Role.TARGET_AUDIO, fields[2])
    return mapping


def make_plan(col, mode, **kw):
    plan = build_plan(
        mode=mode,
        mapping=make_mapping(),
        source_notetype="Starter",
        source_deck="Starter",
        suffix="Flipped",
        **kw,
    )
    plan.note_ids = col.find_notes(plan.scope_query)
    return plan


def run(col, plan, **kw):
    return apply_plan(col, plan, front="{{Word}}", back="{{FrontSide}}{{Meaning}}",
                      css=".card { color: White; }", **kw)


class TestScopeIsRespected(unittest.TestCase):
    def test_only_notes_in_both_the_deck_and_notetype_are_in_scope(self):
        col, _, _ = make_collection()
        plan = make_plan(col, ConversionMode.NEW_DECK)
        self.assertEqual(len(plan.note_ids), 5, "same notetype in another deck must be excluded")


class TestNewDeckMode(unittest.TestCase):
    def setUp(self):
        self.col, self.src, self.deck = make_collection()
        self.plan = make_plan(self.col, ConversionMode.NEW_DECK)
        self.result = run(self.col, self.plan)

    def test_originals_are_completely_untouched(self):
        for nid in self.plan.note_ids:
            self.assertEqual(self.col.notes[nid].mid, self.src["id"])
        self.assertEqual(self.col.models.by_name("Starter")["id"], self.src["id"])
        self.assertEqual(len(self.col.find_notes('note:"Starter" deck:"Starter"')), 5)

    def test_copies_are_created_on_the_clone(self):
        self.assertEqual(self.result.notes_converted, 5)
        clone_notes = [n for n in self.col.notes.values() if n.mid == self.result.clone_notetype_id]
        self.assertEqual(len(clone_notes), 5)

    def test_field_values_are_copied_by_name(self):
        clone_notes = [n for n in self.col.notes.values() if n.mid == self.result.clone_notetype_id]
        words = sorted(n["Word"] for n in clone_notes)
        self.assertEqual(words, ["word0", "word1", "word2", "word3", "word4"])

    def test_media_references_are_shared_not_duplicated(self):
        clone_notes = [n for n in self.col.notes.values() if n.mid == self.result.clone_notetype_id]
        self.assertTrue(any("[sound:s0.mp3]" == n["Sound"] for n in clone_notes))
        self.assertEqual(self.col.media.trashed, [], "new-deck mode must never trash media")

    def test_scheduling_derived_tags_are_stripped_but_others_kept(self):
        clone_notes = [n for n in self.col.notes.values() if n.mid == self.result.clone_notetype_id]
        tags = [t for n in clone_notes for t in n.tags]
        self.assertNotIn("leech", tags)
        self.assertIn("keepme", tags)

    def test_new_cards_are_reset(self):
        self.assertEqual(self.result.cards_reset, 5)
        self.assertTrue(self.col.reset_calls)
        self.assertTrue(self.col.reset_calls[0]["reset_counts"])

    def test_original_cards_keep_their_review_history(self):
        originals = [c for c in self.col.cards if c["mid"] == self.src["id"]]
        self.assertTrue(all(c["ivl"] == 30 for c in originals))


class TestFlipInPlaceMode(unittest.TestCase):
    def setUp(self):
        self.col, self.src, self.deck = make_collection()
        self.plan = make_plan(self.col, ConversionMode.FLIP_IN_PLACE)
        self.result = run(self.col, self.plan)

    def test_notes_move_onto_the_clone(self):
        for nid in self.plan.note_ids:
            self.assertEqual(self.col.notes[nid].mid, self.result.clone_notetype_id)

    def test_note_count_is_unchanged(self):
        self.assertEqual(len(self.col.notes), 7)  # 5 converted + 2 out of scope

    def test_out_of_scope_notes_are_not_moved(self):
        untouched = [n for n in self.col.notes.values() if n.mid == self.src["id"]]
        self.assertEqual(len(untouched), 1, "the same notetype in another deck must stay put")

    def test_the_change_request_carries_exactly_our_note_ids(self):
        request = self.col.change_notetype_calls[0]
        self.assertEqual(sorted(request.note_ids), sorted(self.plan.note_ids))

    def test_scheduling_is_reset(self):
        self.assertEqual(self.result.cards_reset, 5)
        converted = [c for c in self.col.cards if c["mid"] == self.result.clone_notetype_id]
        self.assertTrue(all(c["ivl"] == 0 and c["type"] == 0 for c in converted))


class TestNotetypeNaming(unittest.TestCase):
    """Regression: ``col.models.ensure_name_unique`` takes a notetype dict and mutates it
    in place; it does not take a plain string and return a new one. Calling it with a bare
    string crashes with ``TypeError: string indices must be integers, not 'str'`` -- this
    is the exact bug reported against a real Anki collection.
    """

    def test_clone_gets_a_fresh_name_when_none_collides(self):
        col, _, _ = make_collection()
        result = run(col, make_plan(col, ConversionMode.NEW_DECK))
        self.assertEqual(result.clone_notetype_name, "Starter (Flipped)")

    def test_clone_name_is_disambiguated_on_collision(self):
        col, src, _ = make_collection()
        col.add_notetype("Starter (Flipped)", FIELDS)  # pre-occupy the obvious name
        result = run(col, make_plan(col, ConversionMode.NEW_DECK))
        self.assertNotEqual(result.clone_notetype_name, "Starter (Flipped)")
        self.assertTrue(col.models.by_name(result.clone_notetype_name))

    def test_ensure_name_unique_is_never_called_with_a_bare_string(self):
        """Production code must route naming through ``by_name``, not this method --
        its real calling convention (dict in, mutate in place) is why the bug happened.
        """
        col, _, _ = make_collection()
        original = col.models.ensure_name_unique

        def guard(arg):
            self.assertIsInstance(
                arg, dict, "ensure_name_unique was called with %r, not a notetype dict" % (arg,)
            )
            return original(arg)

        col.models.ensure_name_unique = guard
        run(col, make_plan(col, ConversionMode.NEW_DECK))  # must not raise


class TestTheOriginalNotetypeIsNeverMutated(unittest.TestCase):
    def test_source_templates_and_css_survive(self):
        col, src, _ = make_collection()
        before_qfmt = src["tmpls"][0]["qfmt"]
        before_name = src["name"]
        run(col, make_plan(col, ConversionMode.FLIP_IN_PLACE))
        after = col.models.by_name(before_name)
        self.assertEqual(after["tmpls"][0]["qfmt"], before_qfmt)
        self.assertEqual(after["id"], src["id"])

    def test_clone_receives_the_generated_templates(self):
        col, _, _ = make_collection()
        result = run(col, make_plan(col, ConversionMode.NEW_DECK))
        clone = col.notetypes[result.clone_notetype_id]
        self.assertEqual(clone["tmpls"][0]["qfmt"], "{{Word}}")
        self.assertEqual(len(clone["tmpls"]), 1, "extra templates would make extra cards")


class TestBuildPreviewNote(unittest.TestCase):
    """A real note on a real notetype, so Anki's own renderer can show it -- without
    touching the source deck, the real clone, or any of the user's notes.
    """

    def _preview(self, col, plan, sample_nid, front="{{Word}}", back="{{FrontSide}}{{Meaning}}"):
        return build_preview_note(
            col, plan, front=front, back=back, css=".card{}", template_name="Production",
            sample_note_id=sample_nid,
        )

    def test_creates_a_dedicated_scratch_notetype_and_deck(self):
        col, src, _ = make_collection()
        plan = make_plan(col, ConversionMode.NEW_DECK)
        self._preview(col, plan, plan.note_ids[0])
        preview_name = "%s (Preview)" % plan.clone_notetype
        self.assertIsNotNone(col.models.by_name(preview_name))
        self.assertIsNotNone(col.decks.by_name(preview_name))

    def test_preview_name_never_collides_with_the_real_conversion(self):
        col, src, _ = make_collection()
        plan = make_plan(col, ConversionMode.NEW_DECK)
        self._preview(col, plan, plan.note_ids[0])
        result = run(col, plan)  # the real, non-preview conversion
        self.assertNotEqual(result.clone_notetype_name, "%s (Preview)" % plan.clone_notetype)

    def test_source_note_and_deck_are_untouched(self):
        col, src, deck = make_collection()
        plan = make_plan(col, ConversionMode.NEW_DECK)
        sample_nid = plan.note_ids[0]
        before = list(col.notes[sample_nid].fields), col.notes[sample_nid].mid
        self._preview(col, plan, sample_nid)
        after = list(col.notes[sample_nid].fields), col.notes[sample_nid].mid
        self.assertEqual(before, after)
        self.assertEqual(len(col.find_notes('note:"Starter" deck:"Starter"')), 5)

    def test_field_values_come_from_the_chosen_sample_note(self):
        col, src, _ = make_collection()
        plan = make_plan(col, ConversionMode.NEW_DECK)
        sample_nid = plan.note_ids[2]
        note = self._preview(col, plan, sample_nid)
        self.assertEqual(note["Word"], col.notes[sample_nid]["Word"])

    def test_repeated_previews_reuse_the_same_note_rather_than_accumulating(self):
        col, src, _ = make_collection()
        plan = make_plan(col, ConversionMode.NEW_DECK)
        first = self._preview(col, plan, plan.note_ids[0])
        second = self._preview(col, plan, plan.note_ids[1])
        preview_name = "%s (Preview)" % plan.clone_notetype
        self.assertEqual(len(col.find_notes('note:"%s"' % preview_name)), 1)
        self.assertEqual(first.id, second.id)
        self.assertEqual(second["Word"], col.notes[plan.note_ids[1]]["Word"])

    def test_repeated_previews_pick_up_updated_templates(self):
        col, src, _ = make_collection()
        plan = make_plan(col, ConversionMode.NEW_DECK)
        self._preview(col, plan, plan.note_ids[0], front="{{Word}}")
        self._preview(col, plan, plan.note_ids[0], front="{{Meaning}}")
        preview_name = "%s (Preview)" % plan.clone_notetype
        self.assertEqual(col.models.by_name(preview_name)["tmpls"][0]["qfmt"], "{{Meaning}}")

    def test_preview_never_deletes_anything(self):
        """Even repeated calls only add or update -- see build_preview_note's docstring."""
        col, src, _ = make_collection()
        plan = make_plan(col, ConversionMode.NEW_DECK)
        before_notetypes = set(col.notetypes)
        before_notes = set(col.notes)
        self._preview(col, plan, plan.note_ids[0])
        self._preview(col, plan, plan.note_ids[1])
        after_notetypes = set(col.notetypes)
        after_notes = set(col.notes)
        self.assertTrue(before_notetypes <= after_notetypes)
        self.assertTrue(before_notes <= after_notes)


class TestGuardrails(unittest.TestCase):
    def test_dry_run_writes_nothing(self):
        col, src, _ = make_collection()
        plan = make_plan(col, ConversionMode.FLIP_IN_PLACE)
        plan.dry_run = True
        before = len(col.notes)
        result = run(col, plan)
        self.assertTrue(result.dry_run)
        self.assertEqual(len(col.notes), before)
        self.assertEqual(col.change_notetype_calls, [])
        self.assertEqual(col.reset_calls, [])
        self.assertEqual(result.notes_converted, 5)

    def test_a_note_count_change_between_preflight_and_apply_aborts(self):
        col, src, deck = make_collection()
        plan = make_plan(col, ConversionMode.NEW_DECK)
        col.seed_note(src, deck, ["late", "arrival", "", ""])  # sneaks in after preflight
        with self.assertRaises(ApiMismatch):
            run(col, plan, expected_note_count=5)
        self.assertEqual(col.change_notetype_calls, [])

    def test_invalid_plan_is_refused_before_any_write(self):
        col, _, _ = make_collection()
        plan = make_plan(col, ConversionMode.NEW_DECK)
        plan.media_cleanup = True  # forbidden in new-deck mode
        before = len(col.notetypes)
        with self.assertRaises(Exception):
            run(col, plan)
        self.assertEqual(len(col.notetypes), before, "no clone may be created")


if __name__ == "__main__":
    unittest.main()
