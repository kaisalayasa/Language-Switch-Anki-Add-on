"""Deterministic show/hide toggling for a field already placed on the Front or Back -- the
non-technical alternative to hand-editing HTML, for a user who just wants to turn a field's
display on or off without touching Anki template syntax at all.

**Mechanism**: hiding a field removes its rendering reference from the HTML entirely, replacing
it with an inert marker comment -- ``<!--ddc-hidden:Field:filter-->`` -- that records the field
name and whatever filter (or none) the reference used, so showing it again restores the *exact*
original reference, byte for byte, in the exact same place. Nothing is left behind to render:
no wrapper element, no CSS rule, because there's nothing left in the markup to hide -- a marker
comment is inert HTML, invisible in the browser and untouched by Anki's own ``{{...}}``
substitution (it contains no ``{{``/``}}`` characters, so there's nothing there for Anki to
re-substitute).

**This used to wrap the reference in a `<span class="ddc-hidden">` instead, paired with a CSS
`display: none` rule** -- found in real use to leave visible clutter behind in the generated
HTML (an empty wrapper sitting in the template even while hidden) for no remaining benefit: an
earlier version of this module also forced the wrapped reference through ``{{text:Field}}``,
under the mistaken belief that this was needed to stop a hidden audio-bearing field from still
autoplaying (see ``docs/api-notes.md`` for why ``{{text:Field}}`` never actually did that). That
belief is moot now regardless of hide mechanism: pre-existing audio is stripped out of every
field's *data* at Convert time (``ops/notetype_manager.py``'s ``_strip_pre_existing_audio``), so
by the time a converted card exists to hide fields on, there is no ``[sound:...]`` left in any
field's value for this module to worry about protecting against. With that concern gone, there
was no reason left to keep a wrapper element around instead of just removing the reference.

No separate hidden-state needs to be tracked anywhere else: the HTML itself is the single source
of truth, read back by :func:`is_field_hidden` -- the same principle ``core.deck_state`` already
uses for conversion state (a fact recorded in the artifact itself, not a shadow flag that can
drift).

Never touches a ``{{#Field}}...{{/Field}}`` conditional guard (a presence check, not a render --
removing it would break the template), and never lists a generated-audio field
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
    "HIDDEN_MARKER_PREFIX",
    "visible_fields",
    "is_field_hidden",
    "set_field_hidden",
]

#: Namespaced like every other artifact this addon writes into a card (``ddc-audio-*``, the
#: ``ddc-converted`` css marker, the ``ddc-tts-generated`` tag) -- its own, not a deck's.
HIDDEN_MARKER_PREFIX = "ddc-hidden"

#: The filter-prefix part of a reference, e.g. "text" or "furigana" -- letters/digits/space/
#: dot/hyphen/underscore only (matches content_reference_pattern's own filter character class),
#: so it's always safe to embed inside an HTML comment with no escaping.
_FILTER_CHARS = r"[A-Za-z0-9_ .\-]*"


def _marker(field_name: str, filt: str) -> str:
    return "<!--%s:%s:%s-->" % (HIDDEN_MARKER_PREFIX, field_name, filt)


def _marker_pattern(field_name: str) -> "re.Pattern":
    return re.compile(
        r"<!--%s:%s:(%s)-->" % (re.escape(HIDDEN_MARKER_PREFIX), re.escape(field_name), _FILTER_CHARS)
    )


def _any_marker_pattern() -> "re.Pattern":
    return re.compile(r"<!--%s:([^:]+):%s-->" % (re.escape(HIDDEN_MARKER_PREFIX), _FILTER_CHARS))


def visible_fields(front: str, back: str) -> Tuple[Tuple[str, ...], Tuple[str, ...]]:
    """Real (non-generated-audio) fields referenced -- or currently hidden as a marker comment,
    see module docstring -- anywhere on ``front``/``back``, in template order. The toggleable
    list a field-hide panel shows.

    A field already hidden is still found here: its marker comment still names it, so a hidden
    field's checkbox can stay listed (just unchecked) rather than disappearing from the list
    entirely once toggled off -- there would be no way to show it again otherwise. A field never
    placed at all -- a deck's own bookkeeping field ``llm.direction`` already excluded, or a
    not-yet-existing generated-audio field -- is correctly absent here too, since there's
    nothing in the HTML to find either way.

    A field referenced on both sides counts as Front-only, matching
    ``template_fields.current_sides``'s own rule (the Back routinely re-shows the Front via
    ``{{FrontSide}}``, which would otherwise double list everything).
    """
    def _fields_on(html: str):
        live = [n for n in referenced_fields(html) if not is_generated_field(n)]
        hidden = [
            n for n in (m.group(1) for m in _any_marker_pattern().finditer(html or ""))
            if not is_generated_field(n) and n not in live
        ]
        return live + hidden

    front_all = _fields_on(front)
    back_all = _fields_on(back)
    back_only = [n for n in back_all if n not in front_all]
    return tuple(front_all), tuple(back_only)


def _current_filter(html: str, field_name: str) -> str:
    """The filter prefix ``field_name`` is currently referenced with (``"text"``,
    ``"furigana"``, ...), or ``""`` for a bare ``{{Field}}`` reference. ``""`` if not
    referenced at all, which is harmless -- callers only reach this after confirming a
    reference exists.
    """
    match = content_reference_pattern(field_name).search(html or "")
    if not match:
        return ""
    inner = match.group(0)[2:-2]  # strip the surrounding {{ }}
    if ":" in inner:
        return inner.rsplit(":", 1)[0]
    return ""


def is_field_hidden(html: str, field_name: str) -> bool:
    """Whether ``field_name``'s reference is currently replaced by its hidden-marker comment."""
    return _marker_pattern(field_name).search(html or "") is not None


def set_field_hidden(html: str, field_name: str, hidden: bool) -> str:
    """Return ``html`` with ``field_name``'s rendering reference(s) removed (``hidden=True``,
    replaced by an inert marker comment) or restored (``hidden=False``, the marker replaced by
    the exact original reference).

    A no-op if already in the requested state, so toggling twice in the same direction can
    never double-remove or forget the original filter. Every occurrence is toggled together --
    a field referenced more than once (unusual for AI-generated templates, but not forbidden)
    must not end up half-hidden; if occurrences somehow differ, the first one found decides the
    filter every occurrence is restored to, which only matters in a case this addon doesn't
    itself ever produce.
    """
    if hidden == is_field_hidden(html, field_name):
        return html
    if hidden:
        original_filter = _current_filter(html, field_name)
        marker = _marker(field_name, original_filter)
        return content_reference_pattern(field_name).sub(lambda m: marker, html)

    def _restore(match: "re.Match") -> str:
        filt = match.group(1)
        return "{{%s:%s}}" % (filt, field_name) if filt else "{{%s}}" % field_name

    return _marker_pattern(field_name).sub(_restore, html)
