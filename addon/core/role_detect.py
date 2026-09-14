"""Guess a role mapping for a notetype nobody has written a profile for.

This is tier 3 of ``claude.md``'s "role assignment comes from exactly three sources":
a matched profile wins, then the user's own choice in the mapper UI, and only then this.
It produces a *seed* -- something sensible in the field list the moment a deck is selected,
which the user can then correct. It never gets the last word.

**No rule here looks at a field's name.** Field names are untrusted input: most decks call
everything ``Front``/``Back``/``Field 1``, and descriptive names lie (in the reference deck,
the field named ``Notes`` holds an index string and the one named ``Core-Index`` holds an
integer). Everything below comes from two honest signals instead -- where the notetype's own
templates put a field, and what the field's real content looks like.

Structure first
---------------
``Target`` and ``Native`` are *positional* definitions, not linguistic ones: Target is
whatever ends up on the front **after** conversion, Native whatever ends up on the back.
That makes the current template a direct answer rather than a guess:

* fields the current front shows are about to be demoted to the back -> **Native**
* fields only the current back shows are about to be promoted -> **Target**

with one catch that is easy to get wrong and produces a perfectly inverted mapping: this
holds for a deck that has **not** been converted yet. Run it against a notetype this addon
already flipped and it reads the flip a second time, reporting both languages backwards --
and since the two states are structurally identical (fields on the front, fields on the
back, either way), no amount of cleverness distinguishes them from the templates alone.

So the state is read from a mark the conversion itself leaves behind:
``template_generator.GENERATED_CSS_MARKER`` in the notetype's CSS, or a field named by
``audio_fields`` -- either proves the notetype is this addon's own output, and the rule then
runs the other way round. Both marks live in the collection, so they survive closing the
dialog, an ``.apkg`` round trip and a different machine.

Content second
--------------
Within a side, roles come from what the content *is*: an ``[sound:...]`` reference, a
``base[reading]`` ruby annotation, digits only, how often the field is empty at all, and how
long the text runs (a word is short, an example sentence is not). Language detection then
acts as a guard rather than a driver -- a field whose language disagrees with its own side's
majority is left unmapped instead of being put on the card, which is what keeps the
converted front showing one language.

Audio is not decided here
-------------------------
Which field holds the newly generated audio is policy, not detection: it is always a field
the addon creates. See ``audio_fields`` -- this module simply hands its result to
:func:`~addon.core.audio_fields.resolve_audio_fields`, which binds generated fields to the
target audio roles and demotes pre-existing audio to the native ones so it stays on the
notetype but off the card.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Dict, List, Optional, Sequence, Tuple

from .audio_fields import field_has_sound, is_generated_field, resolve_audio_fields
from .language_detect import detect_field_language
from .role_schema import FieldBinding, Role, RoleMapping
from .template_generator import GENERATED_CSS_MARKER, referenced_fields

__all__ = ["FieldSignals", "guess_role_mapping", "is_converted_notetype"]

_TAG_RE = re.compile(r"<[^>]+>")
_COMMENT_RE = re.compile(r"<!--.*?-->", re.DOTALL)
_SOUND_RE = re.compile(r"\[sound:[^\]]*\]")
_RUBY_RE = re.compile(r"\S+\[[^\[\]]+\]")
_ENTITY_RE = re.compile(r"&[a-zA-Z]+;|&#\d+;")
_WS_RE = re.compile(r"\s+")

#: Below this share of non-empty samples a field is bookkeeping or an optional extra, not
#: something to build a card around. The reference deck has a field that is empty on 1980 of
#: 1983 notes; putting that on the front would produce blank cards.
_MIN_FILL_RATIO = 0.2

#: A field only counts as the sentence rather than the term if it is both meaningfully
#: longer than the term and long enough to be a sentence at all -- otherwise two similar
#: short fields (a word and its plural, say) get split across two different roles.
_SENTENCE_MIN_CHARS = 18
_SENTENCE_LENGTH_RATIO = 1.6

#: Ruby markup has to be more than incidental before a field is treated as a reading: one
#: bracketed aside in one sample is not an annotation scheme.
_MIN_RUBY_RATIO = 0.5

#: How much text a *statistical* language guess needs behind it before this module will act
#: on it. A Unicode-script match is reliable at any length and ignores this; ``langdetect``
#: is not -- on single words it is confidently wrong often enough to matter ("water" comes
#: back as Afrikaans). Roughly a short phrase.
_RELIABLE_LANGDETECT_CHARS = 20


@dataclass(frozen=True)
class FieldSignals:
    """What one field's real content looks like. Derived only from sampled values."""

    name: str
    ord: int
    fill_ratio: float = 0.0
    avg_length: float = 0.0
    has_sound: bool = False
    ruby_ratio: float = 0.0
    numeric: bool = False
    language: Optional[str] = None
    #: How ``language`` was arrived at -- ``"script"``, ``"langdetect"`` or ``"unknown"``.
    #: Kept because the two are not equally trustworthy and this module acts on the
    #: difference; see :attr:`has_reliable_language`.
    language_confidence: str = "unknown"

    @property
    def has_reliable_language(self) -> bool:
        """Whether :attr:`language` is solid enough to *exclude* this field on.

        Voting on a weak guess is fine -- weight sorts it out. Acting on one is not: a
        four-letter word's guess is near noise, and dropping the deck's main term field
        because a statistical model called "water" Afrikaans would leave the card front
        empty. A Unicode-script match carries no such doubt at any length.
        """
        if self.language is None:
            return False
        if self.language_confidence == "script":
            return True
        return self.avg_length >= _RELIABLE_LANGDETECT_CHARS

    @property
    def is_ruby(self) -> bool:
        return self.ruby_ratio >= _MIN_RUBY_RATIO

    @property
    def is_sparse(self) -> bool:
        return self.fill_ratio < _MIN_FILL_RATIO

    @property
    def is_bookkeeping(self) -> bool:
        """Numeric or near-always-empty: real data, but never card content."""
        return self.numeric or self.is_sparse

    @property
    def is_content(self) -> bool:
        """Eligible to carry a term/sentence/reading role."""
        return not self.has_sound and not self.is_bookkeeping


