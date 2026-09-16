"""Batch TTS generation, against ``FakeCollection`` and an injected fake provider -- no real
Anki, no real Piper.

There is exactly one audio field and one source field per notetype now -- the model decides
both once, at conversion time (``DeckAnalysis.audio_field_name``/``speak_text_from``), so
there is no per-role loop here any more the way the old ``AUDIO_SOURCE_ROLES`` design had.
There is also no ``finish_audio_batch`` step: the model already writes a bare reference to the
audio field into the front template it produces, and that field starts empty, so there is
nothing left to "turn on" once a batch finishes.
"""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from addon.ops.tts_batch import (
    AUDIO_DONE_TAG,
    apply_note_audio,
    generate_note_audio,
    notes_needing_audio,
    plan_note_audio,
)
from addon.tts.piper_provider import EmptyTextError

from tests.fake_collection import FakeCollection

FIELDS = ["Word", "Meaning", "Audio"]
AUDIO_FIELD = "Audio"
SOURCE_FIELD = "Word"
VOICE_ID = "en_US-lessac-medium"


class FakeProvider:
    """Stands in for ``PiperProvider``: same contract, no subprocess, no network."""

    def __init__(self, *, fail_for=()):
        self.calls = []
        self.fail_for = set(fail_for)

    def synthesize(self, text, *, voice_id, out_path=None):
        self.calls.append(text)
        if not text.strip():
            raise EmptyTextError("nothing to say")
        if text in self.fail_for:
            raise RuntimeError("synthesis exploded")
        path = Path(tempfile.mktemp(suffix=".wav"))
        path.write_bytes(b"RIFF....WAVEfake")
        return path


def make_collection():
    col = FakeCollection()
    nt = col.add_notetype("Clone", FIELDS)
    deck = col.add_deck("Clone")
    return col, nt, deck


class TestGenerateNoteAudio(unittest.TestCase):
    def test_writes_the_audio_field_and_tags_the_note(self):
        col, nt, deck = make_collection()
        note = col.seed_note(nt, deck, ["hello", "bonjour", ""])
        provider = FakeProvider()

        result = generate_note_audio(
            col, note, provider, VOICE_ID, audio_field=AUDIO_FIELD, source_field=SOURCE_FIELD
        )

        self.assertTrue(result.ok)
        self.assertEqual(result.fields_written, ["Audio"])
        self.assertTrue(note["Audio"].startswith("[sound:"))
        self.assertIn(AUDIO_DONE_TAG, note.tags)
        self.assertEqual(provider.calls, ["hello"])

    def test_empty_source_field_is_skipped_not_an_error(self):
        col, nt, deck = make_collection()
        note = col.seed_note(nt, deck, ["", "bonjour", ""])
        provider = FakeProvider()

        result = generate_note_audio(
            col, note, provider, VOICE_ID, audio_field=AUDIO_FIELD, source_field=SOURCE_FIELD
        )

        self.assertTrue(result.ok)
        self.assertEqual(result.fields_written, [])
        self.assertEqual(result.skipped, ["Audio"])
        self.assertEqual(note["Audio"], "")
        self.assertNotIn(AUDIO_DONE_TAG, note.tags)

    def test_synthesis_failure_leaves_note_untagged_and_unwritten(self):
        col, nt, deck = make_collection()
        note = col.seed_note(nt, deck, ["hello", "bonjour", ""])
        provider = FakeProvider(fail_for=["hello"])

        result = generate_note_audio(
            col, note, provider, VOICE_ID, audio_field=AUDIO_FIELD, source_field=SOURCE_FIELD
        )

        self.assertFalse(result.ok)
        self.assertIn("synthesis exploded", result.error)
        self.assertNotIn(AUDIO_DONE_TAG, note.tags)
        self.assertEqual(note["Audio"], "", "must not write a partial/wrong result")

    def test_a_source_field_that_does_not_exist_on_the_note_is_left_alone(self):
        col, nt, deck = make_collection()
        note = col.seed_note(nt, deck, ["hello", "bonjour", ""])

        result = generate_note_audio(
            col, note, FakeProvider(), VOICE_ID, audio_field=AUDIO_FIELD, source_field="Nonexistent"
        )

        self.assertTrue(result.ok)
        self.assertEqual(result.fields_written, [])
        self.assertFalse(result.changed)
        self.assertNotIn(AUDIO_DONE_TAG, note.tags)


def _synth_all(pending, provider, voice_id):
    """Stands in for ``ops.tts_runner._synth_one`` -- the bit of the parallel path that
    would otherwise run on a worker thread -- so these tests can exercise the plan/apply
    split without spinning up real threads."""
    synthesized = []
    for item in pending:
        try:
            wav_path = provider.synthesize(item.text, voice_id=voice_id)
        except EmptyTextError:
            continue
        except Exception as exc:  # noqa: BLE001 -- mirrors the real runner's handling
            return None, "%s: %s" % (type(exc).__name__, exc)
        synthesized.append((item.audio_field, wav_path))
    return synthesized, None


