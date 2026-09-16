"""Records and reads back whether a notetype was produced by this addon's conversion, and in
which direction -- replacing the old approach of re-deriving direction by parsing a notetype's
current templates, which broke the moment it was pointed at a notetype the addon had already
converted (the very same templates, read backwards the second time; see ``addon/llm/direction.py``
for the fuller account of why direction is no longer inferred from template structure at all).

Two independent marks are written at conversion time: a tag added to every converted note, and a
comment appended to the notetype's CSS. Either alone is conclusive -- each can be lost
independently (CSS is hand-editable from Anki's own card-layout screen; tags can be bulk-removed
by the user), and both survive ``.apkg`` export/import, moving to a different machine, or simply
reopening the addon much later. Reading either one back is enough to answer: has this notetype
already been converted, and if so, what's on the front vs. the back now.

This module only answers that one question. Whether a converted deck *also* still needs audio
generated is a separate concern (comparing note content against however audio-generation
progress is tracked) and does not belong here.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Optional, Sequence

__all__ = [
    "ConversionState",
    "conversion_tag",
    "append_css_marker",
    "state_from_tags",
    "state_from_css",
    "state_from_notetype",
]

#: Namespace prefix shared with this addon's other self-authored artifacts (the ``ddc-audio``
#: field name, etc.) -- distinguishes marks this addon wrote from anything a deck already had.
_TAG_PREFIX = "ddc-converted"
_TAG_RE = re.compile(r"^%s::([^:]+)::([^:]+)$" % re.escape(_TAG_PREFIX))
_CSS_MARKER_RE = re.compile(r"/\*\s*ddc-converted:\s*target=(\S+)\s+native=(\S+?)\s*\*/")


@dataclass(frozen=True)
class ConversionState:
    target_language: str
    native_language: str


def conversion_tag(target_language: str, native_language: str) -> str:
    """The tag to add to every note on a converted notetype's clone.

    Uses Anki's own ``::`` tag-hierarchy separator (shows as a nested
    ``ddc-converted > <target> > <native>`` group in the tag browser), matching how this
    addon's other internal tags are namespaced.
    """
    return "%s::%s::%s" % (_TAG_PREFIX, target_language, native_language)


def append_css_marker(css: str, target_language: str, native_language: str) -> str:
    """Return ``css`` with the conversion marker appended, replacing any marker already there.

    Idempotent rather than additive on purpose: re-analyzing/re-converting an already-marked
    notetype must not accumulate a growing pile of stale comments.
    """
    stripped = _CSS_MARKER_RE.sub("", css or "").rstrip()
    marker = "/* ddc-converted: target=%s native=%s */" % (target_language, native_language)
    return (stripped + "\n" + marker + "\n") if stripped else (marker + "\n")


def state_from_tags(tags: Sequence[str]) -> Optional[ConversionState]:
    """Read conversion state back from a note's tags, if the mark is present."""
    for tag in tags:
        match = _TAG_RE.match(tag)
        if match:
            return ConversionState(target_language=match.group(1), native_language=match.group(2))
    return None


def state_from_css(css: str) -> Optional[ConversionState]:
    """Read conversion state back from a notetype's css, if the mark is present."""
    match = _CSS_MARKER_RE.search(css or "")
    if match is None:
        return None
    return ConversionState(target_language=match.group(1), native_language=match.group(2))


def state_from_notetype(css: str, sample_tags: Sequence[str] = ()) -> Optional[ConversionState]:
    """Read conversion state from whichever mark is still present.

    Checks the css marker first -- a single check against the notetype itself, available even
    with zero notes -- falling back to a sample of a note's tags if the css marker was stripped
    (e.g. hand-edited away in Anki's card-layout screen). Returns ``None`` if neither mark is
    found: this notetype was never converted by this addon, or every trace of it is gone.
    """
    from_css = state_from_css(css)
    if from_css is not None:
        return from_css
    return state_from_tags(sample_tags)
