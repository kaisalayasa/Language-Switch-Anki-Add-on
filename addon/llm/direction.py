"""Deterministic resolution of direction, target/native language, and field placement.

Why the model doesn't decide this any more: across every real test run today (see
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
to writing HTML for a placement it is given, and choosing which of the given target-side fields
is best to speak aloud.

Placement is decided **per field**, not by relocating each current side as a whole block, which
matters for a deck like Core 2000: its current "back" (the answer reveal) mixes English
translation together with Japanese reading aids (furigana) shown alongside it. Swapping that
whole group onto the new front would incorrectly drag the Japanese reading aids over with the
English content. Instead, each field's own detected language decides which new side it belongs
on; only the *side-level* majority language (used to decide which language counts as target vs
native at all) comes from ``current_sides``.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import List, Optional, Sequence, Tuple

from ..core.language_detect import detect_field_language
from .audio_safety import SOUND_TAG_RE
from .template_fields import current_sides

__all__ = ["Direction", "resolve_direction"]

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
class Direction:
    new_front_fields: Tuple[str, ...]
    new_back_fields: Tuple[str, ...]
    target_language: str
    native_language: str


def resolve_direction(fields, qfmt: str, afmt: str, *, audio_field_name: str) -> Direction:
    """Everything about direction, computed -- nothing here is a judgment call.

    1. Which fields are currently on the front vs back (``current_sides`` -- pure template
       parsing).
    2. The dominant language of each *current* side decides target/native language at the deck
       level: the language currently on the back is being promoted (target), the language
       currently on the front is being demoted (native).
    3. Each field is then placed individually by its *own* detected language against that
       target/native pair -- not by relocating its current side wholesale. A field whose
       language doesn't confidently match either (bookkeeping, pure audio, too short to
       detect) falls back to the back, matching the old generator's "never silently drop a
       field" policy. The field reserved for new audio always goes to the front -- it's the
       whole point of the conversion.
    """
    field_list = list(fields)
    by_name = {f.name: f for f in field_list}
    field_names = [f.name for f in field_list]

    current_front, current_back = current_sides(field_names, qfmt, afmt)
    native_language = _side_language(current_front, by_name) or "?"
    target_language = _side_language(current_back, by_name) or "?"

    new_front: List[str] = []
    new_back: List[str] = []
    for field in field_list:
        if field.name == audio_field_name:
            new_front.append(field.name)
            continue
        language = _field_language(field, by_name)
        if language == target_language:
            new_front.append(field.name)
        else:
            # Matches native_language, or no confident language at all (bookkeeping, pure
            # audio, an image field) -- either way, kept rather than dropped.
            new_back.append(field.name)

    return Direction(
        new_front_fields=tuple(new_front),
        new_back_fields=tuple(new_back),
        target_language=target_language,
        native_language=native_language,
    )