class TestPlanAndApplyNoteAudio(unittest.TestCase):
    """The split-out read/write halves that let a batch runner parallelize synthesis
    (only ``provider.synthesize`` -- pure, no ``col``) while keeping every ``col``/``note``
    call on one thread. Composing them must reproduce ``generate_note_audio`` exactly."""

    def test_plan_lists_the_source_text_for_the_audio_field(self):
        col, nt, deck = make_collection()
        note = col.seed_note(nt, deck, ["hello", "bonjour", ""])

        pending = plan_note_audio(note, audio_field=AUDIO_FIELD, source_field=SOURCE_FIELD)

        self.assertEqual([(p.audio_field, p.text) for p in pending], [("Audio", "hello")])

    def test_plan_includes_empty_source_text_unfiltered(self):
        """Skipping empty text is the provider's job (``EmptyTextError``), not the plan's --
        the plan is purely descriptive of what's bound."""
        col, nt, deck = make_collection()
        note = col.seed_note(nt, deck, ["", "bonjour", ""])

        pending = plan_note_audio(note, audio_field=AUDIO_FIELD, source_field=SOURCE_FIELD)

        self.assertEqual([(p.audio_field, p.text) for p in pending], [("Audio", "")])

    def test_apply_writes_the_field_and_tags_like_generate_note_audio(self):
        col, nt, deck = make_collection()
        note = col.seed_note(nt, deck, ["hello", "bonjour", ""])

        result = apply_note_audio(col, note, [("Audio", Path("/tmp/a.wav"))])

        self.assertTrue(result.ok)
        self.assertEqual(result.fields_written, ["Audio"])
        self.assertTrue(note["Audio"].startswith("[sound:"))
        self.assertIn(AUDIO_DONE_TAG, note.tags)

    def test_apply_with_error_writes_nothing_and_leaves_note_untagged(self):
        col, nt, deck = make_collection()
        note = col.seed_note(nt, deck, ["hello", "bonjour", ""])

        result = apply_note_audio(col, note, [], error="RuntimeError: synthesis exploded")

        self.assertFalse(result.ok)
        self.assertEqual(note["Audio"], "")
        self.assertNotIn(AUDIO_DONE_TAG, note.tags)

    def test_plan_then_apply_matches_generate_note_audio_on_the_same_note(self):
        col, nt, deck = make_collection()
        a = col.seed_note(nt, deck, ["hello", "bonjour", ""])
        b = col.seed_note(nt, deck, ["hello", "bonjour", ""])

        sequential = generate_note_audio(
            col, a, FakeProvider(), VOICE_ID, audio_field=AUDIO_FIELD, source_field=SOURCE_FIELD
        )

        pending = plan_note_audio(b, audio_field=AUDIO_FIELD, source_field=SOURCE_FIELD)
        synthesized, error = _synth_all(pending, FakeProvider(), VOICE_ID)
        split = apply_note_audio(col, b, synthesized, error=error)

        self.assertEqual(sequential.ok, split.ok)
        self.assertEqual(sorted(sequential.fields_written), sorted(split.fields_written))
        self.assertEqual(AUDIO_DONE_TAG in a.tags, AUDIO_DONE_TAG in b.tags)

    def test_plan_then_apply_matches_generate_note_audio_on_hard_failure(self):
        col, nt, deck = make_collection()
        a = col.seed_note(nt, deck, ["hello", "bonjour", ""])
        b = col.seed_note(nt, deck, ["hello", "bonjour", ""])

        sequential = generate_note_audio(
            col, a, FakeProvider(fail_for=["hello"]), VOICE_ID,
            audio_field=AUDIO_FIELD, source_field=SOURCE_FIELD,
        )

        pending = plan_note_audio(b, audio_field=AUDIO_FIELD, source_field=SOURCE_FIELD)
        synthesized, error = _synth_all(pending, FakeProvider(fail_for=["hello"]), VOICE_ID)
        split = apply_note_audio(col, b, synthesized or [], error=error)

        self.assertFalse(sequential.ok)
        self.assertFalse(split.ok)
        self.assertEqual(a["Audio"], b["Audio"], "both must leave the field untouched")
        self.assertEqual(AUDIO_DONE_TAG in a.tags, AUDIO_DONE_TAG in b.tags)


class TestNotesNeedingAudio(unittest.TestCase):
    def test_second_run_skips_already_tagged_notes(self):
        col, nt, deck = make_collection()
        a = col.seed_note(nt, deck, ["hello", "bonjour", ""])
        b = col.seed_note(nt, deck, ["goodbye", "au revoir", ""])
        provider = FakeProvider()

        pending = notes_needing_audio(col, "Clone")
        self.assertEqual(sorted(pending), sorted([a.id, b.id]))

        generate_note_audio(
            col, a, provider, VOICE_ID, audio_field=AUDIO_FIELD, source_field=SOURCE_FIELD
        )

        pending_after = notes_needing_audio(col, "Clone")
        self.assertEqual(pending_after, [b.id])

    def test_force_reprocesses_already_tagged_notes(self):
        col, nt, deck = make_collection()
        note = col.seed_note(nt, deck, ["hello", "bonjour", ""])
        generate_note_audio(
            col, note, FakeProvider(), VOICE_ID, audio_field=AUDIO_FIELD, source_field=SOURCE_FIELD
        )

        self.assertEqual(notes_needing_audio(col, "Clone"), [])
        self.assertEqual(notes_needing_audio(col, "Clone", force=True), [note.id])

    def test_scoped_by_notetype_only_not_deck(self):
        col, nt, deck = make_collection()
        other_deck = col.add_deck("Elsewhere")
        col.seed_note(nt, deck, ["a", "b", ""])
        col.seed_note(nt, other_deck, ["c", "d", ""])
        self.assertEqual(len(notes_needing_audio(col, "Clone")), 2)


if __name__ == "__main__":
    unittest.main()
