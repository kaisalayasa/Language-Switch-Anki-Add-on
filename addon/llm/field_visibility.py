"""Deterministic show/hide toggling for a field already placed on the Front or Back -- the
non-technical alternative to hand-editing HTML, for a user who just wants to turn a field's
display on or off without touching Anki template syntax at all.

**Mechanism**: hiding a field wraps its rendering reference (``{{Field}}``, ``{{text:Field}}``,
any filter) in ``<span class="ddc-hidden">...</span>``, paired with one CSS rule
(:func:`ensure_hidden_field_css`) that sets ``display: none`` on it -- the same technique the
real Core 2000 deck's own CSS already uses for its ``.ios-only``/``.mac-only`` toggles, so this
is ordinary, well-understood Anki template behavior, not a new mechanism this addon invented.
Reversible by construction: showing a field again is exactly removing that same wrapper, so
toggling back and forth never loses or regenerates any markup -- the field's own reference, and
whatever surrounding HTML the AI wrote around it (a ``<div class="...">``, its class, everything
else), stays completely untouched either way. This is also why no separate hidden-state needs
to be tracked anywhere: the HTML itself is the single source of truth, read back by
:func:`is_field_hidden` -- exactly the same principle ``core.deck_state`` already uses for
conversion state (a fact recorded in the artifact itself, not a shadow flag that can drift).

Never touches a ``{{#Field}}...{{/Field}}`` conditional guard (a presence check, not a render --
wrapping it would break the template), and never lists a generated-audio field
(``core.audio_fields.is_generated_field``) as toggleable at all: that field's own visibility is
already governed by whether TTS has actually filled it in (the conditional
``llm.analyze._append_audio_html`` appends), a structurally different concern from "does this
field have real content the user wants to show or hide" -- exposing it here would risk a user
hiding their own audio player by mistake, not turning off unwanted text.
"""

from __future__ import annotations

import re
from typing import Tuple

from ..core.audio_fields import is_generated_field
from .template_fields import content_reference_pattern, referenced_fields

__all__ = [
    "HIDDEN_FIELD_CSS_CLASS",
    "visible_fields",
    "is_field_hidden",
    "set_field_hidden",
    "ensure_hidden_field_css",
]

#: Namespaced like every other artifact this addon writes into a card (``ddc-audio-*``, the
#: ``ddc-converted`` css marker, the ``ddc-tts-generated`` tag) -- its own, not a deck's.
HIDDEN_FIELD_CSS_CLASS = "ddc-hidden"

_HIDDEN_CSS_RULE = ".%s { display: none; }" % HIDDEN_FIELD_CSS_CLASS


def visible_fields(front: str, back: str) -> Tuple[Tuple[str, ...], Tuple[str, ...]]:
    """Real (non-generated-audio) fields referenced anywhere on ``front``/``back``, in template
    order -- the toggleable list a field-hide panel shows.

    A field already hidden (wrapped, see module docstring) is still found here: wrapping only
    adds a surrounding ``<span>``, it never removes the ``{{Field}}`` reference itself, so a
    hidden field's checkbox can still be shown (unchecked) rather than disappearing from the
    list entirely once toggled off. A field never placed at all -- a deck's own bookkeeping
    field ``llm.direction`` already excluded, or a not-yet-existing generated-audio field -- is
    correctly absent here too, since there's nothing in the HTML to find.

    A field referenced on both sides counts as Front-only, matching
    ``template_fields.current_sides``'s own rule (the Back routinely re-shows the Front via
    ``{{FrontSide}}``, which would otherwise double list everything).
    """
    front_all = [n for n in referenced_fields(front) if not is_generated_field(n)]
    back_all = [n for n in referenced_fields(back) if not is_generated_field(n)]
    back_only = [n for n in back_all if n not in front_all]
    return tuple(front_all), tuple(back_only)


def _wrapped_pattern(field_name: str) -> "re.Pattern":
    return re.compile(
        r'<span class="%s">(\{\{(?:[A-Za-z0-9_ .\-]+:)?%s\}\})</span>'
        % (HIDDEN_FIELD_CSS_CLASS, re.escape(field_name))
    )


def is_field_hidden(html: str, field_name: str) -> bool:
    """Whether ``field_name``'s reference is currently wrapped in the hidden-field span."""
    return _wrapped_pattern(field_name).search(html or "") is not None


def set_field_hidden(html: str, field_name: str, hidden: bool) -> str:
    """Return ``html`` with ``field_name``'s rendering reference(s) wrapped (``hidden=True``) or
    unwrapped (``hidden=False``).

    A no-op if already in the requested state, so toggling twice in the same direction can
    never nest ``<span>`` wrappers. Every occurrence is toggled together -- a field referenced
    more than once (unusual for AI-generated templates, but not forbidden) must not end up
    half-hidden.
    """
    if hidden == is_field_hidden(html, field_name):
        return html
    if hidden:
        pattern = content_reference_pattern(field_name)
        return pattern.sub(
            lambda m: '<span class="%s">%s</span>' % (HIDDEN_FIELD_CSS_CLASS, m.group(0)), html
        )
    return _wrapped_pattern(field_name).sub(lambda m: m.group(1), html)


def ensure_hidden_field_css(css: str) -> str:
    """Return ``css`` with the one rule :func:`set_field_hidden`'s wrapper depends on present,
    appending it only if missing -- idempotent, matching ``deck_state.append_css_marker``'s own
    "don't accumulate copies on repeat calls" discipline.
    """
    if HIDDEN_FIELD_CSS_CLASS in (css or ""):
        return css
    stripped = (css or "").rstrip()
    return (stripped + "\n" + _HIDDEN_CSS_RULE + "\n") if stripped else (_HIDDEN_CSS_RULE + "\n")
