"""``run_conversion``: the real Apply flow, undo-entry placement, its interaction with Anki's
schema-invalidation rule, and writing the ``core.deck_state`` css marker.

Before this file existed, only the inner ``apply_plan`` was tested directly -- which bypasses
the undo-entry wrapping entirely. That gap is exactly how a "target undo op not found" bug sat
latent in both real conversion modes (not just the Preview feature, where it was first
noticed) until these tests were written. See ``docs/api-notes.md``.
"""

from __future__ import annotations

import unittest

from addon.core.conversion import ConversionMode
from addon.core.deck_state import state_from_css
from addon.ops.convert_op import run_conversion

from tests.fake_collection import FakeCollection
from tests.test_notetype_manager import make_collection, make_plan


class TestRunConversionDoesNotHitTheUndoTrap(unittest.TestCase):
    """The actual regression: this used to raise ``RuntimeError: target undo op not
    found`` on every real (non-dry) run, in both modes, because the undo marker was set
    before ``apply_plan``'s schema-changing ``build_clone`` call.
    """

    def test_new_deck_mode_does_not_raise(self):
        col, _src, _ = make_collection()
        plan = make_plan(col, ConversionMode.NEW_DECK)
        result, _changes = run_conversion(col, plan)
        self.assertEqual(result.notes_converted, 5)

    def test_flip_in_place_mode_does_not_raise(self):
        col, _src, _ = make_collection()
        plan = make_plan(col, ConversionMode.FLIP_IN_PLACE)
        result, _changes = run_conversion(col, plan)
        self.assertEqual(result.notes_converted, 5)

    def test_dry_run_still_writes_nothing(self):
        col, _src, _ = make_collection()
        plan = make_plan(col, ConversionMode.NEW_DECK)
        plan.dry_run = True
        before = len(col.notetypes)
        result, changes = run_conversion(col, plan)
        self.assertTrue(result.dry_run)
        self.assertIsNone(changes)
        self.assertEqual(len(col.notetypes), before)


class TestUndoEntryNeverSpansASchemaChange(unittest.TestCase):
    """Locks in the fix's actual mechanism, using the same schema-generation simulation
    that reproduces Anki's real behaviour (see ``FakeCollection._bump_schema_generation``).
    """

    def test_merging_across_a_notetype_write_fails_like_real_anki(self):
        """Proves the FakeCollection simulation reproduces the bug at all -- if this test
        ever stops failing, the simulation has drifted from the real backend behaviour it
        exists to model, and the other tests here would no longer mean anything.
        """
        col = FakeCollection()
        nt = col.add_notetype("Demo", ["A", "B"])
        token = col.add_custom_undo_entry("span across a schema change")
        col.models.update_dict(dict(nt))
        with self.assertRaisesRegex(RuntimeError, "target undo op not found"):
            col.merge_undo_entries(token)

    def test_apply_plan_places_the_marker_after_build_clone(self):
        """The specific ordering fix: for new-deck mode, the marker must be set after
        ``build_clone`` (schema change) and before ``_new_deck`` (pure data).
        """
        col, _src, _ = make_collection()
        plan = make_plan(col, ConversionMode.NEW_DECK)
        result, _ = run_conversion(col, plan)
        self.assertIsNotNone(result.clone_notetype_id)

    def test_apply_plan_places_the_marker_after_change_notetype_of_notes(self):
        """For flip-in-place mode, ``change_notetype_of_notes`` is treated as schema-level
        too (see FakeModels.change_notetype_of_notes), so the marker must be set after it,
        not merely after ``build_clone``.
        """
        col, _src, _ = make_collection()
        plan = make_plan(col, ConversionMode.FLIP_IN_PLACE)
        result, _ = run_conversion(col, plan)
        for nid in plan.note_ids:
            self.assertEqual(col.notes[nid].mid, result.clone_notetype_id)


class TestOpChangesFlowsThroughForCollectionOp(unittest.TestCase):
    """``run_conversion``'s second return value is what a real ``CollectionOp`` needs to
    refresh Anki's UI; make sure it's actually populated, not silently dropped.
    """

    def test_op_changes_is_populated_on_a_real_run(self):
        col, _src, _ = make_collection()
        plan = make_plan(col, ConversionMode.NEW_DECK)
        result, changes = run_conversion(col, plan)
        self.assertIs(changes, result.op_changes)

    def test_op_changes_is_none_for_a_dry_run(self):
        col, _src, _ = make_collection()
        plan = make_plan(col, ConversionMode.NEW_DECK)
        plan.dry_run = True
        result, changes = run_conversion(col, plan)
        self.assertIsNone(changes)


class TestConversionCssMarkerIsWritten(unittest.TestCase):
    """core.deck_state's css mark -- the other half of the recorded-direction pair, alongside
    the note tag notetype_manager.py writes directly. Only run_conversion (not the lower-level
    apply_plan) applies it, since it needs the plan's target/native language."""

    def test_the_clones_css_carries_the_marker(self):
        col, _src, _ = make_collection()
        plan = make_plan(col, ConversionMode.NEW_DECK, target_language="es", native_language="en")
        result, _changes = run_conversion(col, plan)
        clone = col.models.by_name(result.clone_notetype_name)
        state = state_from_css(clone["css"])
        self.assertIsNotNone(state)
        self.assertEqual(state.target_language, "es")
        self.assertEqual(state.native_language, "en")

    def test_the_original_css_content_is_preserved_alongside_the_marker(self):
        col, _src, _ = make_collection()
        plan = make_plan(col, ConversionMode.NEW_DECK, css=".card { color: White; }")
        result, _changes = run_conversion(col, plan)
        clone = col.models.by_name(result.clone_notetype_name)
        self.assertIn(".card { color: White; }", clone["css"])

    def test_a_dry_run_writes_no_marker_anywhere(self):
        col, _src, _ = make_collection()
        plan = make_plan(col, ConversionMode.NEW_DECK)
        plan.dry_run = True
        run_conversion(col, plan)
        for nt in col.notetypes.values():
            self.assertIsNone(state_from_css(nt.get("css", "")))


if __name__ == "__main__":
    unittest.main()
