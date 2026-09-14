"""Which characters a given voice can actually pronounce.

``sanitize.sanitize_text`` filters text to a set of Unicode ranges, and deliberately knows
nothing about languages -- the concrete range list belongs with whoever calls it. This is
that caller-side table.

Why it exists: the sanitizer previously ran with a hardcoded Latin range list at every call
site regardless of the voice. That is right for an English voice and silently catastrophic
for any other -- text in a non-Latin script is filtered away character by character until
nothing is left, and "nothing left to synthesize" is indistinguishable from an empty field.
A whole deck can process in seconds, report no failures, and produce no audio at all.

Keyed off the **voice**, not the declared target language, because the voice is what is
going to speak: keeping characters a voice cannot pronounce is what produces garbage output,
and that is a property of the voice alone. A voice/deck mismatch (say an English voice on
Korean text) then shows up as "there was nothing to say", which is true and reported, rather
than as plausible-sounding noise.

An unrecognised locale maps to **no filtering at all** rather than to a guess. Markup is
still stripped; only the script allowlist is skipped. Passing through a few characters a
voice mispronounces is a much smaller failure than deleting every character of a script this
table happens not to list yet.
"""

from __future__ import annotations

import re
from typing import Dict, Tuple

from .sanitize import LATIN_RANGES

__all__ = ["ranges_for_language", "ranges_for_voice", "language_of_voice"]

Ranges = Tuple[Tuple[int, int], ...]

_KANA: Ranges = ((0x3040, 0x309F), (0x30A0, 0x30FF))
_CJK: Ranges = ((0x3400, 0x4DBF), (0x4E00, 0x9FFF), (0xF900, 0xFAFF))
_HANGUL: Ranges = ((0x1100, 0x11FF), (0x3130, 0x318F), (0xAC00, 0xD7A3))
_CYRILLIC: Ranges = ((0x0400, 0x04FF), (0x0500, 0x052F))
_GREEK: Ranges = ((0x0370, 0x03FF), (0x1F00, 0x1FFF))
_ARABIC: Ranges = ((0x0600, 0x06FF), (0x0750, 0x077F), (0xFB50, 0xFDFF), (0xFE70, 0xFEFF))
_HEBREW: Ranges = ((0x0590, 0x05FF), (0xFB1D, 0xFB4F))
_DEVANAGARI: Ranges = ((0x0900, 0x097F), (0xA8E0, 0xA8FF))
_THAI: Ranges = ((0x0E00, 0x0E7F),)
_ARMENIAN: Ranges = ((0x0530, 0x058F),)
_GEORGIAN: Ranges = ((0x10A0, 0x10FF), (0x2D00, 0x2D2F))

#: Only languages whose script is genuinely known are listed. Everything absent falls
#: through to "no script filtering", which is the safe direction to be wrong in.
_BY_LANGUAGE: Dict[str, Ranges] = {
    # Latin-script languages, where filtering earns its keep: real field data mixes in
    # characters of the *other* language mid-sentence, and those make a voice stumble.
    "af": LATIN_RANGES, "ca": LATIN_RANGES, "cs": LATIN_RANGES, "cy": LATIN_RANGES,
    "da": LATIN_RANGES, "de": LATIN_RANGES, "en": LATIN_RANGES, "es": LATIN_RANGES,
    "et": LATIN_RANGES, "fi": LATIN_RANGES, "fr": LATIN_RANGES, "hr": LATIN_RANGES,
    "hu": LATIN_RANGES, "id": LATIN_RANGES, "is": LATIN_RANGES, "it": LATIN_RANGES,
    "lb": LATIN_RANGES, "lt": LATIN_RANGES, "lv": LATIN_RANGES, "ms": LATIN_RANGES,
    "nl": LATIN_RANGES, "no": LATIN_RANGES, "pl": LATIN_RANGES, "pt": LATIN_RANGES,
    "ro": LATIN_RANGES, "sk": LATIN_RANGES, "sl": LATIN_RANGES, "sq": LATIN_RANGES,
    "sv": LATIN_RANGES, "sw": LATIN_RANGES, "tl": LATIN_RANGES, "tr": LATIN_RANGES,
    "vi": LATIN_RANGES,

    "ja": _KANA + _CJK,
    "ko": _HANGUL,
    "zh": _CJK,
    "ru": _CYRILLIC, "uk": _CYRILLIC, "bg": _CYRILLIC, "sr": _CYRILLIC, "mk": _CYRILLIC,
    "be": _CYRILLIC, "kk": _CYRILLIC,
    "el": _GREEK,
    "ar": _ARABIC, "fa": _ARABIC, "ur": _ARABIC, "ps": _ARABIC,
    "he": _HEBREW,
    "hi": _DEVANAGARI, "mr": _DEVANAGARI, "ne": _DEVANAGARI,
    "th": _THAI,
    "hy": _ARMENIAN,
    "ka": _GEORGIAN,
}

_LANG_RE = re.compile(r"^[A-Za-z]{2,3}")


def language_of_voice(voice_id: str) -> str:
    """The bare language code in a Piper voice id, e.g. ``en_US-lessac-medium`` -> ``en``.

    Returns ``""`` for anything that doesn't start with a language code, which callers
    treat the same as an unknown language.
    """
    match = _LANG_RE.match(str(voice_id or "").strip())
    return match.group(0).lower() if match else ""


def ranges_for_language(code: str) -> Ranges:
    """Unicode ranges worth keeping for ``code``. Empty means "keep every character"."""
    normalised = str(code or "").strip().lower().replace("-", "_")
    if not normalised:
        return ()
    for candidate in (normalised, normalised.split("_")[0]):
        if candidate in _BY_LANGUAGE:
            return _BY_LANGUAGE[candidate]
    return ()


def ranges_for_voice(voice_id: str) -> Ranges:
    """Unicode ranges worth keeping for whatever language ``voice_id`` speaks."""
    return ranges_for_language(language_of_voice(voice_id))
