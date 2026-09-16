"""Orchestrates one full "analyze this deck" call: resolve direction, build the prompt, call the
model, parse and validate its reply, retry on a validation failure, and attach a trust rating
the user can see before ever touching the preview.

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

Known limitation, not folded into the rating (kept out deliberately, to match what was actually
asked for): a deck whose language detection came back uncertain (``direction.py``'s
``target_language``/``native_language`` as ``"?"``) does not lower the star count, even though
that is itself a real source of risk. Worth revisiting if it turns out to matter in practice.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from typing import Callable, List, Optional, Sequence, Tuple

from .audio_safety import enforce_audio_safety, sound_field_names
from .direction import resolve_direction
from .prompt import FieldSample, PromptInput, build_prompt
from .response import ParsedResponse, ResponseParseError, parse_response
from .validate import ValidationProblem, validate_response

__all__ = ["DeckAnalysis", "CallModelFn", "MAX_ATTEMPTS", "analyze_deck"]

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
    speak_text_from: str
    front: str
    back: str
    css: str
    target_language: str
    native_language: str
    audio_field_name: str
    #: The given field placement direction.py computed -- not re-derived from the returned
    #: front/back HTML, so this stays accurate even when the model dropped a field the
    #: placement allowed it to omit (e.g. a bookkeeping field), which a UI showing "what was
    #: decided" should still reflect faithfully.
    new_front_fields: Tuple[str, ...]
    new_back_fields: Tuple[str, ...]
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
    audio_field_name: str,
    call_model_fn: CallModelFn,
    max_attempts: int = MAX_ATTEMPTS,
) -> DeckAnalysis:
    """Run the full analyze pipeline for one notetype and return a :class:`DeckAnalysis`.

    ``call_model_fn`` is ``(system_prompt, user_prompt) -> raw response text`` -- a thin,
    caller-built wrapper around :func:`addon.llm.client.call_model` with the runtime/model
    paths already bound (e.g. via :func:`functools.partial`), so this module knows nothing
    about subprocess details and stays trivially testable with a canned function.
    """
    direction = resolve_direction(fields, qfmt, afmt, audio_field_name=audio_field_name)
    audio_fields = sound_field_names(fields)
    known_field_names = [f.name for f in fields]

    prompt_input = PromptInput(
        deck_name=deck_name,
        notetype_name=notetype_name,
        fields=tuple(fields),
        new_front_fields=direction.new_front_fields,
        new_back_fields=direction.new_back_fields,
        current_css=css,
        audio_field_name=audio_field_name,
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
            front=enforce_audio_safety(parsed.front, sound_fields=audio_fields, keep=audio_field_name),
            back=enforce_audio_safety(parsed.back, sound_fields=audio_fields, keep=audio_field_name),
        )
        problems = validate_response(
            safe_parsed,
            known_field_names=known_field_names,
            new_front_fields=direction.new_front_fields,
            new_back_fields=direction.new_back_fields,
            audio_field_name=audio_field_name,
        )
        if not problems:
            stars = _STARS_BY_ATTEMPTS_USED.get(attempt, 1)
            return DeckAnalysis(
                description=safe_parsed.description,
                speak_text_from=safe_parsed.speak_text_from,
                front=safe_parsed.front,
                back=safe_parsed.back,
                css=safe_parsed.css,
                target_language=direction.target_language,
                native_language=direction.native_language,
                audio_field_name=audio_field_name,
                new_front_fields=direction.new_front_fields,
                new_back_fields=direction.new_back_fields,
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
        speak_text_from=safe_parsed.speak_text_from if safe_parsed else "",
        front=safe_parsed.front if safe_parsed else "",
        back=safe_parsed.back if safe_parsed else "",
        css=safe_parsed.css if safe_parsed else "",
        target_language=direction.target_language,
        native_language=direction.native_language,
        audio_field_name=audio_field_name,
        new_front_fields=direction.new_front_fields,
        new_back_fields=direction.new_back_fields,
        trust_stars=1,
        attempts_used=max_attempts,
        unresolved_problems=tuple(problems),
        last_raw_response=raw_response,
        review_message=_review_message(1, max_attempts, tuple(problems)),
    )


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
