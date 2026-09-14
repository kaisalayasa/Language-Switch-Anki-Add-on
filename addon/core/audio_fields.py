"""Where generated audio lives, and what happens to audio the deck already had.

One policy, stated once, so every caller agrees on it:

**The newly-fronted language's audio always goes into a field this addon creates.**
It is never written into a field that was already on the notetype.

Why, concretely -- this replaces an earlier "repurpose the demoted language's audio field"
design that produced a real, reproducible bug:

* A repurposed field arrives at TTS time **already holding the old language's audio**
  (in new-deck mode the duplicate carries it over verbatim). Between the conversion and a
  successful synthesis run there is a window where the template references that field and
  the field still holds the wrong language -- so the card plays the language the conversion
  was supposed to retire. If synthesis fails or is interrupted for a note, that window never
  closes for it.
* A field this addon just created starts **empty**. An empty field renders as nothing, so the
  worst case becomes *silence until the audio is generated*, never *the wrong language*. The
  bug stops being a matter of ordering and becomes structurally impossible.
* The deck's original audio survives instead of being overwritten, which also makes the
  conversion that much less destructive.

The audio a deck already had is not deleted and not left dangling either: it is bound to a
**native** audio role. That matters more than it looks -- the generator only ever emits
target-side audio, *and* it dumps every completely unmapped field onto the back so nothing
is silently lost. An audio field left unmapped would therefore still reach the card through
that dump and still play. Binding it to a native role is what actually hides it.

This module names no language and no deck's field names: the one name it matches on is the
one **it chose itself** (see :data:`GENERATED_FIELD_PREFIX`). That is deliberately not the
field-name matching ``claude.md`` forbids -- the rule there protects against trusting names
that came from a *deck*, which are untrusted input and routinely lie. A name this addon
wrote is its own artifact, exactly like ``template_generator.GENERATED_CSS_MARKER``, and is
the only durable way to recognise the field again after the dialog closes: it is empty until
TTS runs, so no content signal can identify it.
"""

from __future__ import annotations

import re
from typing import Dict, Iterable, List, Optional, Sequence, Set, Tuple

from .role_schema import AUDIO_SOURCE_ROLES, FieldBinding, Role, RoleMapping

__all__ = [
    "AUDIO_DONE_TAG",
    "GENERATED_FIELD_PREFIX",
    "DEMOTED_TO",
    "SOUND_TAG_RE",
    "generated_field_name",
    "generated_field_role",
    "is_generated_field",
    "field_has_sound",
    "sound_field_names",
    "needed_audio_roles",
    "plan_generated_fields",
    "resolve_audio_fields",
]

#: Marks a note as having current, generated audio -- the resumability mechanism for a
#: batch: a re-run only touches notes without it. Lives here, with the rest of the audio
#: policy, because two layers need it and neither may import the other: the TTS batch sets
#: it, and the conversion has to strip it off a freshly duplicated note (whose generated
#: audio field is empty, so it is emphatically not done).
AUDIO_DONE_TAG = "ddc-tts-generated"

#: Namespace for every field this addon creates, matching the ``ddc`` prefix already used
#: for generated CSS classes and for the tag above. A generated field is named ``<stem>`` or
#: ``<stem> (LANG)`` -- e.g. ``ddc-audio (EN)``.
GENERATED_FIELD_PREFIX = "ddc-"

_ROLE_STEM: Dict[Role, str] = {
    Role.TARGET_AUDIO: "ddc-audio",
    Role.TARGET_SENTENCE_AUDIO: "ddc-audio-sentence",
}

#: Longest stem first, so the sentence stem is never read as the word stem plus noise.
#: Belt and braces: :func:`generated_field_role` also requires a space after the stem, so
#: ``ddc-audio-sentence`` cannot match ``ddc-audio`` however this list is ordered.
_STEMS_LONGEST_FIRST: List[Tuple[Role, str]] = sorted(
    _ROLE_STEM.items(), key=lambda kv: -len(kv[1])
)

