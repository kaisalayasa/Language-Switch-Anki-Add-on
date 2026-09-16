"""Deterministic resolution of direction, target/native language, field placement, and which
front fields get generated audio.

Why the model doesn't decide direction any more: across every real test run today (see
``docs/llm-notes.md``), the model never once correctly executed "whichever fields are
currently on the back move to the new front, and vice versa" -- regardless of whether it was
told the fact plainly, told it with an explicit worked-out swap rule, given the language of
each side directly, or split into a separate call whose only job was this one decision. It
consistently either described the current, unflipped state as if nothing had changed, or (when
further confused, e.g. by a deck name that didn't match the content) fell back to reproducing
the system prompt's own worked example almost verbatim. Four different phrasings failed
identically. That is evidence of an abstract-reasoning limit on this specific task, not of
insufficient information -- so the fix is to stop asking: direction, target/native language,
and field placement are computed here, from facts that are already fully computable
(``template_fields.current_sides`` for structure, ``core.language_detect.detect_field_language``
for content, both already proven accurate against real deck content). The model's job narrows
to writing HTML for a placement it is given.

Placement is decided **per field**, not by relocating each current side as a whole block, which
matters for a deck like Core 2000: its current "back" (the answer reveal) mixes English
translation together with Japanese reading aids (furigana) shown alongside it. Swapping that
whole group onto the new front would incorrectly drag the Japanese reading aids over with the
English content. Instead, each field's own detected language decides which new side it belongs
on; only the *side-level* majority language (used to decide which language counts as target vs
native at all) comes from ``current_sides``.

**Re-analyzing an already-converted notetype is a real case, not a hypothetical one** -- a user
reopening the addon later just to generate more TTS audio may still go through Analyze first (see
``known_state``, and ``ui/main_screen.py`` for when it isn't even needed any more).
``current_sides`` alone cannot tell "never converted" apart from "already converted": on an
already-converted notetype, the current front genuinely speaks the target language, so blindly
reading "front = native, back = target" from template structure -- the rule that's correct the
*first* time -- reads it exactly backwards the second time and flips the deck right back to its
original direction. That is the same double-flip bug class the old role-detection system had,
reached a different way. The fix is the same shape as ``deck_state`` itself: a fact recorded at
conversion time, given here rather than re-derived. See ``resolve_direction``'s ``known_state``
parameter.

**Which fields get audio is deterministic too, not an AI choice.** The model used to pick a
single ``speak_text_from`` field to read aloud -- a real judgment call when a front carries both
a short word/term and a full example sentence. Removed once it became clear there was nothing
left to actually choose: every field this module places on the new front already passed the
"confident, real target-language content" bar (``_field_language``), which is exactly what
"worth generating audio for" means anyway -- so every such field gets its own paired audio field
(``core.audio_fields.generated_audio_field_name``) instead of the model preferring one over
another. A field this addon already created for audio on a previous conversion (recognised via
``core.audio_fields.is_generated_field``) is excluded entirely from both the given placement and
from getting a *new* audio target: it doesn't need audio generated for it, it already carries (or
is waiting to carry) audio itself, is kept on the front unconditionally, and is never shown to the
model as something to place -- see ``resolve_direction``.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import List, Optional, Sequence, Tuple

from ..core.audio_fields import generated_audio_field_name, is_generated_field
from ..core.deck_state import ConversionState
from ..core.language_detect import detect_field_language
from .audio_safety import SOUND_TAG_RE
from .template_fields import current_sides

__all__ = ["AudioTarget", "Direction", "resolve_direction"]

_TAG_RE = re.compile(r"<[^>]+>")


def _visible_text(raw: str) -> str:
    """Strip markup that would otherwise confuse language detection -- audio references and
    HTML tags. Separate from ``prompt._clean_for_display``, which only strips audio markup and
    deliberately keeps HTML visible to the model; this is for the detector, not the model."""
    text = SOUND_TAG_RE.sub(" ", raw or "")
    text = _TAG_RE.sub(" ", text)
    return text.strip()


def _side_language(field_names: Sequence[str], by_name: dict) -> Optional[str]:
    """The dominant language of a group of fields, weighted by how much real text each one
    carries -- the field with the most content decides it, so a short throwaway field (a POS
    tag, an empty-most-of-the-time field) can't outvote the field that actually carries the
    side's meaning. Returns ``None`` if nothing on this side produced a confident guess."""
    best_language = None
    best_length = -1
    for name in field_names:
        field = by_name.get(name)
        if field is None:
            continue
        cleaned = [_visible_text(s) for s in field.samples]
        guess = detect_field_language(cleaned)
        if not guess.is_confident:
            continue
        total_length = sum(len(s) for s in cleaned)
        if total_length > best_length:
            best_length = total_length
            best_language = guess.code
    return best_language


