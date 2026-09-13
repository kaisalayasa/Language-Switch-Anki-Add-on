"""Batch-generate Piper TTS audio into real notes (M5).

Connects M2's standalone, already-proven Piper pipeline (``addon/tts/``) to real notes for
the first time. The ``TARGET_AUDIO``/``TARGET_SENTENCE_AUDIO`` template blocks have existed
since M1 (deliberately left unreferenced -- ``TemplateOptions.include_audio=False`` --
until this module could actually fill the fields they point at).

Every note-processing function here takes an injected ``provider`` (a
:class:`~addon.tts.provider_base.TTSProvider`), so this module is unit-testable against
``tests/fake_collection.py`` exactly the way ``notetype_manager.py`` already is -- no real
Anki, no real Piper, in the test suite.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from ..core.role_schema import AUDIO_SOURCE_ROLES, RoleMapping
from ..core.template_generator import TemplateOptions, generate_templates
from ..tts.piper_provider import EmptyTextError
from ..tts.provider_base import TTSProvider
from .notetype_manager import shape_notetype

__all__ = [
    "AUDIO_DONE_TAG",
    "NoteAudioResult",
    "PendingSynthesis",
    "notes_needing_audio",
    "generate_note_audio",
    "plan_note_audio",
    "apply_note_audio",
    "finish_audio_batch",
]

#: Marks a note as having current, generated audio. The resumability/caching mechanism for
#: the whole batch: a re-run only touches notes without this tag, unless ``force=True``.
#: Namespaced (matches template_generator.py's ``ddc`` CSS-class prefix) so it reads
#: unambiguously in the Browser as belonging to this addon.
AUDIO_DONE_TAG = "ddc-tts-generated"


@dataclass
class NoteAudioResult:
    note_id: int
    fields_written: List[str] = field(default_factory=list)
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
    mapping: RoleMapping,
    provider: TTSProvider,
    voice_id: str,
) -> NoteAudioResult:
    """(Re)synthesize and write every audio field this mapping's roles call for.

    A source field that sanitizes to empty text (:class:`EmptyTextError`) is skipped for
    that field only -- not every note has a sentence. Any other synthesis failure aborts
    *this note only*: it's reported, left untagged, and retried on the next run rather than
    aborting the whole batch over one bad note.
    """
    result = NoteAudioResult(note_id=note.id)

    for audio_role, source_role in AUDIO_SOURCE_ROLES.items():
        audio_field = mapping.first(audio_role)
        source_field = mapping.first(source_role)
        if audio_field is None or source_field is None:
            continue
        try:
            text = note[source_field.name]
        except (KeyError, IndexError):
            continue

        try:
            wav_path = provider.synthesize(text, voice_id=voice_id)
        except EmptyTextError:
            continue
        except Exception as exc:  # noqa: BLE001 -- one note's failure must not sink the batch
            result.error = "%s: %s" % (type(exc).__name__, exc)
            return result

        filename = col.media.add_file(str(wav_path))
        note[audio_field.name] = "[sound:%s]" % filename
        result.fields_written.append(audio_field.name)

    if result.changed:
        if AUDIO_DONE_TAG not in note.tags:
            note.tags.append(AUDIO_DONE_TAG)
        col.update_note(note)
    return result


@dataclass(frozen=True)
class PendingSynthesis:
    """One audio field a note still needs, with the raw source text to speak."""

    audio_field: str
    text: str


def plan_note_audio(note: Any, mapping: RoleMapping) -> List[PendingSynthesis]:
    """The read-only half of :func:`generate_note_audio`: what needs (re)synthesizing for
    this note, without calling a provider or writing anything.

    Split out so a batch runner can synthesize many notes' text concurrently in a thread
    pool -- safe, since this and :func:`~addon.tts.piper_provider.PiperProvider.synthesize`
    touch no shared state -- while still writing every result back into the collection one
    note at a time via :func:`apply_note_audio`, on whichever single thread owns ``col``.
    Anki's collection is not documented as safe for concurrent access from multiple Python
    threads, so nothing here or in a caller may call a ``col``/``note`` method from more
    than one thread at once.
    """
    pending: List[PendingSynthesis] = []
    for audio_role, source_role in AUDIO_SOURCE_ROLES.items():
        audio_field = mapping.first(audio_role)
        source_field = mapping.first(source_role)
        if audio_field is None or source_field is None:
            continue
        try:
            text = note[source_field.name]
        except (KeyError, IndexError):
            continue
        pending.append(PendingSynthesis(audio_field.name, text))
    return pending


def apply_note_audio(
    col: Any,
    note: Any,
    synthesized: List[Tuple[str, Path]],
    *,
    error: Optional[str] = None,
) -> NoteAudioResult:
    """The write-only half of :func:`generate_note_audio`: writes already-synthesized
    ``(audio_field_name, wav_path)`` pairs into ``note``, atomically, exactly like it does.

    Pass ``error`` for a hard synthesis failure (anything but "nothing to say") -- nothing
    is written and the note is left untagged, so it's retried on the next run, matching
    "one bad note must not sink the batch, but must not be falsely marked done either."
    """
    result = NoteAudioResult(note_id=note.id)
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


def finish_audio_batch(
    col: Any,
    clone_notetype: Dict[str, Any],
    mapping: RoleMapping,
    *,
    source_css: str,
    options: TemplateOptions,
) -> None:
    """Flip the clone's template to actually reference the now-populated audio fields.

    A one-time, end-of-batch schema change -- not per note. Audio fields can be silently
    filled in for the whole batch while the template still doesn't reference them; this is
    what turns that on. ``options.include_audio`` should already be ``True`` -- this
    function does not set it, since the caller also controls ``audio_on_front`` and
    ``include_unmapped``, which should match whatever the original conversion used.

    Reuses :func:`~addon.ops.notetype_manager.shape_notetype` with the clone's own name (so
    it updates the existing notetype rather than creating a new one) instead of duplicating
    template-writing logic. ``shape_notetype`` always sets ``id = 0`` -- correct for the
    fresh-clone case it was built for (a not-yet-added notetype), wrong here: this is an
    *update* to an existing notetype, so the real id is restored before saving. Getting this
    backwards would silently update/create the wrong notetype.
    """
    templates = generate_templates(mapping, source_css=source_css, options=options)
    shaped = shape_notetype(
        clone_notetype,
        name=clone_notetype["name"],
        front=templates.front_html,
        back=templates.back_html,
        css=templates.css,
        template_name=options.template_name,
    )
    shaped["id"] = clone_notetype["id"]
    col.models.update_dict(shaped)
