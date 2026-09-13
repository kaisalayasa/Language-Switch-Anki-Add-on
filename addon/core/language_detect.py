"""Language detection: a fast Unicode-script pass, falling back to ``langdetect`` for
same-script (Latin) text where the script alone can't tell two languages apart.

This module *seeds* suggested defaults for M3's mapper UI -- it never decides anything on
its own. Per ``claude.md``'s "Language detection" section: surface the guess with a
confidence indicator, let the user override, never silently finalize it. Concretely, that
means this module only ever reports a language *per field*; deciding which detected language
is Target (front, being studied) vs Native (back, already known) is a pedagogical/directional
choice only a human can make, and is deliberately left to the UI layer, not decided here.

Unlike ``role_schema.py``/``template_generator.py``, this module is *not* required to be
language-agnostic (see ``tests/test_purity.py``) -- naming real languages is its entire job.
It does still avoid ``anki``/``aqt`` imports, like the rest of ``core/``, so it stays testable
with stock Python.

``langdetect`` is vendored (see ``addon/vendor/README.md``) rather than pip-installed, since
Anki addons run inside Anki's own bundled interpreter with no ``pip`` at runtime, and
``claude.md``'s hard constraints forbid any network call except the Piper binary/voice
downloads. ``py3langid`` (the name in ``claude.md``) was rejected in an earlier session
because it pulls in ``numpy``, a compiled per-platform dependency -- the same packaging
problem the Piper *subprocess* design exists to avoid.
"""

from __future__ import annotations

import os
import sys
from collections import Counter
from dataclasses import dataclass
from typing import List, Optional, Sequence, Tuple

__all__ = ["LanguageGuess", "detect_language", "detect_field_language"]

_VENDOR_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "vendor")
if _VENDOR_DIR not in sys.path:
    sys.path.insert(0, _VENDOR_DIR)

from langdetect import DetectorFactory, LangDetectException, detect_langs  # noqa: E402

# Without this, detect_langs() seeds Python's random module from OS entropy on every call,
# so the same field text can get a different guess on different runs -- confirmed
# empirically while verifying this dependency, not a theoretical concern. See
# addon/vendor/README.md.
DetectorFactory.seed = 0

# Unicode script fast-pass, exactly the ranges claude.md specifies. This is a deliberate
# approximation claude.md itself names ("Japanese-ish", "Russian-ish") -- e.g. CJK Unified
# Ideographs are shared with Chinese, so Chinese-only text also tags as "ja" here. Latin
# script is *not* listed: it's ambiguous between many languages and always falls through to
# the langdetect pass below instead.
_SCRIPT_RANGES: List[Tuple[str, Tuple[Tuple[int, int], ...]]] = [
    ("ja", ((0x3040, 0x309F), (0x30A0, 0x30FF), (0x4E00, 0x9FFF))),  # Hiragana, Katakana, CJK
    ("ko", ((0xAC00, 0xD7A3), (0x1100, 0x11FF))),  # Hangul syllables + Hangul Jamo
    ("ru", ((0x0400, 0x04FF),)),  # Cyrillic
]

# Below this length, langdetect's n-gram model is noise, not signal -- confirmed
# empirically: a single character ("a") returns a high-confidence *wrong* guess ("tl").
_MIN_LANGDETECT_LENGTH = 3


@dataclass(frozen=True)
class LanguageGuess:
    """A detector's best guess for one piece of text, with its own confidence label.

    ``confidence`` is one of ``"script"`` (a Unicode-range match -- treat as reliable),
    ``"langdetect"`` (a statistical guess -- treat as a hint, not a fact), or ``"unknown"``
    (no usable signal, ``code`` is ``None``).
    """

    code: Optional[str]
    confidence: str
    probability: Optional[float] = None

    @property
    def is_confident(self) -> bool:
        return self.code is not None


def _script_guess(text: str) -> Optional[str]:
    for code, ranges in _SCRIPT_RANGES:
        for ch in text:
            cp = ord(ch)
            if any(lo <= cp <= hi for lo, hi in ranges):
                return code
    return None


def detect_language(text: str) -> LanguageGuess:
    """Guess the language of a single piece of text.

    Empty/whitespace-only text and text too short for the statistical fallback to be
    meaningful both resolve to ``"unknown"`` rather than guessing -- a wrong guess with a
    confident label is worse than admitting there's no signal.
    """
    text = text.strip()
    if not text:
        return LanguageGuess(code=None, confidence="unknown")

    script_code = _script_guess(text)
    if script_code is not None:
        return LanguageGuess(code=script_code, confidence="script")

    if len(text) < _MIN_LANGDETECT_LENGTH:
        return LanguageGuess(code=None, confidence="unknown")

    try:
        results = detect_langs(text)
    except LangDetectException:
        return LanguageGuess(code=None, confidence="unknown")
    if not results:
        return LanguageGuess(code=None, confidence="unknown")
    top = results[0]
    return LanguageGuess(code=top.lang, confidence="langdetect", probability=top.prob)


def detect_field_language(samples: Sequence[str]) -> LanguageGuess:
    """Aggregate a field's guess over a few real sample values (see
    ``convert_dialog._collect_samples``), majority-voting the code so one short or noisy
    sample can't dominate the guess for the whole field the way a single-character
    ``detect_language`` call can.
    """
    guesses = [detect_language(s) for s in samples if s and s.strip()]
    confident = [g for g in guesses if g.is_confident]
    if not confident:
        return LanguageGuess(code=None, confidence="unknown")

    counts = Counter(g.code for g in confident)
    best_code, _ = counts.most_common(1)[0]
    return next(g for g in confident if g.code == best_code)
