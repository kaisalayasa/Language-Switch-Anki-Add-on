"""Execution of a conversion plan, against a fake collection.

These cover the destructive paths -- repointing notes, duplicating notes, resetting
scheduling -- without needing a real Anki profile, so a regression shows up here rather
than in someone's collection.
"""

from __future__ import annotations

import unittest

from addon.core.audio_fields import AUDIO_DONE_TAG
from addon.core.conversion import ConversionMode, build_plan
from addon.ops.notetype_manager import ApiMismatch, apply_plan

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


def make_plan(col, mode, **kw):
    kw.setdefault("front", "{{Word}}")
    kw.setdefault("back", "{{FrontSide}}{{Meaning}}")
    kw.setdefault("css", ".card { color: White; }")
    kw.setdefault("target_language", "xx")
    kw.setdefault("native_language", "yy")
    plan = build_plan(
        mode=mode,
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


class _FlakyOnceCollection(FakeCollection):
    """Simulates a real-world report: the *first* ``merge_undo_entries`` call still raises
    "target undo op not found" even though ``apply_plan``'s marker already sits after every
    schema-level call it knows about (see docs/api-notes.md) -- exercising the retry
    recovery in ``apply_plan`` rather than the normal, already-covered success path."""

    def __init__(self) -> None:
        super().__init__()
        self.merge_attempts = 0

    def merge_undo_entries(self, target):
        self.merge_attempts += 1
        if self.merge_attempts == 1:
            self._pending_undo_tokens.pop(target, None)
            raise RuntimeError("target undo op not found")
        return super().merge_undo_entries(target)


class TestUndoMergeRecovery(unittest.TestCase):
    def test_a_flaky_first_merge_does_not_fail_the_conversion(self):
        col = _FlakyOnceCollection()
        src = col.add_notetype("Starter", FIELDS, css=".card { color: White; }")
        deck = col.add_deck("Starter")
        col.seed_note(src, deck, ["word0", "meaning0", "[sound:s0.mp3]", "0"])
        plan = make_plan(col, ConversionMode.NEW_DECK)

        result = run(col, plan)

        self.assertEqual(result.notes_converted, 1)
        self.assertEqual(col.merge_attempts, 2, "must have retried exactly once, not raised")
        self.assertTrue(
            any("Done:" in m for m in result.messages),
            "a flaky first merge must still end in a normal success message: %r" % result.messages,
        )
        self.assertFalse(
            any("undo" in m.lower() for m in result.messages),
            "the retry is an internal recovery, not something to surface to the user: %r"
            % result.messages,
        )


class TestGeneratedAudioFieldsAreCreatedOnTheClone(unittest.TestCase):
    """A conversion adds the field its generated audio will live in.

    The alternative -- reusing the deck's existing audio field -- is what made a converted
    Korean deck keep playing Korean: that field already held the old language's audio, so
    the card played it for as long as synthesis hadn't overwritten it, which for a failed or
    partial run is forever. See addon/core/audio_fields.py.
    """

    def _converted(self, mode, new_fields=("ddc-audio (EN)",)):
        col, src, _deck = make_collection()
        plan = make_plan(col, mode)
        plan.new_fields = list(new_fields)
        result = run(col, plan)
        return col, src, col.models.by_name(result.clone_notetype_name), result

    def test_the_clone_gains_the_field_and_the_original_does_not(self):
        col, src, clone, _result = self._converted(ConversionMode.NEW_DECK)
        self.assertIn("ddc-audio (EN)", [f["name"] for f in clone["flds"]])
        self.assertNotIn("ddc-audio (EN)", [f["name"] for f in src["flds"]])

    def test_it_is_appended_after_the_originals_which_all_keep_their_ord(self):
        """Ords matter: Flip-in-place hands Anki a field map built from them. Inserting the
        new field among the existing ones would shift every later field by one."""
        _col, src, clone, _result = self._converted(ConversionMode.NEW_DECK)
        for original in src["flds"]:
            match = [f for f in clone["flds"] if f["name"] == original["name"]]
            self.assertEqual(len(match), 1, original["name"])
            self.assertEqual(match[0]["ord"], original["ord"], original["name"])
        self.assertEqual(clone["flds"][-1]["name"], "ddc-audio (EN)")
        self.assertEqual(clone["flds"][-1]["ord"], len(src["flds"]))

    def test_two_audio_fields_can_be_added_at_once(self):
        _col, _src, clone, _r = self._converted(
            ConversionMode.NEW_DECK, new_fields=("ddc-audio (EN)", "ddc-audio-sentence (EN)")
        )
        names = [f["name"] for f in clone["flds"]]
        self.assertIn("ddc-audio (EN)", names)
        self.assertIn("ddc-audio-sentence (EN)", names)
        self.assertEqual(len(set(names)), len(names), "no duplicate field names")

    def test_the_new_field_starts_empty_on_every_duplicated_note(self):
        """The whole point. An empty field renders as nothing, so a card can be silent but
        never wrong, however the TTS run goes."""
        col, _src, _clone, result = self._converted(ConversionMode.NEW_DECK)
        copies = [n for n in col.notes.values() if n.mid == result.clone_notetype_id]
        self.assertEqual(len(copies), 5)
        for note in copies:
            self.assertEqual(note["ddc-audio (EN)"], "")
            self.assertEqual(note["Sound"], note["Sound"])  # original audio carried over
            self.assertTrue(note["Sound"].startswith("[sound:"))

    def test_flip_in_place_also_gets_the_field(self):
        col, _src, clone, result = self._converted(ConversionMode.FLIP_IN_PLACE)
        self.assertIn("ddc-audio (EN)", [f["name"] for f in clone["flds"]])

        moved = [n for n in col.notes.values() if n.mid == result.clone_notetype_id]
        self.assertEqual(len(moved), 5)
        for note in moved:
            self.assertEqual(note["ddc-audio (EN)"], "", "the new field must arrive empty")
            self.assertTrue(note["Word"].startswith("word"), "originals must survive intact")
            self.assertTrue(note["Sound"].startswith("[sound:"))

    def test_a_conversion_with_no_new_fields_is_unchanged(self):
        col, src, _deck = make_collection()
        plan = make_plan(col, ConversionMode.NEW_DECK)
        result = run(col, plan)
        clone = col.models.by_name(result.clone_notetype_name)
        self.assertEqual(
            [f["name"] for f in clone["flds"]], [f["name"] for f in src["flds"]]
        )

    def test_adding_a_field_that_is_already_there_does_nothing(self):
        """Converting an already-converted deck must not accumulate duplicates."""
        col, _src, _deck = make_collection()
        plan = make_plan(col, ConversionMode.NEW_DECK)
        plan.new_fields = ["Sound"]  # already on the notetype
        result = run(col, plan)
        names = [f["name"] for f in col.models.by_name(result.clone_notetype_name)["flds"]]
        self.assertEqual(names.count("Sound"), 1)

    def test_the_generated_field_dict_is_not_a_copy_of_another_fields_identity(self):
        _col, src, clone, _r = self._converted(ConversionMode.NEW_DECK)
        added = [f for f in clone["flds"] if f["name"] == "ddc-audio (EN)"][0]
        for donor in src["flds"]:
            if "id" in donor:
                self.assertNotEqual(added.get("id"), donor["id"])


class TestStaleDoneTagIsNeverCarriedOntoADuplicate(unittest.TestCase):
    def test_the_audio_done_tag_is_stripped_even_though_it_is_not_configured(self):
        """The tag means "this note's generated audio is current". A fresh duplicate's
        generated audio field is empty, so carrying the tag over would make the TTS batch
        skip the note as already done and leave it permanently silent."""
        col, src, deck = make_collection()
        col.seed_note(src, deck, ["w", "m", "[sound:x.mp3]", "9"],
                      tags=[AUDIO_DONE_TAG, "keepme"])
        plan = make_plan(col, ConversionMode.NEW_DECK)
        plan.strip_tags = ["leech"]  # the shipped default; the done tag is not in it

        result = run(col, plan)

        copies = [n for n in col.notes.values() if n.mid == result.clone_notetype_id]
        self.assertTrue(copies)
        for note in copies:
            self.assertNotIn(AUDIO_DONE_TAG, note.tags)
        self.assertTrue(
            any("keepme" in n.tags for n in copies), "unrelated tags must still carry over"
        )


class TestConversionTagIsWritten(unittest.TestCase):
    """core.deck_state's note-tag mark, written at conversion time so reopening the deck
    later reads direction back instead of re-deriving it from template structure."""

    def test_new_deck_mode_tags_every_duplicated_note(self):
        col, _src, _deck = make_collection()
        plan = make_plan(col, ConversionMode.NEW_DECK, target_language="es", native_language="en")
        result = run(col, plan)
        clone_notes = [n for n in col.notes.values() if n.mid == result.clone_notetype_id]
        self.assertEqual(len(clone_notes), 5)
        for note in clone_notes:
            self.assertIn("ddc-converted::es::en", note.tags)

    def test_flip_in_place_mode_tags_every_moved_note(self):
        col, _src, _deck = make_collection()
        plan = make_plan(
            col, ConversionMode.FLIP_IN_PLACE, target_language="ja", native_language="en"
        )
        run(col, plan)
        for nid in plan.note_ids:
            self.assertIn("ddc-converted::ja::en", col.notes[nid].tags)

    def test_dry_run_tags_nothing(self):
        col, _src, _deck = make_collection()
        plan = make_plan(col, ConversionMode.NEW_DECK)
        plan.dry_run = True
        run(col, plan)
        for note in col.notes.values():
            self.assertFalse(any(t.startswith("ddc-converted::") for t in note.tags))


if __name__ == "__main__":
    unittest.main()
