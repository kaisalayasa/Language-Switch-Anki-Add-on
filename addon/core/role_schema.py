"""Role definitions and validation for deck direction conversion.

This module is deliberately language-agnostic and Anki-agnostic:

* It imports nothing from ``anki`` or ``aqt`` and can be unit-tested with stock Python.
* It contains no language name, no script name, and no deck-specific field name.
  Those live only in profile JSON. See ``claude.md`` -> "Field names are untrusted input".

Terminology (structural, not tied to any language pair):

``TARGET``
    The language being studied -- the one that ends up on the **front** after conversion.
``NATIVE``
    The language the learner already knows -- ends up on the **back**.

Which real-world language fills each side is supplied by the caller; nothing here knows
or cares.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Dict, Iterable, List, Optional, Sequence, Tuple

__all__ = [
    "Side",
    "Kind",
    "Role",
    "FieldBinding",
    "RoleMapping",
    "ValidationError",
    "ValidationResult",
    "FieldAssignment",
    "mapping_from_assignments",
    "assignments_from_mapping",
    "AUDIO_SOURCE_ROLES",
]


class Side(Enum):
    """Which half of the card a role belongs to."""

    TARGET = "target"
    NATIVE = "native"
    NEUTRAL = "neutral"


class Kind(Enum):
    """What sort of content a role carries.

    Every member needs a distinct value. Two Enum members that compare equal become
    *aliases* of one another in Python, which would silently collapse distinct roles into
    one -- so the meta kinds are enumerated individually rather than sharing a single
    ``META``.
    """

    # Content kinds -- available on both sides.
    TERM = "term"
    READING = "reading"
    SENTENCE = "sentence"
    AUDIO = "audio"
    SENTENCE_AUDIO = "sentence_audio"

    # Meta kinds -- side-neutral.
    PART_OF_SPEECH = "part_of_speech"
    NOTE = "note"
    CLOZE = "cloze"
    IMAGE = "image"

    @property
    def is_meta(self) -> bool:
        return self in (Kind.PART_OF_SPEECH, Kind.NOTE, Kind.CLOZE, Kind.IMAGE)


class Role(Enum):
    """A semantic slot a field may be assigned to.

    Roles are symmetric across both sides: the native side can need readings and audio
    just as much as the target side. Membership is ``(side, kind)``.
    """

    TARGET_TERM = (Side.TARGET, Kind.TERM)
    TARGET_READING = (Side.TARGET, Kind.READING)
    TARGET_SENTENCE = (Side.TARGET, Kind.SENTENCE)
    TARGET_AUDIO = (Side.TARGET, Kind.AUDIO)
    TARGET_SENTENCE_AUDIO = (Side.TARGET, Kind.SENTENCE_AUDIO)

    NATIVE_TERM = (Side.NATIVE, Kind.TERM)
    NATIVE_READING = (Side.NATIVE, Kind.READING)
    NATIVE_SENTENCE = (Side.NATIVE, Kind.SENTENCE)
    NATIVE_AUDIO = (Side.NATIVE, Kind.AUDIO)
    NATIVE_SENTENCE_AUDIO = (Side.NATIVE, Kind.SENTENCE_AUDIO)

    POS = (Side.NEUTRAL, Kind.PART_OF_SPEECH)
    NOTES = (Side.NEUTRAL, Kind.NOTE)
    CLOZE_TEXT = (Side.NEUTRAL, Kind.CLOZE)
    IMAGE = (Side.NEUTRAL, Kind.IMAGE)

    @property
    def side(self) -> Side:
        return self.value[0]

    @property
    def kind(self) -> Kind:
        return self.value[1]

    @property
    def is_audio(self) -> bool:
        return self.kind in (Kind.AUDIO, Kind.SENTENCE_AUDIO)

    @classmethod
    def from_key(cls, key: str) -> "Role":
        """Look up a role by its profile-JSON key, e.g. ``"TargetTerm"``.

        Accepts ``TargetTerm``, ``TARGET_TERM`` and ``target_term`` alike so hand-written
        profiles are forgiving.
        """
        normalised = key.replace("-", "_").replace(" ", "_").upper()
        if normalised in cls.__members__:
            return cls.__members__[normalised]
        # CamelCase -> UPPER_SNAKE
        snake = ""
        for i, ch in enumerate(key):
            if ch.isupper() and i and not key[i - 1].isupper():
                snake += "_"
            snake += ch
        snake = snake.replace("-", "_").replace(" ", "_").upper()
        if snake in cls.__members__:
            return cls.__members__[snake]
        raise KeyError(f"unknown role {key!r}")

    @property
    def key(self) -> str:
        """The CamelCase key used in profile JSON."""
        return "".join(part.capitalize() for part in self.name.split("_"))


#: Which content role each audio role's speech should be synthesized from (M5). Only the
#: target side gets TTS -- per claude.md's "audio is a replacement, not an addition", the
#: native side's audio field is either left alone or dropped, never (re)synthesized here.
AUDIO_SOURCE_ROLES: Dict[Role, Role] = {
    Role.TARGET_AUDIO: Role.TARGET_TERM,
    Role.TARGET_SENTENCE_AUDIO: Role.TARGET_SENTENCE,
}


@dataclass(frozen=True)
class FieldBinding:
    """A single note field, bound to a role.

    ``name`` and ``ord`` are both recorded so a mapping can be re-validated against a live
    notetype. If they disagree we refuse rather than silently misaligning fields.

    ``filter`` is an *Anki template filter* name to wrap the reference in -- the generator
    emits ``{{filter:Name}}`` instead of ``{{Name}}``. It is stored as an opaque string
    that the profile chooses; this module never interprets it, never validates it against
    a list, and has no opinion about which filters exist. Anki ships a number of them, and
    addons add more.

    ``hidden`` excludes the field from the automatic unmapped-field dump -- useful for
    bookkeeping columns like indices and frequency ranks that would be noise on a card.

    ``css_class`` is an extra class name to put on this field's wrapper, so a profile can
    reuse a class the source notetype's own CSS already defines. Opaque string; the
    generator emits it and never interprets it.
    """

    name: str
    ord: int
    filter: Optional[str] = None
    hidden: bool = False
    css_class: Optional[str] = None

    def reference(self) -> str:
        """The Anki template reference for this field."""
        return "{{%s:%s}}" % (self.filter, self.name) if self.filter else "{{%s}}" % self.name

    def conditional_open(self) -> str:
        return "{{#%s}}" % self.name

    def conditional_close(self) -> str:
        return "{{/%s}}" % self.name


class ValidationError(Exception):
    """Raised when a mapping cannot safely be applied to a notetype."""


@dataclass
class ValidationResult:
    errors: List[str] = field(default_factory=list)
    warnings: List[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.errors

    def raise_if_failed(self) -> None:
        if self.errors:
            raise ValidationError("; ".join(self.errors))


@dataclass
class RoleMapping:
    """An assignment of note fields to roles, for one notetype.

    A field may carry more than one role, and a role may be backed by more than one field
    (e.g. a plain term field plus a ruby-annotated display variant). Both directions are
    many-to-many on purpose.
    """

    notetype_name: str
    #: Every field on the notetype, in ord order. The source of truth for validation.
    fields: List[FieldBinding]
    assignments: Dict[Role, List[FieldBinding]] = field(default_factory=dict)
    #: Free-form labels for the two languages. Opaque to this module -- used only for
    #: display and to let the TTS layer pick a voice.
    target_language: Optional[str] = None
    native_language: Optional[str] = None

    # -- lookup -------------------------------------------------------------

    def get(self, role: Role) -> List[FieldBinding]:
        """All fields bound to ``role`` (possibly empty)."""
        return list(self.assignments.get(role, ()))

    def first(self, role: Role) -> Optional[FieldBinding]:
        """The primary field for ``role``, or ``None`` if unassigned."""
        bound = self.assignments.get(role)
        return bound[0] if bound else None

    def has(self, *roles: Role) -> bool:
        """True if *any* of ``roles`` has at least one field bound."""
        return any(self.assignments.get(r) for r in roles)

    def roles_for(self, field_name: str) -> List[Role]:
        return [r for r, bs in self.assignments.items() if any(b.name == field_name for b in bs)]

    def assigned_field_names(self) -> List[str]:
        seen: List[str] = []
        for bindings in self.assignments.values():
            for b in bindings:
                if b.name not in seen:
                    seen.append(b.name)
        return seen

    def unmapped(self, *, include_hidden: bool = False) -> List[FieldBinding]:
        """Fields carrying no role, in ord order.

        These still render on the back so no data is silently hidden, unless flagged
        ``hidden`` in the profile.
        """
        assigned = set(self.assigned_field_names())
        return [
            f
            for f in sorted(self.fields, key=lambda f: f.ord)
            if f.name not in assigned and (include_hidden or not f.hidden)
        ]

    def bind(self, role: Role, binding: FieldBinding) -> None:
        self.assignments.setdefault(role, []).append(binding)

    # -- validation ---------------------------------------------------------

    def validate(self) -> ValidationResult:
        """Check the mapping is internally coherent and renderable.

        This does not touch Anki; it only checks the mapping against its own field list.
        Use :meth:`validate_against` to additionally check a live notetype.
        """
        result = ValidationResult()
        by_name = {f.name: f for f in self.fields}

        if not self.fields:
            result.errors.append("notetype has no fields")

        dupes = [n for n in by_name if sum(1 for f in self.fields if f.name == n) > 1]
        if dupes:
            result.errors.append("duplicate field names: %s" % ", ".join(sorted(set(dupes))))

        for role, bindings in self.assignments.items():
            if not bindings:
                continue
            for b in bindings:
                known = by_name.get(b.name)
                if known is None:
                    result.errors.append(
                        "role %s is bound to field %r, which is not on this notetype"
                        % (role.key, b.name)
                    )
                elif known.ord != b.ord:
                    result.errors.append(
                        "role %s binding for %r has ord %d but the notetype says %d"
                        % (role.key, b.name, b.ord, known.ord)
                    )

        # A bilingual flip needs something to ask and something to answer with.
        if not self.has(Role.TARGET_TERM, Role.TARGET_SENTENCE):
            result.errors.append(
                "no target-side content assigned: the card front would be empty "
                "(assign at least TargetTerm or TargetSentence)"
            )
        if not self.has(Role.NATIVE_TERM, Role.NATIVE_SENTENCE):
            result.errors.append(
                "no native-side content assigned: the card back would be empty "
                "(assign at least NativeTerm or NativeSentence)"
            )

        if self.has(Role.TARGET_READING) and not self.has(Role.TARGET_TERM):
            result.warnings.append("TargetReading is assigned but TargetTerm is not")
        if self.has(Role.NATIVE_READING) and not self.has(Role.NATIVE_TERM):
            result.warnings.append("NativeReading is assigned but NativeTerm is not")

        return result

    def validate_against(self, live_fields: Sequence[Tuple[int, str]]) -> ValidationResult:
        """Re-check this mapping against a notetype as it exists right now.

        ``live_fields`` is ``[(ord, name), ...]`` read from the actual notetype. Passed in
        as plain tuples so this module stays free of Anki imports.

        Resolution order is name-first, ord-as-fallback. If the two disagree we record an
        error rather than guessing -- a silently misaligned field map would write the
        wrong content into every note.
        """
        result = self.validate()
        live_by_name = {name: ord_ for ord_, name in live_fields}
        live_by_ord = {ord_: name for ord_, name in live_fields}

        for binding in self.fields:
            live_ord = live_by_name.get(binding.name)
            if live_ord is None:
                at_ord = live_by_ord.get(binding.ord)
                if at_ord is None:
                    result.errors.append(
                        "field %r (ord %d) is missing from the notetype"
                        % (binding.name, binding.ord)
                    )
                else:
                    result.errors.append(
                        "field %r is missing from the notetype; ord %d now holds %r "
                        "-- refusing to guess" % (binding.name, binding.ord, at_ord)
                    )
            elif live_ord != binding.ord:
                result.errors.append(
                    "field %r moved from ord %d to ord %d -- refusing to guess"
                    % (binding.name, binding.ord, live_ord)
                )

        return result

    # -- serialisation ------------------------------------------------------

    @classmethod
    def from_profile(cls, data: dict, *, live_fields: Optional[Sequence[Tuple[int, str]]] = None) -> "RoleMapping":
        """Build a mapping from profile JSON.

        Expected shape::

            {
              "notetype": "...",
              "target_language": "...",
              "native_language": "...",
              "fields": [{"name": "...", "ord": 0, "filter": null, "hidden": false}, ...],
              "roles": {"TargetTerm": ["Some Field"], ...}
            }

        ``live_fields``, when given, replaces the profile's declared field list with what
        the notetype actually has -- so ``validate_against`` can compare the two.
        """
        declared = [
            FieldBinding(
                name=f["name"],
                ord=int(f["ord"]),
                filter=f.get("filter") or None,
                hidden=bool(f.get("hidden", False)),
                css_class=f.get("css_class") or None,
            )
            for f in data.get("fields", [])
        ]
        mapping = cls(
            notetype_name=data.get("notetype", ""),
            fields=declared,
            target_language=data.get("target_language"),
            native_language=data.get("native_language"),
        )
        by_name = {f.name: f for f in declared}
        for role_key, field_names in (data.get("roles") or {}).items():
            role = Role.from_key(role_key)
            if isinstance(field_names, str):
                field_names = [field_names]
            for name in field_names:
                binding = by_name.get(name)
                if binding is None:
                    # Keep it; validate() will report it as an error with context.
                    binding = FieldBinding(name=name, ord=-1)
                mapping.bind(role, binding)
        if live_fields is not None:
            mapping.fields = cls._merge_live(declared, live_fields)
        return mapping

    @staticmethod
    def _merge_live(
        declared: Sequence[FieldBinding], live_fields: Sequence[Tuple[int, str]]
    ) -> List[FieldBinding]:
        """Keep the profile's render hints while adopting the notetype's real field list."""
        hints = {f.name: f for f in declared}
        merged: List[FieldBinding] = []
        for ord_, name in sorted(live_fields):
            hint = hints.get(name)
            merged.append(
                FieldBinding(
                    name=name,
                    ord=ord_,
                    filter=hint.filter if hint else None,
                    hidden=hint.hidden if hint else False,
                    css_class=hint.css_class if hint else None,
                )
            )
        return merged

    def to_profile(self) -> dict:
        return {
            "notetype": self.notetype_name,
            "target_language": self.target_language,
            "native_language": self.native_language,
            "fields": [
                {
                    "name": f.name,
                    "ord": f.ord,
                    "filter": f.filter,
                    "hidden": f.hidden,
                    "css_class": f.css_class,
                }
                for f in sorted(self.fields, key=lambda f: f.ord)
            ],
            "roles": {
                role.key: [b.name for b in bindings]
                for role, bindings in sorted(self.assignments.items(), key=lambda kv: kv[0].name)
                if bindings
            },
        }


def fields_from_notetype(flds: Iterable[dict]) -> List[Tuple[int, str]]:
    """Extract ``[(ord, name), ...]`` from an Anki notetype dict's ``flds``.

    Lives here rather than in the Anki-facing layer so callers can hand this module a
    plain list without importing anki themselves. Takes dicts, not Anki objects.
    """
    return sorted((int(f["ord"]), str(f["name"])) for f in flds)


@dataclass(frozen=True)
class FieldAssignment:
    """One row of a manual, one-role-per-field mapping UI (M3's ``RoleMapperDialog``).

    A flatter, UI-shaped view of a mapping than :class:`RoleMapping` itself: at most one
    ``role`` per field, rather than the data model's full many-to-many. ``filter`` and
    ``css_class`` are carried through unedited from wherever the row was seeded (a shipped
    profile, typically) -- M3's UI has no control that sets them, only ones that might
    preserve or drop them.
    """

    name: str
    ord: int
    role: Optional[Role] = None
    hidden: bool = False
    filter: Optional[str] = None
    css_class: Optional[str] = None


def mapping_from_assignments(
    notetype_name: str,
    assignments: Sequence[FieldAssignment],
    *,
    target_language: Optional[str] = None,
    native_language: Optional[str] = None,
) -> RoleMapping:
    """Rebuild a :class:`RoleMapping` from flat UI rows.

    Builds fresh rather than mutating an existing mapping's bindings in place --
    :class:`FieldBinding` is ``frozen`` on purpose, and rebuilding from what the table
    actually shows is simpler than trying to patch a mapping incrementally as checkboxes
    and dropdowns change.
    """
    fields = [
        FieldBinding(
            name=a.name, ord=a.ord, filter=a.filter, hidden=a.hidden, css_class=a.css_class
        )
        for a in assignments
    ]
    mapping = RoleMapping(
        notetype_name=notetype_name,
        fields=fields,
        target_language=target_language,
        native_language=native_language,
    )
    by_name = {f.name: f for f in fields}
    for a in assignments:
        if a.role is not None:
            mapping.bind(a.role, by_name[a.name])
    return mapping


def assignments_from_mapping(mapping: RoleMapping) -> List[FieldAssignment]:
    """The reverse of :func:`mapping_from_assignments` -- seeds a UI table from a mapping.

    If a field genuinely carries more than one role (the data model allows it; no shipped
    profile currently uses it), the first one wins here -- a documented limitation of the
    one-role-per-field UI, not data loss, since a mapping built from the table is always
    rebuilt from what the table shows, never patched onto the original.
    """
    role_for: Dict[str, Role] = {}
    for role, bindings in mapping.assignments.items():
        for b in bindings:
            role_for.setdefault(b.name, role)
    return [
        FieldAssignment(
            name=f.name,
            ord=f.ord,
            role=role_for.get(f.name),
            hidden=f.hidden,
            filter=f.filter,
            css_class=f.css_class,
        )
        for f in sorted(mapping.fields, key=lambda f: f.ord)
    ]
