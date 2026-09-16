"""Deterministic enforcement that a deck's pre-existing audio can never play on a converted
card -- applied to the model's generated templates *after* the fact, regardless of what the
model actually wrote.

Why this exists rather than trusting the model to get it right: the first real test of the
prompt (a deck with a field mixing text and embedded audio, `"stellen [sound:...mp3]"`) showed
the model referencing that field as a bare `{{Front}}` instead of `{{text:Front}}` -- which would
play the deck's own, original-direction audio on the newly-converted card. That is precisely the
failure this project's audio policy exists to prevent (see the old `core/audio_fields.py`'s
`TestTheCardNeverPlaysTheOldAudio`), and it is not something worth re-litigating with a smarter
prompt: whether a field's audio should ever play is a fact we can determine deterministically from
its own sample content, so there is no judgment call to hand to a 1.5B model at all.

The rule: every field whose real content contains `[sound:...]` gets every bare or
differently-filtered reference to it forced to `{{text:Field}}` -- which renders the field's text
while stripping any embedded audio reference. Conditional guards (`{{#Field}}...{{/Field}}`,
`{{^Field}}...{{/Field}}`) are left alone; they are presence checks, not renders, and touch
nothing themselves.

No exemption is needed for the new fields a conversion itself creates for generated audio: the
model is never told those names exist at all any more (``llm/direction.py`` computes and appends
their references itself, after this function and validation have both already run -- see
``analyze.py``), so there is nothing for the model to legitimately bare-reference here. If a
response ever did hallucinate a reference to one, this function neutralizing it is the correct,
safe outcome, not a false positive.
"""

from __future__ import annotations

import re
from typing import Iterable, Sequence

from .template_fields import content_reference_pattern

__all__ = ["SOUND_TAG_RE", "field_has_sound", "sound_field_names", "enforce_audio_safety"]

SOUND_TAG_RE = re.compile(r"\[sound:[^\]]*\]")

#: The filter this module forces onto any bare/differently-filtered reference to a field with
#: real pre-existing audio. Anki's own built-in "strip special references and HTML" modifier.
_TEXT_FILTER = "text"


def field_has_sound(samples: Sequence[str]) -> bool:
    """Whether any sample for one field carries a ``[sound:...]`` reference."""
    return any(SOUND_TAG_RE.search(s or "") for s in samples)


def sound_field_names(fields) -> frozenset:
    """Every field name (from a sequence of ``llm.prompt.FieldSample``) whose real sample
    content contains audio."""
    return frozenset(f.name for f in fields if field_has_sound(f.samples))


def enforce_audio_safety(html: str, *, sound_fields: Iterable[str]) -> str:
    """Rewrite ``html`` so every field in ``sound_fields`` can only ever be referenced via
    ``{{text:Field}}``, never played as audio.

    Applied to both the Front and Back template HTML; a leak can happen on either side.

    Idempotent: a reference already written as ``{{text:Field}}`` matches the same pattern and is
    replaced with itself, not double-wrapped.
    """
    out = html
    for name in sound_fields:
        pattern = content_reference_pattern(name)
        out = pattern.sub("{{%s:%s}}" % (_TEXT_FILTER, name), out)
    return out
