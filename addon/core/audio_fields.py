"""Naming and recognition for the one field this addon creates to hold newly-generated audio.

**The newly-fronted language's audio always goes into a field this addon creates.** It is
never written into a field that was already on the notetype -- see ``addon/llm/prompt.py``'s
hard rules 3-5 and ``addon/llm/audio_safety.py`` for how the model (and a deterministic
backstop) are kept from ever referencing the deck's own pre-existing audio as anything but
plain text. A field this addon just created starts **empty**, so the worst case if TTS hasn't
run yet is silence, never the wrong language playing.

This module names no language and no deck's field names: the one name it matches on is the one
**it chose itself** (:data:`GENERATED_AUDIO_FIELD`). That is deliberately not the field-name
matching ``claude.md`` forbids -- the rule there protects against trusting names that came from
a *deck*, which are untrusted input and routinely lie. A name this addon wrote is its own
artifact, and is the only durable way to recognise the field again after the dialog closes: it
is empty until TTS runs, so no content signal can identify it either.

Everything about *which* field on a notetype should hold which role -- the policy funnel this
module used to own (``resolve_audio_fields``, ``plan_generated_fields``, role demotion) -- is
gone along with the role-mapping system it existed for. The model now places the empty audio
field directly in the templates it writes (see ``PromptInput.audio_field_name``); this module's
only remaining job is naming that field consistently and recognising it later.
"""

from __future__ import annotations

from typing import Iterable

__all__ = [
    "AUDIO_DONE_TAG",
    "GENERATED_FIELD_PREFIX",
    "GENERATED_AUDIO_FIELD",
    "generated_field_name",
    "is_generated_field",
]

#: Marks a note as having current, generated audio -- the resumability mechanism for a batch:
#: a re-run only touches notes without it. Stripped off a freshly duplicated note (whose
#: generated audio field is empty, so it is emphatically not done) by
#: ``ops/notetype_manager.py``.
AUDIO_DONE_TAG = "ddc-tts-generated"

#: Namespace for every field this addon creates, matching the ``ddc`` prefix already used for
#: generated CSS classes and for the tag above.
GENERATED_FIELD_PREFIX = "ddc-"

#: The one field a conversion creates to hold newly-generated audio. A fixed name rather than
#: a per-conversion label (the old design's ``ddc-audio (EN)`` scheme) -- the model is told
#: this exact name as a given fact (see ``llm.prompt.PromptInput.audio_field_name``), so it
#: must be a constant both sides agree on, not something computed per notetype.
GENERATED_AUDIO_FIELD = "ddc-audio"


def generated_field_name(taken: Iterable[str] = ()) -> str:
    """:data:`GENERATED_AUDIO_FIELD`, made unique against ``taken`` if it's already in use.

    Only matters if a notetype somehow already has a field with this exact name (e.g. a
    second conversion of an already-converted notetype) -- appends a numeric suffix rather
    than colliding with it.
    """
    existing = {str(n).strip().lower() for n in taken}
    base = GENERATED_AUDIO_FIELD
    if base.lower() not in existing:
        return base
    suffix = 2
    while ("%s %d" % (base, suffix)).lower() in existing:
        suffix += 1
    return "%s %d" % (base, suffix)


def is_generated_field(name: str) -> bool:
    """Whether ``name`` is a field this addon created for generated audio."""
    lowered = str(name or "").strip().lower()
    return lowered == GENERATED_AUDIO_FIELD or lowered.startswith(GENERATED_AUDIO_FIELD + " ")
