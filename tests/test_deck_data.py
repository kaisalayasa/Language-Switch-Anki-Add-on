"""``ops.deck_data``: reading (deck, notetype) pairs and per-field samples out of a
collection, against ``tests/fake_collection.py`` -- no real Anki, no real profile.

``addon_config`` is not covered here -- it needs a real running Anki process
(``mw.addonManager``), which is exactly why it's the one function in this module still
importing ``aqt`` at all, and lazily at that.
"""

from __future__ import annotations

import unittest

from addon.ops.deck_data import collect_field_samples, collect_raw_samples, decks_with_notetypes
from tests.fake_collection import FakeCollection


class TestDecksWithNotetypes(unittest.TestCase):
    def test_only_pairs_with_actual_notes_are_returned(self):
        col = FakeCollection()
        nt = col.add_notetype("Basic", ["Front", "Back"])
        deck = col.add_deck("Spanish")
        empty_deck = col.add_deck("Empty")
        col.seed_note(nt, deck, ["hola", "hello"])

        pairs = decks_with_notetypes(col)

        self.assertIn(("Spanish", "Basic", 1), pairs)
        self.assertNotIn(("Empty", "Basic", 0), pairs)
        self.assertFalse(any(p[0] == "Empty" for p in pairs))

    def test_a_notetype_shared_across_two_decks_produces_two_pairs(self):
        col = FakeCollection()
        nt = col.add_notetype("Basic", ["Front", "Back"])
        deck_a = col.add_deck("A")
        deck_b = col.add_deck("B")
        col.seed_note(nt, deck_a, ["1", "2"])
        col.seed_note(nt, deck_b, ["3", "4"])

        pairs = decks_with_notetypes(col)

        self.assertIn(("A", "Basic", 1), pairs)
        self.assertIn(("B", "Basic", 1), pairs)

    def test_sorted_by_note_count_descending_then_deck_name(self):
        col = FakeCollection()
        nt = col.add_notetype("Basic", ["Front", "Back"])
        big = col.add_deck("Big")
        small = col.add_deck("Small")
        for i in range(3):
            col.seed_note(nt, big, ["%d" % i, "%d" % i])
        col.seed_note(nt, small, ["0", "0"])

        pairs = decks_with_notetypes(col)

        self.assertEqual(pairs[0], ("Big", "Basic", 3))
        self.assertEqual(pairs[1], ("Small", "Basic", 1))


class TestCollectRawSamples(unittest.TestCase):
    def test_samples_are_kept_verbatim_including_markup(self):
        col = FakeCollection()
        nt = col.add_notetype("Basic", ["Word", "Audio"])
        deck = col.add_deck("D")
        note = col.seed_note(nt, deck, ["<b>hola</b>", "[sound:x.mp3]"])

        samples = collect_raw_samples(col, [note.id])

        self.assertEqual(samples["Word"], ["<b>hola</b>"])
        self.assertEqual(samples["Audio"], ["[sound:x.mp3]"])

    def test_blank_values_are_kept_not_skipped(self):
        """The bucket's length must reflect notes actually scanned -- callers (llm.direction's
        language weighting) depend on that, not just on non-empty content."""
        col = FakeCollection()
        nt = col.add_notetype("Basic", ["Word", "Sentence"])
        deck = col.add_deck("D")
        note = col.seed_note(nt, deck, ["hola", ""])

        samples = collect_raw_samples(col, [note.id])

        self.assertEqual(samples["Sentence"], [""])

    def test_respects_the_per_field_limit(self):
        col = FakeCollection()
        nt = col.add_notetype("Basic", ["Word"])
        deck = col.add_deck("D")
        note_ids = [col.seed_note(nt, deck, ["w%d" % i]).id for i in range(5)]

        samples = collect_raw_samples(col, note_ids, limit_per_field=2)

        self.assertEqual(len(samples["Word"]), 2)


class TestCollectFieldSamples(unittest.TestCase):
    def test_returns_one_field_sample_per_field_in_notetype_order(self):
        col = FakeCollection()
        nt = col.add_notetype("Basic", ["Word", "Translation"])
        deck = col.add_deck("D")
        col.seed_note(nt, deck, ["hola", "hello"])
        col.seed_note(nt, deck, ["adios", "goodbye"])
        note_ids = col.find_notes('note:"Basic"')

        fields = collect_field_samples(col, note_ids)

        self.assertEqual([f.name for f in fields], ["Word", "Translation"])
        word = next(f for f in fields if f.name == "Word")
        self.assertEqual(sorted(word.samples), ["adios", "hola"])

    def test_result_is_directly_usable_as_llm_prompt_fieldsample_objects(self):
        from addon.llm.prompt import FieldSample

        col = FakeCollection()
        nt = col.add_notetype("Basic", ["Word"])
        deck = col.add_deck("D")
        col.seed_note(nt, deck, ["hola"])

        fields = collect_field_samples(col, col.find_notes('note:"Basic"'))

        self.assertIsInstance(fields[0], FieldSample)


if __name__ == "__main__":
    unittest.main()
