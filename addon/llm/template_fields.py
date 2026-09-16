"""Deterministic extraction of which fields a template currently shows on which side.

Same principle as ``audio_safety.py``: whether a field is currently on the Front or the Back of
a card is a mechanical fact, computed by parsing ``{{FieldName}}`` references out of the real
``qfmt``/``afmt`` strings -- there is no judgment call here for a model to get right or wrong.
The first real test of the prompt showed the model inverting this exact fact for a deck whose
field is literally named "Front" (colliding with the template-side vocabulary "front"/"back" the
whole task is built on): it claimed the deck was currently English-front/German-back when the
real templates show the opposite, and its output left the deck unconverted while claiming
success. Rather than trust a 1.5B model to parse Anki template syntax correctly under that kind
of naming collision, it's computed here and handed to the model as a given fact, the same way
``audio_safety.sound_field_names`` already is.
"""

from __future__ import annotations

import re
from typing import List, Sequence, Tuple

__all__ = ["referenced_fields", "current_sides"]

_REF_RE = re.compile(r"\{\{([^}]+)\}\}")

#: Not real note fields -- Anki's own built-in template keywords. Excluded because a caller
#: asking "which real fields does this template reference" never wants these mixed in.
_META_KEYWORDS = frozenset({"FrontSide", "Tags", "Type", "Deck", "Subdeck", "Card", "CardFlag"})


def referenced_fields(html: str) -> List[str]:
    """Every field name ``html`` references, in first-seen order.

    Covers plain ``{{Field}}``, conditional ``{{#Field}}``/``{{^Field}}``/``{{/Field}}``, and
    filtered ``{{filter:Field}}`` forms.
    """
    found: List[str] = []
    for raw in _REF_RE.findall(html or ""):
        name = raw.strip()
        if name.startswith(("#", "/", "^")):
            name = name[1:].strip()
        elif ":" in name:
            name = name.rsplit(":", 1)[1].strip()
        if not name or name in _META_KEYWORDS:
            continue
        if name not in found:
            found.append(name)
    return found


def current_sides(
    known_field_names: Sequence[str], qfmt: str, afmt: str
) -> Tuple[List[str], List[str]]:
    """``(front_fields, back_fields)`` currently shown by this notetype's live templates.

    Restricted to fields that actually exist on the notetype (``known_field_names``) and
    ordered to match it, so the result is stable regardless of the order fields happen to
    appear in the template HTML.

    A field referenced on both sides counts as front-only: the front is the more specific
    claim, and Anki's own Back templates routinely pull in the whole front via
    ``{{FrontSide}}`` anyway, which would otherwise double-count everything shown on the front.
    """
    known = list(known_field_names)
    front_raw = set(referenced_fields(qfmt)) & set(known)
    back_raw = (set(referenced_fields(afmt)) & set(known)) - front_raw
    front = [n for n in known if n in front_raw]
    back = [n for n in known if n in back_raw]
    return front, back