def _visible_text(raw: str) -> str:
    """The text a reader would actually see: markup, comments and audio references gone."""
    text = _COMMENT_RE.sub(" ", raw or "")
    text = _SOUND_RE.sub(" ", text)
    text = _TAG_RE.sub(" ", text)
    text = _ENTITY_RE.sub(" ", text)
    return _WS_RE.sub(" ", text).strip()


def _signals_for(name: str, ord_: int, samples: Sequence[str]) -> FieldSignals:
    values = list(samples or [])
    if not values:
        return FieldSignals(name=name, ord=ord_)

    visible = [_visible_text(v) for v in values]
    non_empty = [v for v in visible if v]
    fill_ratio = len(non_empty) / len(values)
    avg_length = (sum(len(v) for v in non_empty) / len(non_empty)) if non_empty else 0.0

    ruby_hits = sum(1 for v in values if _RUBY_RE.search(v or ""))
    ruby_ratio = ruby_hits / len(values)

    numeric = bool(non_empty) and all(
        v.replace(".", "").replace(",", "").replace("-", "").isdigit() for v in non_empty
    )

    has_sound = field_has_sound(values)

    # Audio markup is meaningless to a language detector, and a field of pure digits has no
    # language either -- feeding it either one only produces a confident wrong answer.
    language = None
    confidence = "unknown"
    if non_empty and not numeric and not has_sound:
        guess = detect_field_language(non_empty)
        language, confidence = guess.code, guess.confidence

    return FieldSignals(
        name=name,
        ord=ord_,
        fill_ratio=fill_ratio,
        avg_length=avg_length,
        has_sound=has_sound,
        ruby_ratio=ruby_ratio,
        numeric=numeric,
        language=language,
        language_confidence=confidence,
    )