#: The native-side counterpart of each target audio role. Audio the deck already had is
#: demoted along this map: still bound (so the generator's unmapped-field dump can't leak it
#: onto the back) but never emitted, since the generator only ever renders target audio.
DEMOTED_TO: Dict[Role, Role] = {
    Role.TARGET_AUDIO: Role.NATIVE_AUDIO,
    Role.TARGET_SENTENCE_AUDIO: Role.NATIVE_SENTENCE_AUDIO,
}

_TARGET_AUDIO_ROLES: Tuple[Role, ...] = tuple(DEMOTED_TO)
_NATIVE_AUDIO_ROLES: Tuple[Role, ...] = tuple(DEMOTED_TO.values())
_ALL_AUDIO_ROLES: Tuple[Role, ...] = _TARGET_AUDIO_ROLES + _NATIVE_AUDIO_ROLES

SOUND_TAG_RE = re.compile(r"\[sound:[^\]]*\]")


# ---------------------------------------------------------------------------
# naming and recognition
# ---------------------------------------------------------------------------


def generated_field_name(
    role: Role, *, language: Optional[str] = None, taken: Iterable[str] = ()
) -> str:
    """A field name for ``role``, unique against ``taken``.

    ``language`` is a free-form label (whatever the mapping carries, typically a short code)
    shown in the name purely so a human can tell at a glance what the field holds. It plays
    no part in recognition -- :func:`generated_field_role` keys off the stem alone, so
    changing the declared language later never orphans an existing field.
    """
    if role not in _ROLE_STEM:
        raise KeyError("no generated field is defined for role %s" % role.key)
    stem = _ROLE_STEM[role]
    label = (language or "").strip()
    base = "%s (%s)" % (stem, label.upper()) if label else stem

    existing = {str(n).strip().lower() for n in taken}
    if base.lower() not in existing:
        return base
    suffix = 2
    while ("%s %d" % (base, suffix)).lower() in existing:
        suffix += 1
    return "%s %d" % (base, suffix)


def generated_field_role(name: str) -> Optional[Role]:
    """Which target audio role ``name`` was generated for, or ``None`` if it wasn't ours."""
    lowered = str(name or "").strip().lower()
    for role, stem in _STEMS_LONGEST_FIRST:
        if lowered == stem or lowered.startswith(stem + " "):
            return role
    return None


def is_generated_field(name: str) -> bool:
    return generated_field_role(name) is not None


# ---------------------------------------------------------------------------
# content signal
# ---------------------------------------------------------------------------


def field_has_sound(samples: Sequence[str]) -> bool:
    """Whether any sample for one field carries an ``[sound:...]`` reference."""
    return any(SOUND_TAG_RE.search(s or "") for s in samples)


def sound_field_names(samples_by_field: Dict[str, Sequence[str]]) -> Set[str]:
    """Every field name whose sampled content contains audio."""
    return {name for name, values in samples_by_field.items() if field_has_sound(values)}


# ---------------------------------------------------------------------------
# policy
# ---------------------------------------------------------------------------


def needed_audio_roles(mapping: RoleMapping) -> List[Role]:
    """Which target audio roles this mapping can actually produce speech for.

    Driven entirely by what there is to *say*: an audio role is wanted exactly when the
    content role it is synthesized from (:data:`role_schema.AUDIO_SOURCE_ROLES`) has a field
    bound. A deck with no sentence field gets no field for sentence audio.
    """
    return [
        audio_role
        for audio_role, source_role in AUDIO_SOURCE_ROLES.items()
        if mapping.has(source_role)
    ]


