"""Turn a role mapping into Anki card templates.

Pure string generation. This module imports nothing from ``anki``/``aqt`` and can be
exercised with stock Python, which is what makes it cheap to test.

It also contains **no language name, no script name and no deck-specific field name** --
see ``claude.md`` -> "Field names are untrusted input". Field names arrive as opaque
strings inside :class:`~addon.core.role_schema.FieldBinding`; anything language-specific
(which Anki filter to apply, which CSS class to reuse) is carried on the binding and set
by the profile.

Layout policy
-------------
Front (the prompt, in the language being studied)::

    TargetTerm  [+ TargetReading]
    POS
    TargetSentence
    [TargetAudio, TargetSentenceAudio]      -- only when ``audio_on_front``

Back (the answer, in the language already known)::

    {{FrontSide}}
    <hr id=answer>
    NativeTerm  [+ NativeReading]
    NativeSentence
    Image
    Notes
    [unmapped fields]                       -- only when ``include_unmapped``

Roles with nothing bound emit nothing at all: there is never a dangling ``{{Field}}``.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field as _dc_field
from typing import List, Optional, Sequence, Tuple

from .role_schema import FieldBinding, Role, RoleMapping

__all__ = [
    "TemplateOptions",
    "GeneratedTemplates",
    "generate_templates",
    "split_render_order",
    "GENERATED_CSS_MARKER",
]

#: Sentinel so a regenerated template can replace only our appended CSS block and leave
#: the user's own styling alone.
GENERATED_CSS_MARKER = "/* --- deck-direction-converter: generated styles --- */"

#: Class prefix for everything this module emits. Namespaced to avoid colliding with
#: whatever the source notetype's CSS already defines.
_PREFIX = "ddc"


@dataclass
class TemplateOptions:
    """Knobs for :func:`generate_templates`."""

    #: Render fields that carry no role, on the back, unstyled -- so nothing is hidden
    #: without the profile having said so. Fields flagged ``hidden`` are still skipped.
    include_unmapped: bool = True
    #: Reference the target-side audio fields at all. **Off for M1**: those fields still
    #: hold the *demoted* language's audio until M5 overwrites them, so referencing them
    #: anywhere would play the wrong language. Turned on once TTS has filled them.
    include_audio: bool = False
    #: When audio is included, put it on the front. Defaults to True because the target
    #: side is the prompt, and hearing the prompt is the point.
    audio_on_front: bool = True
    #: Emitted as the clone's template name.
    template_name: str = "Production"
    #: Appended to the source CSS. Set False to manage styling entirely by hand.
    append_css: bool = True
    #: Override the order front/back role-blocks are emitted in. ``None`` (the default,
    #: and what every caller used before this option existed) reproduces the fixed order
    #: below byte-for-byte. A role valid for that side but missing from a given order is
    #: appended in its default position rather than silently dropped -- a partial custom
    #: order can reorder blocks but can never make one disappear. See
    #: :func:`split_render_order` for turning one whole-card order (e.g. from a
    #: drag-reorderable field list) into this pair.
    front_order: Optional[Sequence[Role]] = None
    back_order: Optional[Sequence[Role]] = None


@dataclass
class GeneratedTemplates:
    front_html: str
    back_html: str
    css: str
    template_name: str
    #: Every field name referenced by the generated HTML. Used by tests and by the
    #: pre-flight check to prove we never emit a reference to a field that isn't there.
    referenced_fields: List[str] = _dc_field(default_factory=list)


# ---------------------------------------------------------------------------
# emission helpers
# ---------------------------------------------------------------------------


def _classes(*names: Optional[str]) -> str:
    return " ".join(n for n in names if n)


def _block(binding: FieldBinding, role_class: str, *, conditional: bool = True) -> str:
    """One field, wrapped in a div, optionally guarded by a conditional section.

    The guard matters: this deck has notes with empty audio and 1980 empty ``Caution``
    values, and an unguarded div would render as a stray empty box on every one of them.
    """
    inner = '<div class="%s">%s</div>' % (
        _classes(role_class, binding.css_class),
        binding.reference(),
    )
    if not conditional:
        return inner
    return "%s%s%s" % (binding.conditional_open(), inner, binding.conditional_close())


def _role_blocks(
    mapping: RoleMapping,
    role: Role,
    role_class: str,
    *,
    first_unconditional: bool = False,
) -> List[str]:
    """Render every field bound to ``role``, in the order the profile listed them."""
    out: List[str] = []
    for index, binding in enumerate(mapping.get(role)):
        conditional = not (first_unconditional and index == 0)
        out.append(_block(binding, role_class, conditional=conditional))
    return out


def _audio_blocks(mapping: RoleMapping, roles: Sequence[Role]) -> List[str]:
    """Audio references get no wrapper div -- Anki renders them as a play button."""
    out: List[str] = []
    for role in roles:
        for binding in mapping.get(role):
            out.append(
                "%s%s%s"
                % (binding.conditional_open(), binding.reference(), binding.conditional_close())
            )
    return out


def _join(parts: Sequence[str]) -> str:
    return "\n".join(p for p in parts if p)


# ---------------------------------------------------------------------------
# public API
# ---------------------------------------------------------------------------


def generate_templates(
    mapping: RoleMapping,
    *,
    source_css: str = "",
    options: Optional[TemplateOptions] = None,
) -> GeneratedTemplates:
    """Build front/back HTML and CSS for ``mapping``.

    ``source_css`` is the original notetype's CSS. It is carried over **verbatim** and our
    generated block appended, because real decks routinely declare ``@font-face`` rules
    pointing at media-folder files; dropping them would break the card's appearance.

    Raises :class:`~addon.core.role_schema.ValidationError` if the mapping could not
    produce a usable card.
    """
    opts = options or TemplateOptions()
    mapping.validate().raise_if_failed()

    front = _build_front(mapping, opts)
    back = _build_back(mapping, opts)

    css = source_css
    if opts.append_css:
        css = _append_css(source_css)

    return GeneratedTemplates(
        front_html=front,
        back_html=back,
        css=css,
        template_name=opts.template_name,
        referenced_fields=_referenced_fields(front, back),
    )


#: Roles eligible for the front, in the fixed default order -- exactly what the generator
#: emitted before ``front_order``/``back_order`` existed.
_FRONT_ROLES: Tuple[Role, ...] = (
    Role.TARGET_TERM, Role.TARGET_READING, Role.POS, Role.TARGET_SENTENCE,
)
_FRONT_CLASSES = {
    Role.TARGET_TERM: "%s-target-term" % _PREFIX,
    Role.TARGET_READING: "%s-target-reading" % _PREFIX,
    Role.POS: "%s-pos" % _PREFIX,
    Role.TARGET_SENTENCE: "%s-target-sentence" % _PREFIX,
}

#: Roles eligible for the back, in the fixed default order.
_BACK_ROLES: Tuple[Role, ...] = (
    Role.NATIVE_TERM, Role.NATIVE_READING, Role.NATIVE_SENTENCE, Role.IMAGE, Role.NOTES,
)
_BACK_CLASSES = {
    Role.NATIVE_TERM: "%s-native-term" % _PREFIX,
    Role.NATIVE_READING: "%s-native-reading" % _PREFIX,
    Role.NATIVE_SENTENCE: "%s-native-sentence" % _PREFIX,
    Role.IMAGE: "%s-image" % _PREFIX,
    Role.NOTES: "%s-notes" % _PREFIX,
}

_AUDIO_ROLES = (Role.TARGET_AUDIO, Role.TARGET_SENTENCE_AUDIO)


def _resolve_order(
    default_order: Sequence[Role], custom_order: Optional[Sequence[Role]]
) -> List[Role]:
    """``custom_order`` filtered to roles valid for this side, plus any valid role it left
    out appended in its default position -- so a partial custom order can reorder blocks
    but never silently drops one."""
    if custom_order is None:
        return list(default_order)
    valid = set(default_order)
    ordered = [r for r in custom_order if r in valid]
    ordered += [r for r in default_order if r not in ordered]
    return ordered


def split_render_order(
    render_order: Optional[Sequence[Role]], *, audio_on_front: bool = True
) -> Tuple[Optional[List[Role]], Optional[List[Role]]]:
    """Split one whole-card role order -- e.g. what a drag-reorderable field list produces
    reading its rows top-to-bottom, :class:`~addon.core.role_schema.RoleMapping`'s
    ``render_order`` -- into the ``front_order``/``back_order`` :class:`TemplateOptions`
    expects. ``None`` in, ``(None, None)`` out, so "no custom order" round-trips cleanly.
    """
    if render_order is None:
        return None, None
    front_roles = set(_FRONT_ROLES)
    back_roles = set(_BACK_ROLES)
    if audio_on_front:
        front_roles |= set(_AUDIO_ROLES)
    else:
        back_roles |= set(_AUDIO_ROLES)
    front = [r for r in render_order if r in front_roles]
    back = [r for r in render_order if r in back_roles]
    return front, back


def _build_front(mapping: RoleMapping, opts: TemplateOptions) -> str:
    order = _resolve_order(_FRONT_ROLES, opts.front_order)

    # Whichever of these actually carries content is the primary prompt, and its first
    # binding is kept unconditional regardless of where it lands in the order: Anki
    # refuses to build a card whose front can render entirely empty, so the primary
    # prompt must always emit something.
    primary = Role.TARGET_TERM if mapping.has(Role.TARGET_TERM) else Role.TARGET_SENTENCE

    parts: List[str] = []
    for role in order:
        parts += _role_blocks(
            mapping, role, _FRONT_CLASSES[role], first_unconditional=(role is primary)
        )

    if opts.include_audio and opts.audio_on_front:
        parts += _audio_blocks(mapping, _AUDIO_ROLES)

    return _join(parts)


def _build_back(mapping: RoleMapping, opts: TemplateOptions) -> str:
    order = _resolve_order(_BACK_ROLES, opts.back_order)
    parts: List[str] = ["{{FrontSide}}", '<hr id="answer">']

    if opts.include_audio and not opts.audio_on_front:
        parts += _audio_blocks(mapping, _AUDIO_ROLES)

    for role in order:
        parts += _role_blocks(mapping, role, _BACK_CLASSES[role])

    if opts.include_unmapped:
        extra = mapping.unmapped()
        if extra:
            parts.append('<div class="%s-unmapped">' % _PREFIX)
            for binding in extra:
                parts.append(_block(binding, "%s-unmapped-field" % _PREFIX))
            parts.append("</div>")

    return _join(parts)


def _append_css(source_css: str) -> str:
    """Append our block, replacing any previous one so regeneration stays idempotent."""
    base = source_css
    marker_at = base.find(GENERATED_CSS_MARKER)
    if marker_at != -1:
        base = base[:marker_at].rstrip()

    rules = [
        ("target-term", "font-size: 32px; font-weight: 600;"),
        ("target-reading", "font-size: 18px; opacity: 0.75;"),
        ("pos", "font-size: 14px; opacity: 0.6;"),
        ("target-sentence", "font-size: 20px; margin-top: 12px;"),
        ("native-term", "font-size: 40px; margin-top: 8px;"),
        ("native-reading", "font-size: 20px; opacity: 0.75;"),
        ("native-sentence", "font-size: 28px; margin-top: 8px;"),
        ("notes", "font-size: 14px; opacity: 0.7; margin-top: 12px;"),
        ("unmapped", "font-size: 12px; opacity: 0.45; margin-top: 16px;"),
    ]
    lines = [GENERATED_CSS_MARKER]
    lines += [".%s-%s { %s }" % (_PREFIX, name, body) for name, body in rules]
    lines.append(".%s-image img { max-height: 200px; }" % _PREFIX)
    generated = "\n".join(lines)

    return (base.rstrip() + "\n\n" + generated).lstrip("\n")


def _referenced_fields(*html: str) -> List[str]:
    """Every field name the generated HTML mentions, including conditional sections."""
    found: List[str] = []
    for chunk in html:
        for raw in re.findall(r"\{\{([^}]+)\}\}", chunk):
            name = raw.strip()
            if name.startswith(("#", "/", "^")):
                name = name[1:].strip()
            elif ":" in name:
                name = name.rsplit(":", 1)[1].strip()
            if name in ("FrontSide", "Tags", "Type", "Deck", "Subdeck", "Card", "CardFlag"):
                continue
            if name and name not in found:
                found.append(name)
    return found
