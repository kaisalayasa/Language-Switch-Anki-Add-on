"""Batch-generate Piper TTS audio into real notes.

Connects ``llm.direction.resolve_direction``'s result to real notes: direction.py already
decides, deterministically, which source field(s) to speak and which empty field each one's
audio goes into (``Direction.audio_targets`` -- one ``(source_field, audio_field)`` pair per
real front-content field; see ``direction.py`` for why this is no longer an AI decision), so
this module's only job is running Piper over each of those source fields for every note that
still needs it.

A note can need MORE than one audio field filled (e.g. both a word and an example sentence on
the front) -- every function here takes ``targets``, a sequence of ``(audio_field,
source_field)`` pairs, rather than a single pair, and processes all of them together per note.
See ``generate_note_audio`` for how a real synthesis failure partway through a note's targets is
handled (the whole note is left untouched and retried next run, not partially saved).

The old ``finish_audio_batch`` step -- flipping the template to reference audio fields only
once a whole batch had finished -- no longer exists. It existed because the previous template
generator only started referencing an audio field once ``TemplateOptions.include_audio`` was
turned on, which created a real hazard: a partial run could flip the switch for notes it hadn't
reached yet, which still held their *original* audio. The model's Front HTML now gets
``{{#field}}{{field}}{{/field}}`` appended for each audio target at analysis time (see
``llm.analyze._append_audio_html``) -- each field starts empty and renders as nothing until TTS
actually fills it, so there is no switch left to flip and no window in which the wrong audio
could play.

Every note-processing function here takes an injected ``provider`` (a
:class:`~addon.tts.provider_base.TTSProvider`), so this module is unit-testable against
``tests/fake_collection.py`` -- no real Anki, no real Piper, in the test suite.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, List, Optional, Sequence, Tuple

from ..core.audio_fields import AUDIO_DONE_TAG
from ..tts.piper_provider import EmptyTextError
from ..tts.provider_base import TTSProvider

__all__ = [
    "AUDIO_DONE_TAG",
    "NoteAudioResult",
    "PendingSynthesis",
    "notes_needing_audio",
    "generate_note_audio",
    "plan_note_audio",
    "apply_note_audio",
]


@dataclass
class NoteAudioResult:
    note_id: int
    fields_written: List[str] = field(default_factory=list)
    #: Set when the source field sanitized to nothing to synthesize -- empty on this note, or
    #: held only characters the chosen voice cannot speak. Counted and reported rather than
    #: passed over in silence: a whole run of these looks exactly like success (no errors,
    #: finishes fast) while producing no audio whatsoever, which is a confusing way to
    #: discover a voice/deck mismatch.
    skipped: List[str] = field(default_factory=list)
    #: Set only for a real synthesis failure (not "nothing to say") -- the note is left
    #: untagged and will be retried on the next run.
    error: Optional[str] = None

    @property
    def ok(self) -> bool:
        return self.error is None

    @property
    def changed(self) -> bool:
        return bool(self.fields_written)


def notes_needing_audio(col: Any, notetype_name: str, *, force: bool = False) -> List[int]:
    """Notes on ``notetype_name`` that still need (re)generated audio.

    Scoped by notetype only -- a converted clone's notes are exactly the ones to process,
    regardless of which deck(s) they're in. Interruption-safe by construction: each note's
    write is independently atomic (see :func:`generate_note_audio`), so a crash mid-batch
    just means the untagged notes get picked up again here next time.

    The tag filter is applied in Python rather than folded into the search string, so this
    behaves identically whether ``col`` is a real collection or the test double in
    ``tests/fake_collection.py`` -- it doesn't depend on both parsing ``-tag:`` the same way.
    """
    note_ids = list(col.find_notes('note:"%s"' % notetype_name.replace('"', '\\"')))
    if force:
        return note_ids
    return [nid for nid in note_ids if AUDIO_DONE_TAG not in col.get_note(nid).tags]


def generate_note_audio(
    col: Any,
    note: Any,
    provider: TTSProvider,
    voice_id: str,
    *,
    targets: Sequence[Tuple[str, str]],
) -> NoteAudioResult:
    """(Re)synthesize each ``(audio_field, source_field)`` pair in ``targets`` for one note.

    A source field that sanitizes to empty text (:class:`EmptyTextError`) is skipped for just
    that one target -- not every note necessarily has content in every field, and the others
    still get synthesized. Any other synthesis failure aborts *this note only*: nothing is
    saved or tagged done, not even targets that already succeeded earlier in the same loop, so
    ``result.fields_written`` always accurately reflects what actually ended up persisted, and a
    re-run retries the whole note cleanly rather than leaving it in an ambiguous
    partially-filled state. One bad note must never sink the whole batch, but it also must
    never be tagged done when it isn't. (A target that succeeded before a later one fails has
    already imported its audio into the media folder, which -- unlike the note write -- can't be
    rolled back; that file is simply orphaned until Anki's own unused-media cleanup finds it,
    the same acceptable tradeoff this addon already makes everywhere else for media cleanup --
    see claude.md's "explicit opt-in" rule.)
    """
    result = NoteAudioResult(note_id=note.id)
    written: List[str] = []

    for audio_field, source_field in targets:
        try:
            text = note[source_field]
        except (KeyError, IndexError, ValueError):
            continue

        try:
            wav_path = provider.synthesize(text, voice_id=voice_id)
        except EmptyTextError:
            result.skipped.append(audio_field)
            continue
        except Exception as exc:  # noqa: BLE001 -- one note's failure must not sink the batch
            result.error = "%s: %s" % (type(exc).__name__, exc)
            return result

        filename = col.media.add_file(str(wav_path))
        note[audio_field] = "[sound:%s]" % filename
        written.append(audio_field)

    if written:
        result.fields_written = written
        if AUDIO_DONE_TAG not in note.tags:
            note.tags.append(AUDIO_DONE_TAG)
        col.update_note(note)
    return result


@dataclass(frozen=True)
class PendingSynthesis:
    """The one audio field a note still needs, with the raw source text to speak."""

    audio_field: str
    text: str


def plan_note_audio(note: Any, *, targets: Sequence[Tuple[str, str]]) -> List[PendingSynthesis]:
    """The read-only half of :func:`generate_note_audio`: what needs (re)synthesizing for
    this note, for every ``(audio_field, source_field)`` pair in ``targets``, without calling a
    provider or writing anything.

    Split out so a batch runner can synthesize many notes' text concurrently in a thread
    pool -- safe, since this and :func:`~addon.tts.piper_provider.PiperProvider.synthesize`
    touch no shared state -- while still writing every result back into the collection one
    note at a time via :func:`apply_note_audio`, on whichever single thread owns ``col``.
    Anki's collection is not documented as safe for concurrent access from multiple Python
    threads, so nothing here or in a caller may call a ``col``/``note`` method from more than
    one thread at once.
    """
    plans: List[PendingSynthesis] = []
    for audio_field, source_field in targets:
        try:
            text = note[source_field]
        except (KeyError, IndexError, ValueError):
            continue
        plans.append(PendingSynthesis(audio_field, text))
    return plans


def apply_note_audio(
    col: Any,
    note: Any,
    synthesized: List[Tuple[str, Path]],
    *,
    error: Optional[str] = None,
    skipped: Optional[List[str]] = None,
) -> NoteAudioResult:
    """The write-only half of :func:`generate_note_audio`: writes already-synthesized
    ``(audio_field, wav_path)`` pairs into ``note``, atomically, exactly like it does.

    Pass ``error`` for a hard synthesis failure (anything but "nothing to say") -- nothing is
    written and the note is left untagged, so it's retried on the next run, matching "one bad
    note must not sink the batch, but must not be falsely marked done either."
    """
    result = NoteAudioResult(note_id=note.id, skipped=list(skipped or []))
    if error is not None:
        result.error = error
        return result
    for audio_field, wav_path in synthesized:
        filename = col.media.add_file(str(wav_path))
        note[audio_field] = "[sound:%s]" % filename
        result.fields_written.append(audio_field)
    if result.changed:
        if AUDIO_DONE_TAG not in note.tags:
            note.tags.append(AUDIO_DONE_TAG)
        col.update_note(note)
    return result