def plan_generated_fields(mapping: RoleMapping) -> List[Tuple[Role, str]]:
    """``[(role, new field name), ...]`` this mapping still needs created.

    Idempotent: a role whose generated field is already on the notetype is skipped, so
    converting an already-converted deck a second time adds nothing.
    """
    already = {generated_field_role(f.name) for f in mapping.fields}
    taken = [f.name for f in mapping.fields]

    planned: List[Tuple[Role, str]] = []
    for role in needed_audio_roles(mapping):
        if role in already:
            continue
        name = generated_field_name(role, language=mapping.target_language, taken=taken)
        taken.append(name)
        planned.append((role, name))
    return planned


def resolve_audio_fields(
    mapping: RoleMapping,
    *,
    sound_fields: Iterable[str] = (),
    extra_fields: Sequence[Tuple[Role, str]] = (),
) -> RoleMapping:
    """Apply this module's policy to ``mapping``, returning a new mapping.

    Three rules, in this order:

    1. Fields **this addon generated** are bound to the target audio roles. They are the only
       fields TTS ever writes to.
    2. Any *other* field bound to a target audio role is demoted to the matching native role
       -- kept, never rendered, never overwritten. This is what makes a profile that predates
       this policy (or a hand mapping) do the right thing without being rewritten.
    3. Any still-unmapped field in ``sound_fields`` is bound to a native audio role as well,
       so it cannot reach the card through the generator's unmapped-field dump.

    ``extra_fields`` declares generated fields that will exist on the clone but are not on
    the notetype yet -- how a conversion generates templates that reference a field it is
    creating in the same operation. They are appended after the existing fields, which is
    also where :func:`~addon.ops.notetype_manager.shape_notetype` puts them.

    The input mapping is never mutated: ``FieldBinding`` is frozen and callers hold on to
    their own mapping (the UI re-reads it constantly), so this builds a fresh one.
    """
    fields = list(mapping.fields)
    next_ord = max((f.ord for f in fields), default=-1) + 1
    for _role, name in extra_fields:
        fields.append(FieldBinding(name=name, ord=next_ord))
        next_ord += 1

    resolved = RoleMapping(
        notetype_name=mapping.notetype_name,
        fields=fields,
        target_language=mapping.target_language,
        native_language=mapping.native_language,
        render_order=list(mapping.render_order) if mapping.render_order else None,
    )

    # Everything that isn't audio passes through untouched.
    for role, bindings in mapping.assignments.items():
        if role in _ALL_AUDIO_ROLES:
            continue
        for binding in bindings:
            resolved.bind(role, binding)

    # Rules 2 and 3: collect what becomes native audio, keeping the word/sentence
    # distinction the incoming mapping already drew, and never binding one field twice.
    demoted: List[Tuple[Role, FieldBinding]] = []
    seen: Set[str] = set()

    def keep_as_native(native_role: Role, binding: FieldBinding) -> None:
        key = binding.name.strip().lower()
        if key in seen:
            return
        seen.add(key)
        demoted.append((native_role, binding))

    for role in _TARGET_AUDIO_ROLES:
        for binding in mapping.get(role):
            if generated_field_role(binding.name) is None:
                keep_as_native(DEMOTED_TO[role], binding)
    for role in _NATIVE_AUDIO_ROLES:
        for binding in mapping.get(role):
            keep_as_native(role, binding)

    loose = {str(n).strip().lower() for n in sound_fields}
    for binding in sorted(fields, key=lambda f: f.ord):
        if binding.name.strip().lower() not in loose:
            continue
        if generated_field_role(binding.name) is not None:
            continue  # our own field, once TTS has filled it -- rule 1 owns this one
        if resolved.roles_for(binding.name):
            continue  # already carries a real role; not ours to reassign
        keep_as_native(Role.NATIVE_AUDIO, binding)

    for native_role, binding in demoted:
        resolved.bind(native_role, binding)

    # Rule 1, last so it wins outright: generated fields take the target audio roles.
    for binding in sorted(fields, key=lambda f: f.ord):
        role = generated_field_role(binding.name)
        if role is not None and resolved.first(role) is None:
            resolved.bind(role, binding)

    return resolved
