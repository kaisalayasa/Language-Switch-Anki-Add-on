"""Batch TTS generation (M5), against ``FakeCollection`` and an injected fake provider --
no real Anki, no real Piper.
"""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from addon.core.role_schema import FieldBinding, Role, RoleMapping
from addon.core.template_generator import TemplateOptions
from addon.ops.tts_batch import (
    AUDIO_DONE_TAG,
    apply_note_audio,
    finish_audio_batch,
    generate_note_audio,
    notes_needing_audio,
    plan_note_audio,
)
from addon.tts.piper_provider import EmptyTextError

from tests.fake_collection import FakeCollection

FIELDS = ["Word", "Meaning", "Sentence", "Audio", "SentenceAudio"]
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


def make_mapping():
    fields = [FieldBinding(n, i) for i, n in enumerate(FIELDS)]
    mapping = RoleMapping(notetype_name="Clone", fields=fields)
    mapping.bind(Role.TARGET_TERM, fields[0])
    mapping.bind(Role.NATIVE_TERM, fields[1])
    mapping.bind(Role.TARGET_SENTENCE, fields[2])
    mapping.bind(Role.TARGET_AUDIO, fields[3])
    mapping.bind(Role.TARGET_SENTENCE_AUDIO, fields[4])
    return mapping


class TestGenerateNoteAudio(unittest.TestCase):
    def test_writes_both_audio_fields_and_tags_the_note(self):
        col, nt, deck = make_collection()
        note = col.seed_note(nt, deck, ["hello", "bonjour", "hello there", "", ""])
        provider = FakeProvider()

        result = generate_note_audio(col, note, make_mapping(), provider, VOICE_ID)

        self.assertTrue(result.ok)
        self.assertEqual(set(result.fields_written), {"Audio", "SentenceAudio"})
        self.assertTrue(note["Audio"].startswith("[sound:"))
        self.assertTrue(note["SentenceAudio"].startswith("[sound:"))
        self.assertIn(AUDIO_DONE_TAG, note.tags)
        self.assertEqual(sorted(provider.calls), ["hello", "hello there"])

    def test_empty_sentence_skips_only_that_field(self):
        col, nt, deck = make_collection()
        note = col.seed_note(nt, deck, ["hello", "bonjour", "", "", ""])
        provider = FakeProvider()

        result = generate_note_audio(col, note, make_mapping(), provider, VOICE_ID)

        self.assertTrue(result.ok)
        self.assertEqual(result.fields_written, ["Audio"])
        self.assertEqual(note["SentenceAudio"], "")
        self.assertIn(AUDIO_DONE_TAG, note.tags)

    def test_synthesis_failure_leaves_note_untagged_and_unwritten(self):
        col, nt, deck = make_collection()
        note = col.seed_note(nt, deck, ["hello", "bonjour", "hello there", "", ""])
        provider = FakeProvider(fail_for=["hello"])

        result = generate_note_audio(col, note, make_mapping(), provider, VOICE_ID)

        self.assertFalse(result.ok)
        self.assertIn("synthesis exploded", result.error)
        self.assertNotIn(AUDIO_DONE_TAG, note.tags)
        self.assertEqual(note["Audio"], "", "must not write a partial/wrong result")

    def test_unbound_audio_role_is_left_alone(self):
        col, nt, deck = make_collection()
        note = col.seed_note(nt, deck, ["hello", "bonjour", "hello there", "", ""])
        fields = [FieldBinding(n, i) for i, n in enumerate(FIELDS)]
        mapping = RoleMapping(notetype_name="Clone", fields=fields)
        mapping.bind(Role.TARGET_TERM, fields[0])
        # No audio roles bound at all.

        result = generate_note_audio(col, note, mapping, FakeProvider(), VOICE_ID)

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

    def test_plan_lists_every_bound_audio_role_with_its_raw_source_text(self):
        col, nt, deck = make_collection()
        note = col.seed_note(nt, deck, ["hello", "bonjour", "hello there", "", ""])

        pending = plan_note_audio(note, make_mapping())

        self.assertEqual(
            sorted((p.audio_field, p.text) for p in pending),
            sorted([("Audio", "hello"), ("SentenceAudio", "hello there")]),
        )

    def test_plan_includes_empty_source_text_unfiltered(self):
        """Skipping empty text is the provider's job (``EmptyTextError``), not the plan's --
        the plan is purely descriptive of what's bound."""
        col, nt, deck = make_collection()
        note = col.seed_note(nt, deck, ["hello", "bonjour", "", "", ""])

        pending = plan_note_audio(note, make_mapping())

        self.assertIn(("SentenceAudio", ""), [(p.audio_field, p.text) for p in pending])

    def test_apply_writes_fields_and_tags_like_generate_note_audio(self):
        col, nt, deck = make_collection()
        note = col.seed_note(nt, deck, ["hello", "bonjour", "hello there", "", ""])

        result = apply_note_audio(
            col, note, [("Audio", Path("/tmp/a.wav")), ("SentenceAudio", Path("/tmp/b.wav"))]
        )

        self.assertTrue(result.ok)
        self.assertEqual(set(result.fields_written), {"Audio", "SentenceAudio"})
        self.assertTrue(note["Audio"].startswith("[sound:"))
        self.assertIn(AUDIO_DONE_TAG, note.tags)

    def test_apply_with_error_writes_nothing_and_leaves_note_untagged(self):
        col, nt, deck = make_collection()
        note = col.seed_note(nt, deck, ["hello", "bonjour", "hello there", "", ""])

        result = apply_note_audio(col, note, [], error="RuntimeError: synthesis exploded")

        self.assertFalse(result.ok)
        self.assertEqual(note["Audio"], "")
        self.assertNotIn(AUDIO_DONE_TAG, note.tags)

    def test_plan_then_apply_matches_generate_note_audio_on_the_same_note(self):
        col, nt, deck = make_collection()
        a = col.seed_note(nt, deck, ["hello", "bonjour", "hello there", "", ""])
        b = col.seed_note(nt, deck, ["hello", "bonjour", "hello there", "", ""])
        mapping = make_mapping()

        sequential = generate_note_audio(col, a, mapping, FakeProvider(), VOICE_ID)

        pending = plan_note_audio(b, mapping)
        synthesized, error = _synth_all(pending, FakeProvider(), VOICE_ID)
        split = apply_note_audio(col, b, synthesized, error=error)

        self.assertEqual(sequential.ok, split.ok)
        self.assertEqual(sorted(sequential.fields_written), sorted(split.fields_written))
        self.assertEqual(AUDIO_DONE_TAG in a.tags, AUDIO_DONE_TAG in b.tags)

    def test_plan_then_apply_matches_generate_note_audio_on_hard_failure(self):
        col, nt, deck = make_collection()
        a = col.seed_note(nt, deck, ["hello", "bonjour", "hello there", "", ""])
        b = col.seed_note(nt, deck, ["hello", "bonjour", "hello there", "", ""])
        mapping = make_mapping()

        sequential = generate_note_audio(col, a, mapping, FakeProvider(fail_for=["hello"]), VOICE_ID)

        pending = plan_note_audio(b, mapping)
        synthesized, error = _synth_all(pending, FakeProvider(fail_for=["hello"]), VOICE_ID)
        split = apply_note_audio(col, b, synthesized or [], error=error)

        self.assertFalse(sequential.ok)
        self.assertFalse(split.ok)
        self.assertEqual(a["Audio"], b["Audio"], "both must leave the field untouched")
        self.assertEqual(AUDIO_DONE_TAG in a.tags, AUDIO_DONE_TAG in b.tags)