def _field_language(field, by_name) -> Optional[str]:
    cleaned = [_visible_text(s) for s in field.samples]
    guess = detect_field_language(cleaned)
    return guess.code if guess.is_confident else None


@dataclass(frozen=True)
class AudioTarget:
    """One real front-content field to speak, and the field its generated audio goes into."""

    source_field: str
    audio_field: str


@dataclass(frozen=True)
class Direction:
    new_front_fields: Tuple[str, ...]
    new_back_fields: Tuple[str, ...]
    target_language: str
    native_language: str
    #: One entry per real (non-generated) field on the new front -- see module docstring.
    audio_targets: Tuple[AudioTarget, ...]


def resolve_direction(
    fields,
    qfmt: str,
    afmt: str,
    *,
    known_state: Optional[ConversionState] = None,
) -> Direction:
    """Everything about direction, computed -- nothing here is a judgment call.

    1. Which fields are currently on the front vs back (``current_sides`` -- pure template
       parsing). Their union is also this deck's *visible* field set (step 3) -- computed
       unconditionally, even when ``known_state`` is given, since that skip only ever applied
       to target/native language (step 2), not to this.
    2. Target/native language at the deck level. Normally: the dominant language of each
       *current* side (the language currently on the back is being promoted (target), the
       language currently on the front is being demoted (native)) -- correct the first time a
       notetype is analyzed, since nothing has been decided yet. But when ``known_state`` is
       given (this notetype was already converted -- see ``core.deck_state``), its recorded
       target/native language is used directly instead: the current front is *already* the
       target language at that point, so re-deriving from current structure would read it
       backwards and flip the deck right back to its original direction (see this module's
       docstring). Using the recorded fact instead makes re-analyzing idempotent -- fields
       already on the target-language side simply match target_language again in step 3 and
       stay put.
    3. Each field is then placed individually by its *own* detected language against that
       target/native pair -- not by relocating its current side wholesale. A field whose
       language doesn't confidently match either (bookkeeping, pure audio, too short to
       detect) falls back to the back, matching the old generator's "never silently drop a
       field" policy -- **provided it was actually shown on the original card at all.** A
       field this addon already created for generated audio (``is_generated_field``), or a
       field that isn't referenced anywhere in the given ``qfmt``/``afmt`` (confirmed against
       a real Core 2000 export: ``Core-Index``, ``Optimized-Voc-Index``, bookkeeping fields
       like it are never in either template, by the original deck author's own design, not by
       omission), is excluded from this placement step entirely -- never shown to the model as
       a front OR back field to place, and never shown to the model's prompt at all (see
       ``analyze.py``, which filters ``PromptInput.fields`` down to exactly what got placed
       here). Reproducing a field's *existing* invisibility isn't "silently dropping" it in
       the sense that policy is about -- the field keeps its data, on the clone, unchanged;
       only whether it renders on either side of the *card* stays exactly as it already was.
       A field referenced only inside a conditional (``{{#Field}}...{{/Field}}``) still counts
       as visible -- ``current_sides``/``referenced_fields`` see through the conditional to the
       field name, exactly as intended, since that field genuinely does render when non-empty.
    4. Every *other* field now on the new front gets its own paired audio target
       (``core.audio_fields.generated_audio_field_name``) -- deterministic and idempotent, so
       re-analyzing an already-converted notetype always proposes the exact same audio field
       names it already has.
    """
    field_list = list(fields)
    by_name = {f.name: f for f in field_list}
    field_names = [f.name for f in field_list]

    current_front, current_back = current_sides(field_names, qfmt, afmt)
    currently_visible = set(current_front) | set(current_back)

    if known_state is not None:
        native_language = known_state.native_language
        target_language = known_state.target_language
    else:
        native_language = _side_language(current_front, by_name) or "?"
        target_language = _side_language(current_back, by_name) or "?"

    new_front: List[str] = []
    new_back: List[str] = []
    for field in field_list:
        if is_generated_field(field.name):
            continue
        if field.name not in currently_visible:
            continue
        language = _field_language(field, by_name)
        if language == target_language:
            new_front.append(field.name)
        else:
            # Matches native_language, or no confident language at all (bookkeeping, pure
            # audio, an image field) -- either way, kept rather than dropped.
            new_back.append(field.name)

    audio_targets = tuple(
        AudioTarget(source_field=name, audio_field=generated_audio_field_name(name))
        for name in new_front
    )

    return Direction(
        new_front_fields=tuple(new_front),
        new_back_fields=tuple(new_back),
        target_language=target_language,
        native_language=native_language,
        audio_targets=audio_targets,
    )
