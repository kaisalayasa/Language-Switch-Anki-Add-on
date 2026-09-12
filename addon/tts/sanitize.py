"""Clean raw Anki field HTML into plain text worth feeding to a TTS engine.

Pure string processing -- no filesystem, no subprocess, no network. Real field data (see
``docs/deck-facts.md``) looks like::

    processing,&nbsp;management<div>(unlike 加工, a new thing is not created)</div>

Feeding that straight to a synthesizer produces garbage: HTML tags read as literal text,
``&nbsp;`` doesn't decode itself, and a gloss that embeds a few characters of the *other*
language mid-sentence makes most engines mispronounce the whole line.

This module knows nothing about which language is being spoken -- ``allowed_ranges`` is a
caller-supplied list of Unicode codepoint ranges, not a hardcoded script. That mirrors
``addon/core``'s rule against baking a specific language into pure logic; the concrete range
list (e.g. "Latin") lives with whoever calls this, not here.
"""

from __future__ import annotations

import html
import re
from typing import Iterable, Sequence, Tuple

__all__ = ["sanitize_text", "LATIN_RANGES"]

# Basic Latin + Latin-1 Supplement + Latin Extended-A/B -- enough for English and most
# Western European languages. Not exhaustive; callers targeting another script pass their
# own ranges instead of this one.
LATIN_RANGES: Tuple[Tuple[int, int], ...] = (
    (0x0041, 0x007A),  # A-Z, [ \ ] ^ _ ` , a-z (includes a few punctuation codepoints; the
                        # always-kept punctuation set below makes that harmless)
    (0x00C0, 0x024F),  # Latin-1 Supplement letters + Latin Extended-A/B
)

# Kept regardless of script: whitespace, digits, and punctuation a TTS engine needs to see
# to phrase a sentence correctly (pauses, contractions, quotation).
_ALWAYS_KEEP = set(" \t\n\r0123456789.,!?;:'\"()-–—…/&")

_COMMENT_RE = re.compile(r"<!--.*?-->", re.DOTALL)
_SOUND_TAG_RE = re.compile(r"\[sound:[^\]]*\]")
_RUBY_RE = re.compile(r"\[[^\[\]]{0,40}\]")  # kanji[reading]-style furigana annotations
_TAG_RE = re.compile(r"<[^>]+>")
_WHITESPACE_RE = re.compile(r"\s+")


def sanitize_text(raw: str, *, allowed_ranges: Sequence[Tuple[int, int]] = ()) -> str:
    """Strip markup/comments/ruby annotations, decode entities, then optionally filter to
    a script allowlist.

    ``allowed_ranges`` empty (the default) means "keep every character" -- only the
    markup-stripping steps run. Pass e.g. :data:`LATIN_RANGES` to also drop characters
    outside that script (after markup is gone, so a stray ``<br>`` doesn't confuse the
    filter).
    """
    text = _COMMENT_RE.sub(" ", raw)
    text = _SOUND_TAG_RE.sub(" ", text)
    text = _RUBY_RE.sub("", text)
    text = _TAG_RE.sub(" ", text)
    text = html.unescape(text)
    if allowed_ranges:
        text = _filter_script(text, allowed_ranges)
    text = _WHITESPACE_RE.sub(" ", text).strip()
    return text


def _filter_script(text: str, allowed_ranges: Iterable[Tuple[int, int]]) -> str:
    ranges = list(allowed_ranges)
    kept = []
    for ch in text:
        if ch in _ALWAYS_KEEP:
            kept.append(ch)
            continue
        cp = ord(ch)
        if any(lo <= cp <= hi for lo, hi in ranges):
            kept.append(ch)
        else:
            kept.append(" ")  # drop the character but keep word boundaries intact
    return "".join(kept)