def is_converted_notetype(css: str, field_names: Sequence[str]) -> bool:
    """Whether this notetype is a conversion's own output rather than an original deck.

    Two independent marks, either of which is conclusive, because each can be lost on its
    own: CSS can be hand-edited in Anki's card-template screen, and a field can be renamed.
    """
    if GENERATED_CSS_MARKER in (css or ""):
        return True
    return any(is_generated_field(name) for name in field_names)


def _sides(
    field_names: Sequence[str], front_html: str, back_html: str
) -> Tuple[List[str], List[str]]:
    """``(current front fields, current back-only fields)``, in notetype order.

    A field shown on both sides counts as a front field -- the front is the more specific
    claim, and Anki's own back templates include ``{{FrontSide}}`` wholesale anyway.

    With no usable templates at all there is nothing structural to read, so this falls back
    to Anki's own default shape for a new notetype: the first field asks, the rest answer.
    """
    known = list(field_names)
    front = [n for n in referenced_fields(front_html or "") if n in known]
    back = [n for n in referenced_fields(back_html or "") if n in known and n not in front]

    if not front and not back:
        return known[:1], known[1:]
    if not front:
        # Everything sits on the back; treat the first as the prompt so a card can exist.
        return back[:1], back[1:]
    return front, back


def _majority_language(signals: Sequence[FieldSignals]) -> Optional[str]:
    """The language of a side, weighted by how much text each field actually carries.

    A flat one-field-one-vote count gets this wrong in a way that matters: ``langdetect`` is
    unreliable on very short text (a single common word can come back confidently wrong),
    and a deck side is typically one short term plus one long sentence. Weighting by average
    length lets the field with enough text to be detectable decide, instead of letting a
    four-character word outvote it.
    """
    weights: Dict[str, float] = {}
    for signal in signals:
        if signal.language is None or not signal.is_content:
            continue
        weights[signal.language] = weights.get(signal.language, 0.0) + max(
            signal.avg_length, 1.0
        )
    if not weights:
        return None
    # Sorted first so an exact weight tie resolves the same way on every run.
    return max(sorted(weights), key=lambda code: weights[code])


def _assign_side(
    signals: Sequence[FieldSignals],
    majority: Optional[str],
    term_role: Role,
    sentence_role: Role,
    reading_role: Role,
) -> Dict[Role, str]:
    """Sort one side's fields into term / reading / sentence.

    Off-language fields are dropped rather than placed: if this side's majority language is
    known and a field *reliably* disagrees with it, putting it on the card would make the
    converted front bilingual, which is exactly what the conversion is trying to undo. It
    stays unmapped, so it still renders on the back and nothing is lost.

    "Reliably" is load-bearing (see :attr:`FieldSignals.has_reliable_language`). Dropping
    fields on a weak guess is worse than not checking at all: the deck's main term field is
    short by nature, short text is exactly where the statistical detector fails, and
    discarding it leaves the side with only its example sentence -- or nothing.
    """
    eligible = [s for s in signals if s.is_content]
    if majority is not None:
        eligible = [
            s for s in eligible
            if s.language == majority or not s.has_reliable_language
        ]
    if not eligible:
        return {}

    out: Dict[Role, str] = {}

    readings = [s for s in eligible if s.is_ruby]
    plain = [s for s in eligible if not s.is_ruby]
    if readings and plain:
        # A ruby field duplicates its plain counterpart with the pronunciation baked in;
        # keep it as the reading only while there is still a plain field to be the term.
        out[reading_role] = readings[0].name
    else:
        plain = list(eligible)

    if not plain:
        return out

    by_length = sorted(plain, key=lambda s: (s.avg_length, s.ord))
    term = by_length[0]
    out[term_role] = term.name

    if len(by_length) > 1:
        longest = by_length[-1]
        if (
            longest.avg_length >= _SENTENCE_MIN_CHARS
            and longest.avg_length >= term.avg_length * _SENTENCE_LENGTH_RATIO
        ):
            out[sentence_role] = longest.name
    return out


