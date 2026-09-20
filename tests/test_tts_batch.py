"""Batch TTS generation, against ``FakeCollection`` and an injected fake provider -- no real
Anki, no real Piper.

A note can need more than one audio field filled now -- e.g. both a word and a full example
sentence, per ``llm.direction.Direction.audio_targets``. Every function here takes ``targets``,
a sequence of ``(audio_field, source_field)`` pairs, and processes all of them together per
note; ``TestMultipleTargetsPerNote`` is the coverage for that being more than a single pair.
There is also no ``finish_audio_batch`` step: the analysis step already writes a bare reference
to each audio field into the front template it produces, and each field starts empty, so there
is nothing left to "turn on" once a batch finishes.
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

FIELDS = ["Word", "Sentence", "Meaning", "AudioWord", "AudioSentence"]
VOICE_ID = "en_US-ljspeech-high"

AUDIO_FIELD = "AudioWord"
SOURCE_FIELD = "Word"
SINGLE_TARGET = [(AUDIO_FIELD, SOURCE_FIELD)]
TWO_TARGETS = [("AudioWord", "Word"), ("AudioSentence", "Sentence")]


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
        note = col.seed_note(nt, deck, ["hello", "a sentence", "bonjour", "", ""])
        provider = FakeProvider()

        result = generate_note_audio(col, note, provider, VOICE_ID, targets=SINGLE_TARGET)

        self.assertTrue(result.ok)
        self.assertEqual(result.fields_written, ["AudioWord"])
        self.assertTrue(note["AudioWord"].startswith("[sound:"))
        self.assertIn(AUDIO_DONE_TAG, note.tags)
        self.assertEqual(provider.calls, ["hello"])

    def test_empty_source_field_is_skipped_not_an_error(self):
        col, nt, deck = make_collection()
        note = col.seed_note(nt, deck, ["", "a sentence", "bonjour", "", ""])
        provider = FakeProvider()

        result = generate_note_audio(col, note, provider, VOICE_ID, targets=SINGLE_TARGET)

        self.assertTrue(result.ok)
        self.assertEqual(result.fields_written, [])
        self.assertEqual(result.skipped, ["AudioWord"])
        self.assertEqual(note["AudioWord"], "")
        self.assertNotIn(AUDIO_DONE_TAG, note.tags)

    def test_synthesis_failure_leaves_note_untagged_and_unwritten(self):
        col, nt, deck = make_collection()
        note = col.seed_note(nt, deck, ["hello", "a sentence", "bonjour", "", ""])
        provider = FakeProvider(fail_for=["hello"])

        result = generate_note_audio(col, note, provider, VOICE_ID, targets=SINGLE_TARGET)

        self.assertFalse(result.ok)
        self.assertIn("synthesis exploded", result.error)
        self.assertNotIn(AUDIO_DONE_TAG, note.tags)
        self.assertEqual(note["AudioWord"], "", "must not write a partial/wrong result")

    def test_a_source_field_that_does_not_exist_on_the_note_is_left_alone(self):
        col, nt, deck = make_collection()
        note = col.seed_note(nt, deck, ["hello", "a sentence", "bonjour", "", ""])

        result = generate_note_audio(
            col, note, FakeProvider(), VOICE_ID, targets=[("AudioWord", "Nonexistent")]
        )

        self.assertTrue(result.ok)
        self.assertEqual(result.fields_written, [])
        self.assertFalse(result.changed)
        self.assertNotIn(AUDIO_DONE_TAG, note.tags)


class TestMultipleTargetsPerNote(unittest.TestCase):
    """A note can need more than one audio field filled -- e.g. both a word and a full example
    sentence. This is the actual point of ``targets`` being a list rather than a single pair."""

    def test_every_targets_audio_field_is_written(self):
        col, nt, deck = make_collection()
        note = col.seed_note(nt, deck, ["hello", "a sentence", "bonjour", "", ""])
        provider = FakeProvider()

        result = generate_note_audio(col, note, provider, VOICE_ID, targets=TWO_TARGETS)

        self.assertTrue(result.ok)
        self.assertEqual(sorted(result.fields_written), ["AudioSentence", "AudioWord"])
        self.assertTrue(note["AudioWord"].startswith("[sound:"))
        self.assertTrue(note["AudioSentence"].startswith("[sound:"))
        self.assertIn(AUDIO_DONE_TAG, note.tags)

    def test_one_empty_target_does_not_block_the_other(self):
        """The word has content, the sentence field is empty on this note -- the word still
        gets synthesized, and the note still ends up tagged done since nothing is genuinely
        still pending for it."""
        col, nt, deck = make_collection()
        note = col.seed_note(nt, deck, ["hello", "", "bonjour", "", ""])
        provider = FakeProvider()

        result = generate_note_audio(col, note, provider, VOICE_ID, targets=TWO_TARGETS)

        self.assertTrue(result.ok)
        self.assertEqual(result.fields_written, ["AudioWord"])
        self.assertEqual(result.skipped, ["AudioSentence"])
        self.assertIn(AUDIO_DONE_TAG, note.tags)

    def test_a_hard_failure_on_one_target_aborts_the_whole_note(self):
        """The word succeeds first, then the sentence hits a real synthesis error --
        ``fields_written`` ends up empty and the note is never tagged done, so a re-run
        retries both targets, not just the one that actually failed. (The word's audio is
        still imported into the media folder by that point -- an accepted, orphaned-file
        tradeoff; see the function's own docstring.)"""
        col, nt, deck = make_collection()
        note = col.seed_note(nt, deck, ["hello", "a sentence", "bonjour", "", ""])
        provider = FakeProvider(fail_for=["a sentence"])

        result = generate_note_audio(col, note, provider, VOICE_ID, targets=TWO_TARGETS)

        self.assertFalse(result.ok)
        self.assertEqual(result.fields_written, [])
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

    def test_plan_lists_the_source_text_for_every_target(self):
        col, nt, deck = make_collection()
        note = col.seed_note(nt, deck, ["hello", "a sentence", "bonjour", "", ""])

        pending = plan_note_audio(note, targets=TWO_TARGETS)

        self.assertEqual(
            sorted((p.audio_field, p.text) for p in pending),
            [("AudioSentence", "a sentence"), ("AudioWord", "hello")],
        )

    def test_plan_includes_empty_source_text_unfiltered(self):
        """Skipping empty text is the provider's job (``EmptyTextError``), not the plan's --
        the plan is purely descriptive of what's bound."""
        col, nt, deck = make_collection()
        note = col.seed_note(nt, deck, ["", "a sentence", "bonjour", "", ""])

        pending = plan_note_audio(note, targets=SINGLE_TARGET)

        self.assertEqual([(p.audio_field, p.text) for p in pending], [("AudioWord", "")])

    def test_apply_writes_the_field_and_tags_like_generate_note_audio(self):
        col, nt, deck = make_collection()
        note = col.seed_note(nt, deck, ["hello", "a sentence", "bonjour", "", ""])

        result = apply_note_audio(col, note, [("AudioWord", Path("/tmp/a.wav"))])

        self.assertTrue(result.ok)
        self.assertEqual(result.fields_written, ["AudioWord"])
        self.assertTrue(note["AudioWord"].startswith("[sound:"))
        self.assertIn(AUDIO_DONE_TAG, note.tags)

    def test_apply_writes_multiple_fields_from_one_note_in_one_call(self):
        col, nt, deck = make_collection()
        note = col.seed_note(nt, deck, ["hello", "a sentence", "bonjour", "", ""])

        result = apply_note_audio(
            col, note, [("AudioWord", Path("/tmp/a.wav")), ("AudioSentence", Path("/tmp/b.wav"))]
        )

        self.assertTrue(result.ok)
        self.assertEqual(sorted(result.fields_written), ["AudioSentence", "AudioWord"])
        self.assertTrue(note["AudioWord"].startswith("[sound:"))
        self.assertTrue(note["AudioSentence"].startswith("[sound:"))

    def test_apply_with_error_writes_nothing_and_leaves_note_untagged(self):
        col, nt, deck = make_collection()
        note = col.seed_note(nt, deck, ["hello", "a sentence", "bonjour", "", ""])

        result = apply_note_audio(col, note, [], error="RuntimeError: synthesis exploded")

        self.assertFalse(result.ok)
        self.assertEqual(note["AudioWord"], "")
        self.assertNotIn(AUDIO_DONE_TAG, note.tags)

    def test_plan_then_apply_matches_generate_note_audio_on_the_same_note(self):
        col, nt, deck = make_collection()
        a = col.seed_note(nt, deck, ["hello", "a sentence", "bonjour", "", ""])
        b = col.seed_note(nt, deck, ["hello", "a sentence", "bonjour", "", ""])

        sequential = generate_note_audio(col, a, FakeProvider(), VOICE_ID, targets=TWO_TARGETS)

        pending = plan_note_audio(b, targets=TWO_TARGETS)
        synthesized, error = _synth_all(pending, FakeProvider(), VOICE_ID)
        split = apply_note_audio(col, b, synthesized, error=error)

        self.assertEqual(sequential.ok, split.ok)
        self.assertEqual(sorted(sequential.fields_written), sorted(split.fields_written))
        self.assertEqual(AUDIO_DONE_TAG in a.tags, AUDIO_DONE_TAG in b.tags)

    def test_plan_then_apply_matches_generate_note_audio_on_hard_failure(self):
        col, nt, deck = make_collection()
        a = col.seed_note(nt, deck, ["hello", "a sentence", "bonjour", "", ""])
        b = col.seed_note(nt, deck, ["hello", "a sentence", "bonjour", "", ""])

        sequential = generate_note_audio(
            col, a, FakeProvider(fail_for=["hello"]), VOICE_ID, targets=SINGLE_TARGET
        )

        pending = plan_note_audio(b, targets=SINGLE_TARGET)
        synthesized, error = _synth_all(pending, FakeProvider(fail_for=["hello"]), VOICE_ID)
        split = apply_note_audio(col, b, synthesized or [], error=error)

        self.assertFalse(sequential.ok)
        self.assertFalse(split.ok)
        self.assertEqual(a["AudioWord"], b["AudioWord"], "both must leave the field untouched")
        self.assertEqual(AUDIO_DONE_TAG in a.tags, AUDIO_DONE_TAG in b.tags)


class TestNotesNeedingAudio(unittest.TestCase):
    def test_second_run_skips_already_tagged_notes(self):
        col, nt, deck = make_collection()
        a = col.seed_note(nt, deck, ["hello", "a sentence", "bonjour", "", ""])
        b = col.seed_note(nt, deck, ["goodbye", "another sentence", "au revoir", "", ""])
        provider = FakeProvider()

        pending = notes_needing_audio(col, "Clone")
        self.assertEqual(sorted(pending), sorted([a.id, b.id]))

        generate_note_audio(col, a, provider, VOICE_ID, targets=SINGLE_TARGET)

        pending_after = notes_needing_audio(col, "Clone")
        self.assertEqual(pending_after, [b.id])

    def test_force_reprocesses_already_tagged_notes(self):
        col, nt, deck = make_collection()
        note = col.seed_note(nt, deck, ["hello", "a sentence", "bonjour", "", ""])
        generate_note_audio(col, note, FakeProvider(), VOICE_ID, targets=SINGLE_TARGET)

        self.assertEqual(notes_needing_audio(col, "Clone"), [])
        self.assertEqual(notes_needing_audio(col, "Clone", force=True), [note.id])

    def test_scoped_by_notetype_only_not_deck(self):
        col, nt, deck = make_collection()
        other_deck = col.add_deck("Elsewhere")
        col.seed_note(nt, deck, ["a", "s", "b", "", ""])
        col.seed_note(nt, other_deck, ["c", "s", "d", "", ""])
        self.assertEqual(len(notes_needing_audio(col, "Clone")), 2)


if __name__ == "__main__":
    unittest.main()
