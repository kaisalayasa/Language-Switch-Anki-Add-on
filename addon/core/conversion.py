"""Conversion planning -- what a run *will* do, decided before anything is written.

Pure and Anki-free, so the risky decisions (which notes are in scope, which mode is safe,
what gets destroyed) are unit-testable without a collection. ``addon/ops/notetype_manager``
executes a plan; it does not decide one.

Two modes, per ``claude.md``:

``FLIP_IN_PLACE``
    Clone the notetype, rewrite its template, then **repoint** the existing notes onto the
    clone. The original-direction cards are replaced.
``NEW_DECK``
    Clone the notetype, rewrite its template, then **duplicate** the notes onto the clone
    in a brand-new deck. The original deck, notetype and notes are untouched.

Scheduling is reset in both modes, unconditionally.

A plan carries plain ``front``/``back``/``css`` strings rather than a role mapping -- the LLM
overhaul's model produces finished template HTML directly (see ``addon/llm/analyze.py``'s
``DeckAnalysis``), so there is no mapping left for this module to validate or apply. This module
deliberately does not import anything from ``addon/llm/`` -- that package already depends on
``addon/core/`` (``direction.py`` uses ``core.language_detect``), and a dependency back the other
way would make a cycle. The caller (``ui/main_screen.py``) unpacks a ``DeckAnalysis`` into
:func:`build_plan`'s plain arguments instead.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import List, Optional

__all__ = [
    "ConversionMode",
    "ConversionPlan",
    "PreflightSummary",
    "ValidationError",
    "ValidationResult",
    "build_plan",
    "escape_search_term",
    "scope_query",
]


class ValidationError(Exception):
    """Raised when a plan cannot safely be applied."""


@dataclass
class ValidationResult:
    errors: List[str] = field(default_factory=list)
    warnings: List[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.errors

    def raise_if_failed(self) -> None:
        if self.errors:
            raise ValidationError("; ".join(self.errors))


class ConversionMode(Enum):
    FLIP_IN_PLACE = "flip_in_place"
    NEW_DECK = "new_deck"

    @property
    def is_destructive(self) -> bool:
        """True when the run changes the notes the user already has."""
        return self is ConversionMode.FLIP_IN_PLACE

    @property
    def label(self) -> str:
        return {
            ConversionMode.FLIP_IN_PLACE: "Flip in place",
            ConversionMode.NEW_DECK: "New deck (non-destructive)",
        }[self]


def escape_search_term(value: str) -> str:
    r"""Escape a value for use inside an Anki search string.

    Deck and notetype names routinely contain spaces, and may contain ``"``, ``*``, ``_``
    or ``\`` -- all of which mean something to the search parser. Getting this wrong would
    silently widen or narrow the set of notes we then write to, which is the one mistake
    with collection-wide blast radius.
    """
    out = []
    for ch in value:
        if ch in ('"', "*", "_", "\\", "(", ")", ":", "-"):
            out.append("\\" + ch)
        else:
            out.append(ch)
    return "".join(out)


def scope_query(notetype_name: str, deck_name: str) -> str:
    """The search that defines exactly which notes a run may touch.

    Scoped by **both** notetype and deck. A notetype can be shared by several decks, and a
    deck can hold several notetypes; either alone would be too broad.
    """
    return 'note:"%s" deck:"%s"' % (
        escape_search_term(notetype_name),
        escape_search_term(deck_name),
    )


@dataclass
class PreflightSummary:
    """Exactly what the user is agreeing to. Nothing is written before this is shown."""

    mode: ConversionMode
    source_notetype: str
    source_deck: str
    clone_notetype: str
    target_deck: str
    note_count: int
    scheduling_will_reset: bool = True
    media_cleanup: bool = False
    new_fields: List[str] = field(default_factory=list)
    warnings: List[str] = field(default_factory=list)

    def lines(self) -> List[str]:
        out = [
            "Mode:              %s" % self.mode.label,
            "Source notetype:   %s" % self.source_notetype,
            "Source deck:       %s" % self.source_deck,
            "Notes in scope:    %d" % self.note_count,
            "New notetype:      %s" % self.clone_notetype,
            "Cards land in:     %s" % self.target_deck,
        ]
        if self.mode is ConversionMode.FLIP_IN_PLACE:
            out.append(
                "Effect:            the %d existing notes move onto the new notetype; "
                "their current cards are replaced" % self.note_count
            )
        else:
            out.append(
                "Effect:            %d copies are created; the original deck, notetype "
                "and notes are untouched" % self.note_count
            )
        out.append(
            "Scheduling:        %s"
            % ("WILL BE RESET (all cards become new)" if self.scheduling_will_reset else "kept")
        )
        out.append(
            "Media cleanup:     %s" % ("yes" if self.media_cleanup else "no (nothing deleted)")
        )
        if self.new_fields:
            out.append(
                "New fields:        %s (added empty, on the new notetype only; generated "
                "audio is written here so existing audio is kept and hidden rather than "
                "overwritten)" % ", ".join(self.new_fields)
            )
        return out

    def as_text(self) -> str:
        body = "\n".join(self.lines())
        if self.warnings:
            body += "\n\nWarnings:\n" + "\n".join("  - %s" % w for w in self.warnings)
        return body


@dataclass
class ConversionPlan:
    mode: ConversionMode
    front: str
    back: str
    css: str
    #: The two languages this conversion records -- written into the notetype's css and onto
    #: every note's tags by ``core.deck_state``, so reopening the deck later reads the
    #: direction back rather than re-deriving it. See ``core/deck_state.py``.
    target_language: str
    native_language: str
    source_notetype: str
    source_deck: str
    clone_notetype: str
    target_deck: str
    template_name: str = "Production"
    note_ids: List[int] = field(default_factory=list)
    strip_tags: List[str] = field(default_factory=lambda: ["leech"])
    reset_scheduling: bool = True
    media_cleanup: bool = False
    dry_run: bool = False
    #: Field names to add to the clone that the source notetype does not have -- in practice
    #: the audio field generated TTS is written into, per ``core.audio_fields``. Appended
    #: after the source's own fields, never inserted among them, and always empty to begin
    #: with. Empty is the point: the demoted language's audio is left in its original field
    #: and hidden rather than overwritten, so a card can never play the wrong language while
    #: waiting for its audio to be generated.
    new_fields: List[str] = field(default_factory=list)

    @property
    def scope_query(self) -> str:
        return scope_query(self.source_notetype, self.source_deck)

    def validate(self) -> ValidationResult:
        result = ValidationResult()

        if not self.front.strip():
            result.errors.append("the front template is empty")
        if not self.back.strip():
            result.errors.append("the back template is empty")
        if not self.clone_notetype.strip():
            result.errors.append("the new notetype needs a name")
        if self.clone_notetype == self.source_notetype:
            result.errors.append(
                "the new notetype must not reuse the source's name (%r) -- the original "
                "notetype is never modified" % self.source_notetype
            )
        if self.mode is ConversionMode.NEW_DECK:
            if self.target_deck == self.source_deck:
                result.errors.append(
                    "new-deck mode must write to a different deck than %r"
                    % self.source_deck
                )
            if self.media_cleanup:
                result.errors.append(
                    "media cleanup must never run in new-deck mode: the original notes "
                    "still reference those files"
                )
        if not self.reset_scheduling:
            result.errors.append(
                "scheduling reset is not optional -- the old review history describes a "
                "direction the learner never practised"
            )
        if not self.note_ids:
            result.warnings.append("no notes matched %s" % self.scope_query)
        return result

    def preflight(self, warnings: Optional[List[str]] = None) -> PreflightSummary:
        summary = PreflightSummary(
            mode=self.mode,
            source_notetype=self.source_notetype,
            source_deck=self.source_deck,
            clone_notetype=self.clone_notetype,
            target_deck=self.target_deck,
            note_count=len(self.note_ids),
            scheduling_will_reset=self.reset_scheduling,
            media_cleanup=self.media_cleanup,
            new_fields=list(self.new_fields),
        )
        summary.warnings.extend(self.validate().warnings)
        summary.warnings.extend(warnings or [])
        return summary


def build_plan(
    *,
    mode: ConversionMode,
    front: str,
    back: str,
    css: str,
    target_language: str,
    native_language: str,
    source_notetype: str,
    source_deck: str,
    note_ids: Optional[List[int]] = None,
    clone_notetype: Optional[str] = None,
    target_deck: Optional[str] = None,
    template_name: str = "Production",
    suffix: str = "English Front",
    dry_run: bool = False,
) -> ConversionPlan:
    """Assemble a plan, filling in sensible names.

    ``suffix`` is caller-supplied so no language name is baked in here.
    """
    clone = clone_notetype or "%s (%s)" % (source_notetype, suffix)
    if mode is ConversionMode.NEW_DECK:
        deck = target_deck or "%s (%s)" % (source_deck, suffix)
    else:
        deck = target_deck or source_deck
    return ConversionPlan(
        mode=mode,
        front=front,
        back=back,
        css=css,
        target_language=target_language,
        native_language=native_language,
        source_notetype=source_notetype,
        source_deck=source_deck,
        clone_notetype=clone,
        target_deck=deck,
        template_name=template_name,
        note_ids=list(note_ids or []),
        dry_run=dry_run,
    )
