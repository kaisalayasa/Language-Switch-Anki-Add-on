"""Checks the model's parsed output for the concrete ways it has actually been observed to go
wrong, before anything reaches the user's preview.

Each check here maps to a real failure captured while testing this prompt against Qwen2.5-7B
(see the LLM overhaul commit history and ``docs/llm-notes.md``), not a hypothetical:

- Hallucinated field references (the German deck once produced a Back template referencing
  ``{{stellen}}``, ``{{stehen}}``, etc. -- fictional per-word fields that don't exist on the
  real notetype).
- Unbalanced conditionals (a Korean-deck run once left ``{{^Picture}}`` open with no matching
  ``{{/Picture}}``).
- A given field placed on the wrong side (the same field-name-collision deck, run to run,
  sometimes inverts the given placement outright).

This module does not fix anything or retry -- it only reports problems. Deciding what to do
about them (re-prompt with the specific problem described, give up after N attempts, surface to
the user) is ``analyze.py``'s job, not yet built. Audio safety is deliberately not re-checked
here: :func:`addon.llm.audio_safety.enforce_audio_safety` already deterministically *rewrites*
any leak rather than just flagging one, so by the time validation runs the invariant already
holds by construction, not by having passed a check.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import List, Sequence

from .response import ParsedResponse
from .template_fields import referenced_fields

__all__ = ["ValidationProblem", "validate_response"]

_COND_TAG_RE = re.compile(r"\{\{([#^/])([^}]+)\}\}")


@dataclass(frozen=True)
class ValidationProblem:
    #: Short machine-readable identifier, e.g. "unknown_field" -- lets a future retry loop
    #: react differently to different problem kinds without parsing ``message``.
    code: str
    message: str


def validate_response(
    parsed: ParsedResponse,
    *,
    known_field_names: Sequence[str],
    new_front_fields: Sequence[str],
    new_back_fields: Sequence[str],
) -> List[ValidationProblem]:
    """Return every problem found; an empty list means the response is safe to show the user."""
    problems: List[ValidationProblem] = []
    all_known = set(known_field_names)
    front_refs = set(referenced_fields(parsed.front))
    back_refs = set(referenced_fields(parsed.back))

    unknown = (front_refs | back_refs) - all_known
    if unknown:
        problems.append(ValidationProblem(
            "unknown_field",
            "Front/Back reference field name(s) that do not exist on this notetype: %s"
            % sorted(unknown),
        ))

    problems.extend(_conditional_balance_problems("front", parsed.front))
    problems.extend(_conditional_balance_problems("back", parsed.back))

    if not referenced_fields(_strip_conditional_blocks(parsed.front)):
        problems.append(ValidationProblem(
            "empty_front",
            "Front template has no field reference outside a conditional block -- it could "
            "render completely empty",
        ))

    leaked_back_on_front = set(new_back_fields) & front_refs
    if leaked_back_on_front:
        problems.append(ValidationProblem(
            "misplaced_field",
            "given BACK field(s) referenced on the FRONT template: %s" % sorted(leaked_back_on_front),
        ))

    leaked_front_on_back = set(new_front_fields) & back_refs
    if leaked_front_on_back:
        problems.append(ValidationProblem(
            "misplaced_field",
            "given FRONT field(s) referenced directly on the BACK template (not via {{FrontSide}}): %s"
            % sorted(leaked_front_on_back),
        ))

    return problems


def _strip_conditional_blocks(html: str) -> str:
    """Remove every ``{{#X}}...{{/X}}``/``{{^X}}...{{/X}}`` block, nested ones included,
    leaving only the text that renders unconditionally. Used to find whether at least one real
    field reference survives outside any conditional (hard rule 1)."""
    kept: List[str] = []
    depth = 0
    last_end = 0
    for match in _COND_TAG_RE.finditer(html):
        sigil = match.group(1)
        if depth == 0:
            kept.append(html[last_end:match.start()])
        if sigil in ("#", "^"):
            depth += 1
        elif sigil == "/":
            depth = max(0, depth - 1)
        last_end = match.end()
    if depth == 0:
        kept.append(html[last_end:])
    return "".join(kept)


def _conditional_balance_problems(section_name: str, html: str) -> List[ValidationProblem]:
    stack: List[str] = []
    problems: List[ValidationProblem] = []
    for sigil, raw_name in _COND_TAG_RE.findall(html):
        name = raw_name.strip()
        if sigil in ("#", "^"):
            stack.append(name)
        else:  # "/"
            if not stack:
                problems.append(ValidationProblem(
                    "unbalanced_conditional",
                    "%s template: {{/%s}} has no matching opening {{#%s}} or {{^%s}}"
                    % (section_name, name, name, name),
                ))
            elif stack[-1] != name:
                problems.append(ValidationProblem(
                    "unbalanced_conditional",
                    "%s template: {{/%s}} does not close the innermost open block {{%s}}"
                    % (section_name, name, stack[-1]),
                ))
                stack.pop()
            else:
                stack.pop()
    for name in stack:
        problems.append(ValidationProblem(
            "unbalanced_conditional",
            "%s template: {{#%s}} or {{^%s}} is never closed" % (section_name, name, name),
        ))
    return problems