class TestNotesNeedingAudio(unittest.TestCase):
    def test_second_run_skips_already_tagged_notes(self):
        col, nt, deck = make_collection()
        a = col.seed_note(nt, deck, ["hello", "bonjour", "", "", ""])
        b = col.seed_note(nt, deck, ["goodbye", "au revoir", "", "", ""])
        mapping = make_mapping()
        provider = FakeProvider()

        pending = notes_needing_audio(col, "Clone")
        self.assertEqual(sorted(pending), sorted([a.id, b.id]))

        generate_note_audio(col, a, mapping, provider, VOICE_ID)

        pending_after = notes_needing_audio(col, "Clone")
        self.assertEqual(pending_after, [b.id])

    def test_force_reprocesses_already_tagged_notes(self):
        col, nt, deck = make_collection()
        note = col.seed_note(nt, deck, ["hello", "bonjour", "", "", ""])
        generate_note_audio(col, note, make_mapping(), FakeProvider(), VOICE_ID)

        self.assertEqual(notes_needing_audio(col, "Clone"), [])
        self.assertEqual(notes_needing_audio(col, "Clone", force=True), [note.id])

    def test_scoped_by_notetype_only_not_deck(self):
        col, nt, deck = make_collection()
        other_deck = col.add_deck("Elsewhere")
        col.seed_note(nt, deck, ["a", "b", "", "", ""])
        col.seed_note(nt, other_deck, ["c", "d", "", "", ""])
        self.assertEqual(len(notes_needing_audio(col, "Clone")), 2)


class TestFinishAudioBatch(unittest.TestCase):
    def test_flips_include_audio_on_without_losing_other_content(self):
        col, nt, deck = make_collection()
        mapping = make_mapping()
        options = TemplateOptions(include_audio=True, template_name="Production")

        finish_audio_batch(col, nt, mapping, source_css=".card{color:red}", options=options)

        updated = col.models.by_name("Clone")
        self.assertIn("Audio", updated["tmpls"][0]["afmt"] + updated["tmpls"][0]["qfmt"])
        self.assertIn("color:red", updated["css"])
        self.assertEqual(updated["tmpls"][0]["name"], "Production")
        self.assertEqual(len(updated["tmpls"]), 1)


if __name__ == "__main__":
    unittest.main()
