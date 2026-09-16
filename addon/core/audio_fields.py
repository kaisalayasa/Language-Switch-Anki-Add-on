"""Naming and recognition for the fields this addon creates to hold newly-generated audio.

**Every real target-language field on the new front gets its own audio field** -- not one
shared field for the whole card. A front commonly carries both a single word/term and a full
example sentence, and a learner benefits from hearing both pronounced. This replaced an earlier
design where the model picked a single ``speak_text_from`` field to read aloud: removed once it
became clear there was nothing left to actually choose between -- every field
``llm/direction.py`` places on the new front already passed its "confident, real target-language
content" bar, which is exactly what "worth generating audio for" means anyway. See
``llm/direction.py`` for how each field's paired audio field is decided.

Naming is **per source field**, not a single shared name:
``generated_audio_field_name("Word")`` always returns ``"ddc-audio-Word"``. Because Anki field
names are already unique on a notetype, prefixing by source field name makes every generated
name unique automatically -- unlike the single-field predecessor this replaced, no collision
suffix logic is needed. Calling it again for the same source field (e.g. re-analyzing an
already-converted notetype) always returns the exact same name, which is what makes reusing
rather than re-minting one so simple -- see ``is_generated_field``.

This module names no language and no deck's field names: the one thing it matches on is a name
**it chose itself** (the ``ddc-audio-`` prefix). That is deliberately not the field-name matching
``claude.md`` forbids -- the rule there protects against trusting names that came from a *deck*,
which are untrusted input and routinely lie. A name this addon wrote is its own artifact, and is
the only durable way to recognise the field again after the dialog closes: it is empty until TTS
runs, so no content signal can identify it either.
"""

from __future__ import annotations

__all__ = [
    "AUDIO_DONE_TAG",
    "GENERATED_FIELD_PREFIX",
    "AUDIO_FIELD_PREFIX",
    "generated_audio_field_name",
    "is_generated_field",
]

#: Marks a note as having current, generated audio -- the resumability mechanism for a batch:
#: a re-run only touches notes without it. Stripped off a freshly duplicated note (whose
#: generated audio fields are empty, so it is emphatically not done) by
#: ``ops/notetype_manager.py``.
AUDIO_DONE_TAG = "ddc-tts-generated"

#: Namespace for every field this addon creates, matching the ``ddc`` prefix already used for
#: generated CSS classes and for the tag above.
GENERATED_FIELD_PREFIX = "ddc-"

#: Prefix for every field a conversion creates to hold newly-generated audio. Never used bare on
#: its own (see ``generated_audio_field_name``) -- kept as its own constant because
#: ``is_generated_field`` needs the exact prefix text, and duplicating the literal string in two
#: places is how it would eventually drift.
AUDIO_FIELD_PREFIX = "ddc-audio"


def generated_audio_field_name(source_field_name: str) -> str:
    """The audio field paired with ``source_field_name`` -- always the same name for the same
    source field name, on any notetype, any time this is called. No collision suffixing is
    needed: Anki field names are already unique per notetype, so prefixing by source field name
    makes each generated name unique automatically.
    """
    return "%s-%s" % (AUDIO_FIELD_PREFIX, source_field_name)


def is_generated_field(name: str) -> bool:
    """Whether ``name`` is a field this addon created for generated audio."""
    lowered = str(name or "").strip().lower()
    return lowered == AUDIO_FIELD_PREFIX or lowered.startswith(AUDIO_FIELD_PREFIX + "-")
