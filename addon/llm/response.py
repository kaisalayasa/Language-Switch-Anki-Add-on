"""Parses the model's raw response text (one big string) into its four declared sections.

``prompt.py`` asks the model for a fixed shape: an ``--- ANALYSIS ---`` JSON header (holding
``description`` and ``speak_text_from``) followed by ``--- FRONT ---``, ``--- BACK ---``, and
``--- CSS ---`` blocks. This module is the other half of that contract -- it does not judge
whether the *content* of those sections is any good (a nonexistent field reference, an
unbalanced conditional, a placement violation are all real failure modes seen while testing this
prompt, but they're ``validate.py``'s job, not this module's). This module only answers: did the
model's reply actually have the four sections we asked for, in the right order, with valid JSON
in the header and the two required keys in it? If not, there is nothing downstream can safely do
with the reply, so it raises rather than guessing at a partial parse.

Markers are imported from ``prompt.py`` (not redefined here) so the two modules can never drift
out of sync silently.
"""

from __future__ import annotations

import json
from dataclasses import dataclass

from .prompt import ANALYSIS_MARKER, BACK_MARKER, CSS_MARKER, FRONT_MARKER

__all__ = ["ParsedResponse", "ResponseParseError", "parse_response"]

#: Markers in the order `prompt.py` asks the model to emit them.
_MARKERS_IN_ORDER = (ANALYSIS_MARKER, FRONT_MARKER, BACK_MARKER, CSS_MARKER)

#: Keys `parse_response` requires in the ANALYSIS JSON. Anything else the model includes
#: (e.g. a stale key from an older prompt version it was somehow trained/primed toward) is
#: ignored rather than rejected -- this module's job is extracting what's needed, not policing
#: the schema beyond that.
_REQUIRED_ANALYSIS_KEYS = frozenset({"description", "speak_text_from"})


class ResponseParseError(ValueError):
    """The model's response doesn't match the format ``prompt.py`` asked for."""


@dataclass(frozen=True)
class ParsedResponse:
    description: str
    speak_text_from: str
    front: str
    back: str
    css: str


def parse_response(text: str) -> ParsedResponse:
    """Split ``text`` (as returned by :func:`addon.llm.client.call_model`) into its sections.

    Raises :class:`ResponseParseError` if any marker is missing, the markers appear out of
    order, the ANALYSIS section isn't valid JSON, or it's missing a required key. Text before
    the first marker (a model preamble, despite being told not to write one) is discarded
    rather than rejected -- harmless as long as the real markers are still found after it.
    """
    missing = [m for m in _MARKERS_IN_ORDER if m not in text]
    if missing:
        raise ResponseParseError(
            "model response is missing section(s) %s:\n%s" % (missing, text)
        )

    positions = [text.index(m) for m in _MARKERS_IN_ORDER]
    if positions != sorted(positions):
        raise ResponseParseError(
            "model response sections are out of order (expected %s):\n%s"
            % (_MARKERS_IN_ORDER, text)
        )

    analysis_part = text.split(ANALYSIS_MARKER, 1)[1].split(FRONT_MARKER, 1)[0]
    front_part = text.split(FRONT_MARKER, 1)[1].split(BACK_MARKER, 1)[0]
    back_part = text.split(BACK_MARKER, 1)[1].split(CSS_MARKER, 1)[0]
    css_part = text.split(CSS_MARKER, 1)[1]

    try:
        analysis = json.loads(analysis_part.strip())
    except json.JSONDecodeError as e:
        raise ResponseParseError(
            "ANALYSIS section is not valid JSON (%s):\n%s" % (e, analysis_part)
        ) from e

    if not isinstance(analysis, dict):
        raise ResponseParseError(
            "ANALYSIS section must be a JSON object, got %s:\n%s"
            % (type(analysis).__name__, analysis_part)
        )

    missing_keys = _REQUIRED_ANALYSIS_KEYS - set(analysis)
    if missing_keys:
        raise ResponseParseError(
            "ANALYSIS is missing required key(s) %s:\n%s" % (sorted(missing_keys), analysis_part)
        )

    return ParsedResponse(
        description=str(analysis["description"]).strip(),
        speak_text_from=str(analysis["speak_text_from"]).strip(),
        front=front_part.strip(),
        back=back_part.strip(),
        css=css_part.strip(),
    )