def guess_role_mapping(
    notetype_name: str,
    live_fields: Sequence[Tuple[int, str]],
    samples_by_field: Dict[str, Sequence[str]],
    *,
    front_html: str = "",
    back_html: str = "",
    css: str = "",
) -> RoleMapping:
    """Seed a :class:`~addon.core.role_schema.RoleMapping` from structure and content.

    ``live_fields`` is ``[(ord, name), ...]`` straight off the notetype, and
    ``samples_by_field`` maps field name to a handful of **raw** values -- raw because
    ``[sound:...]`` and ``base[reading]`` markup are two of the signals, and any sanitizer
    worth using strips both.

    ``front_html``/``back_html``/``css`` come from the notetype's live first template.

    The result is a seed, not a verdict: it can come back incomplete (a deck with nothing
    detectable on one side) and is meant to be shown in the mapper UI, where a human can see
    and fix it. It never raises -- an unusable guess surfaces as a validation message in the
    UI, which is recoverable, rather than as an exception on deck selection, which is not.
    """
    ordered = sorted((int(o), str(n)) for o, n in live_fields)
    names = [n for _o, n in ordered]
    signals = {
        name: _signals_for(name, o, samples_by_field.get(name, ())) for o, name in ordered
    }

    converted = is_converted_notetype(css, names)
    current_front, current_back = _sides(names, front_html, back_html)

    # The whole structural rule, in one line. Before a conversion the current front holds
    # the language being demoted; after one it already holds the promoted language.
    if converted:
        target_names, native_names = current_front, current_back
    else:
        native_names, target_names = current_front, current_back

    target_signals = [signals[n] for n in target_names]
    native_signals = [signals[n] for n in native_names]

    target_language = _majority_language(target_signals)
    native_language = _majority_language(native_signals)

    assigned: Dict[Role, str] = {}
    assigned.update(
        _assign_side(
            target_signals,
            target_language,
            Role.TARGET_TERM,
            Role.TARGET_SENTENCE,
            Role.TARGET_READING,
        )
    )
    assigned.update(
        _assign_side(
            native_signals,
            native_language,
            Role.NATIVE_TERM,
            Role.NATIVE_SENTENCE,
            Role.NATIVE_READING,
        )
    )

    # A deck whose fields never made it onto a template still has to produce a card, and a
    # mapping with an empty side cannot validate. Fall back to "one field asks, another
    # answers" using whatever content fields are left over.
    def _fill(role: Role, partner_role: Role, pick_last: bool) -> None:
        if assigned.get(role) or assigned.get(partner_role):
            return
        used = set(assigned.values())
        spare = [n for n in names if n not in used and signals[n].is_content]
        if spare:
            assigned[role] = spare[-1] if pick_last else spare[0]

    _fill(Role.TARGET_TERM, Role.TARGET_SENTENCE, pick_last=True)
    _fill(Role.NATIVE_TERM, Role.NATIVE_SENTENCE, pick_last=False)

    bindings = {
        name: FieldBinding(name=name, ord=o, hidden=signals[name].is_bookkeeping)
        for o, name in ordered
    }
    mapping = RoleMapping(
        notetype_name=notetype_name,
        fields=[bindings[name] for name in names],
        target_language=target_language,
        native_language=native_language,
    )
    for role, field_name in assigned.items():
        mapping.bind(role, bindings[field_name])

    # Audio is policy, not detection: pre-existing audio becomes native (kept, hidden) and
    # only a field this addon generated is ever a target audio field.
    loose_audio = {name for name, signal in signals.items() if signal.has_sound}
    return resolve_audio_fields(mapping, sound_fields=loose_audio)
