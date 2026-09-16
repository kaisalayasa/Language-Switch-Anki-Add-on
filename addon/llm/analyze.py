"""Orchestrates one full "analyze this deck" call: resolve direction, build the prompt, call the
model, parse and validate its reply, retry on a validation failure, append the (deterministic,
never AI-authored) generated-audio references, and attach a trust rating the user can see before
ever touching the preview.

**The trust rating is computed here, not asked of the model.** Same principle as direction and
audio safety: a number the model reports about its own reliability would be exactly the kind of
ungrounded confidence this project has been actively removing. What's actually observable and
deterministic is how much trouble the real validation pipeline had -- how many attempts it took
to produce a response with zero problems from :func:`~addon.llm.validate.validate_response`, or
whether it never did. That is the "test" the rating is based on:

- Passed with zero problems on the first attempt: 5 stars.
- Needed one retry: 4 stars.
- Needed every retry allowed: 3 stars.
- Never passed, even after every retry: 1 star.

Below 5 stars, ``DeckAnalysis.review_message`` is populated with a plain-language nudge to look
closely at the preview -- and at 1 star specifically, to consider hand-editing the templates
rather than trusting them, since the result being returned at that point is simply the last
attempt's output, unresolved problems and all, never silently discarded.

**Generated-audio field references are never part of what the model writes or what validation
checks.** ``resolve_direction`` already decides, deterministically, which front fields get their
own audio field (see ``direction.py``'s module docstring); this module appends the
``{{#field}}{{field}}{{/field}}`` reference for each one directly onto the model's (validated)
Front HTML, after every retry loop has already finished -- so there is nothing left for the model
to get wrong about audio placement, and nothing for validation to need to check there either.

Known limitation, not folded into the rating (kept out deliberately, to match what was actually
asked for): a deck whose language detection came back uncertain (``direction.py``'s
``target_language``/``native_language`` as ``"?"``) does not lower the star count, even though
that is itself a real source of risk. Worth revisiting if it turns out to matter in practice.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, replace
from typing import Callable, List, Optional, Sequence, Tuple

from ..core.deck_state import ConversionState
from .audio_safety import enforce_audio_safety, sound_field_names
from .direction import AudioTarget, resolve_direction
from .prompt import FieldSample, PromptInput, build_prompt
from .response import ParsedResponse, ResponseParseError, parse_response
from .validate import ValidationProblem, validate_response

__all__ = [
    "DeckAnalysis", "CallModelFn", "MAX_ATTEMPTS", "analyze_deck", "strip_pending_audio_html",
]

#: One initial attempt plus up to two retries. Matches the plan's "re-prompt once or twice"
#: from the original design doc.
MAX_ATTEMPTS = 3

#: attempts-used -> stars, when the attempt ultimately passed validation. An attempt count past
#: what's mapped here (only possible if a caller raises max_attempts) falls back to 1 star --
#: needing that many tries is not a case to reward just because it's unmapped.
_STARS_BY_ATTEMPTS_USED = {1: 5, 2: 4, 3: 3}

#: Given directly to the model on a retry: the callable never sees this text.
CallModelFn = Callable[[str, str], str]


@dataclass(frozen=True)
class DeckAnalysis:
    description: str
    front: str
    back: str
    css: str
    target_language: str
    native_language: str
    #: The given field placement direction.py computed -- not re-derived from the returned
    #: front/back HTML, so this stays accurate even when the model dropped a field the
    #: placement allowed it to omit (e.g. a bookkeeping field), which a UI showing "what was
    #: decided" should still reflect faithfully.
    new_front_fields: Tuple[str, ...]
    new_back_fields: Tuple[str, ...]
    #: One (source_field, audio_field) pair per real front-content field -- see direction.py.
    #: ``front`` above already has each one's reference appended.
    audio_targets: Tuple[AudioTarget, ...]
    trust_stars: int
    attempts_used: int
    unresolved_problems: Tuple[ValidationProblem, ...]
    #: The last raw model reply, kept for troubleshooting even on a clean pass -- especially
    #: valuable on total failure, where front/back/css below may be empty.
    last_raw_response: str
    #: ``None`` at 5 stars. Plain-language, ready to show in the UI as-is.
    review_message: Optional[str]


def analyze_deck(
    *,
    deck_name: str,
    notetype_name: str,
    fields: Sequence[FieldSample],
    qfmt: str,
    afmt: str,
    css: str,
    call_model_fn: CallModelFn,
    max_attempts: int = MAX_ATTEMPTS,
    known_state: Optional[ConversionState] = None,
) -> DeckAnalysis:
    """Run the full analyze pipeline for one notetype and return a :class:`DeckAnalysis`.

    ``call_model_fn`` is ``(system_prompt, user_prompt) -> raw response text`` -- a thin,
    caller-built wrapper around :func:`addon.llm.client.call_model` with the runtime/model
    paths already bound (e.g. via :func:`functools.partial`), so this module knows nothing
    about subprocess details and stays trivially testable with a canned function.

    ``known_state``, when the caller has one (``core.deck_state.state_from_notetype`` on the
    currently selected notetype), is passed straight through to :func:`~.direction.resolve_direction`
    so re-analyzing an already-converted notetype is idempotent instead of flipping it back to
    its original direction -- see that function's docstring for why.
    """
    direction = resolve_direction(fields, qfmt, afmt, known_state=known_state)
    audio_fields = sound_field_names(fields)
    known_field_names = [f.name for f in fields]

    # Only fields resolve_direction actually placed somewhere -- never a field it excluded
    # (a not-yet-existing generated-audio field, or one that isn't shown anywhere on the
    # *original* card, e.g. a deck's own bookkeeping/index fields; see direction.py). The
    # model has no way to reference an excluded field's name if it's never shown its content
    # at all, closing what would otherwise be a real gap: validate.py only checks a reference
    # against the given FRONT/BACK lists, so a field in neither would go unchecked.
    placed = set(direction.new_front_fields) | set(direction.new_back_fields)
    visible_fields = tuple(f for f in fields if f.name in placed)

    prompt_input = PromptInput(
        deck_name=deck_name,
        notetype_name=notetype_name,
        fields=visible_fields,
        new_front_fields=direction.new_front_fields,
        new_back_fields=direction.new_back_fields,
        current_css=css,
    )
    system_prompt, base_user_prompt = build_prompt(prompt_input)

    user_prompt = base_user_prompt
    problems: List[ValidationProblem] = []
    safe_parsed: Optional[ParsedResponse] = None
    raw_response = ""

    for attempt in range(1, max_attempts + 1):
        raw_response = call_model_fn(system_prompt=system_prompt, user_prompt=user_prompt)
        try:
            parsed = parse_response(raw_response)
        except ResponseParseError:
            problems = [ValidationProblem(
                "parse_error",
                "Your previous response did not follow the required output format (missing "
                "or out-of-order --- ANALYSIS/FRONT/BACK/CSS --- markers, or invalid JSON in "
                "ANALYSIS). Follow the exact output format given in the system prompt.",
            )]
            user_prompt = _retry_prompt(base_user_prompt, problems)
            continue

        safe_parsed = replace(
            parsed,
            front=enforce_audio_safety(parsed.front, sound_fields=audio_fields),
            back=enforce_audio_safety(parsed.back, sound_fields=audio_fields),
        )
        problems = validate_response(
            safe_parsed,
            known_field_names=known_field_names,
            new_front_fields=direction.new_front_fields,
            new_back_fields=direction.new_back_fields,
        )
        if not problems:
            stars = _STARS_BY_ATTEMPTS_USED.get(attempt, 1)
            return DeckAnalysis(
                description=safe_parsed.description,
                front=_append_audio_html(safe_parsed.front, direction.audio_targets),
                back=safe_parsed.back,
                css=safe_parsed.css,
                target_language=direction.target_language,
                native_language=direction.native_language,
                new_front_fields=direction.new_front_fields,
                new_back_fields=direction.new_back_fields,
                audio_targets=direction.audio_targets,
                trust_stars=stars,
                attempts_used=attempt,
                unresolved_problems=(),
                last_raw_response=raw_response,
                review_message=_review_message(stars, attempt, ()),
            )
        user_prompt = _retry_prompt(base_user_prompt, problems)

    # Every attempt used and still failing -- return the last attempt's own output rather than
    # discard it silently, but unmistakably flagged: 1 star, and the unresolved problems attached.
    return DeckAnalysis(
        description=safe_parsed.description if safe_parsed else "",
        front=_append_audio_html(safe_parsed.front, direction.audio_targets) if safe_parsed else "",
        back=safe_parsed.back if safe_parsed else "",
        css=safe_parsed.css if safe_parsed else "",
        target_language=direction.target_language,
        native_language=direction.native_language,
        new_front_fields=direction.new_front_fields,
        new_back_fields=direction.new_back_fields,
        audio_targets=direction.audio_targets,
        trust_stars=1,
        attempts_used=max_attempts,
        unresolved_problems=tuple(problems),
        last_raw_response=raw_response,
        review_message=_review_message(1, max_attempts, tuple(problems)),
    )


def _append_audio_html(front: str, audio_targets: Sequence[AudioTarget]) -> str:
    """Append one ``{{#field}}{{field}}{{/field}}`` reference per audio target to ``front``.

    Deterministic, not model-authored -- see module docstring. Always wrapped in a conditional
    so a not-yet-synthesized (empty) audio field renders as nothing rather than a broken player,
    matching what the model used to be told to do by hand for the single field this replaces.
    """
    if not audio_targets:
        return front
    snippets = "".join(
        "{{#%s}}{{%s}}{{/%s}}" % (t.audio_field, t.audio_field, t.audio_field)
        for t in audio_targets
    )
    return front.rstrip() + "\n" + snippets


def strip_pending_audio_html(front: str, pending_audio_fields: Sequence[str]) -> str:
    """The inverse of :func:`_append_audio_html`, for previewing ``front`` before Convert has
    created the audio field(s) it references.

    ``pending_audio_fields`` names generated-audio fields that don't exist on the live
    notetype yet. Each one's whole ``{{#field}}...{{/field}}`` block is guaranteed to render as
    nothing regardless -- the field either doesn't exist yet, or (once Convert creates it)
    starts empty and stays that way until TTS runs -- but rendering the reference against a
    note bound to the real, not-yet-converted notetype makes Anki's own template compiler treat
    it as an unknown field and error out, rather than silently rendering blank (Anki's
    ``ephemeral_card(custom_note_type=...)`` does not override which fields are considered to
    exist for this check -- verified against the real ``anki`` package source; see
    ``docs/api-notes.md``). Stripping the block for preview purposes only produces the exact
    same visible result as leaving it in would if it worked, without touching what actually
    gets saved on Convert.
    """
    out = front
    for field in pending_audio_fields:
        pattern = re.compile(
            r"\{\{#%s\}\}.*?\{\{/%s\}\}" % (re.escape(field), re.escape(field)), re.DOTALL
        )
        out = pattern.sub("", out)
    return out


def _retry_prompt(base_user_prompt: str, problems: Sequence[ValidationProblem]) -> str:
    problem_lines = "\n".join("- %s" % p.message for p in problems)
    return (
        base_user_prompt.rstrip()
        + "\n\nYour previous attempt had the following problem(s). Fix ALL of them this time:\n"
        + problem_lines
        + "\n"
    )


def _review_message(
    stars: int, attempts_used: int, unresolved: Tuple[ValidationProblem, ...]
) -> Optional[str]:
    if stars >= 5:
        return None
    if unresolved:
        return (
            "The AI could not produce a fully valid result after %d attempts. Review the "
            "Front/Back/CSS carefully in the preview, and consider hand-editing them yourself "
            "before converting." % attempts_used
        )
    return (
        "The AI needed %d retry attempt(s) to get this deck right. It passed every check on "
        "the final attempt, but you may want to double-check the preview before converting."
        % (attempts_used - 1)
    )
