"""Builds the one prompt this addon sends to the model.

Per ``docs/llm-notes.md``, the model call is one shot: given a notetype's fields, real sample
content, its current CSS, and an ALREADY-DECIDED field placement, produce a plain-language
description plus the finished Front/Back/CSS templates for that placement -- not a role mapping
for other code to turn into HTML, and not a direction decision for the model to work out itself.

**Direction, target/native language, and field placement are computed by
:func:`~addon.llm.direction.resolve_direction`, never asked of the model.** Four independently
phrased attempts to get the model to execute "whichever fields are on the current back move to
the new front" all failed identically -- see ``direction.py``'s module docstring for the full
account, including the field-naming collision (a deck's field literally named "Front"/"Back")
that broke it hardest. The model's job here is narrowed to two things: pick which given front
field to speak aloud (``speak_text_from``), and write templates that honor a placement it is
simply told, never asked to derive. **This module is the product for that narrowed job** -- if the
model gets the template HTML wrong, the fix is more detail in ``_SYSTEM_PROMPT``, never a bigger
model (the user's explicit call) and never a hand-written fallback that re-derives the deleted
heuristic pipeline.

This module is exempt from ``addon/core``'s no-language-name rule (see ``addon/llm/__init__.py``
and ``tests/test_purity.py``): describing languages is the prompt's entire job. What it must not
do is name a specific *deck* -- the worked example below uses a generic, made-up Spanish/English
vocabulary pair purely to demonstrate the OUTPUT FORMAT, not as a special case the real prompt
logic depends on. Every rule above the worked example is stated in general terms that hold for
any two-language deck.

**Audio safety is deliberately not left to the model's judgment.** The first real test of this
prompt (see ``docs/llm-notes.md``) showed the model failing to notice a field mixing real text
with embedded ``[sound:...]`` audio and referencing it as a bare ``{{Field}}`` -- which would
play the deck's own, original-direction audio on the converted card. Rather than asking a small
model to reliably detect that from raw sample text, detection happens deterministically in
Python (:func:`~addon.llm.audio_safety.sound_field_names`) *before* the prompt is built: the
samples shown to the model have ``[sound:...]`` already stripped out (so the noise it would have
had to parse and reason about isn't there in the first place), and the affected field names are
handed to the model directly as a given fact, not something to infer. ``audio_safety`` is applied
again, deterministically, to whatever the model actually writes -- so an instruction the model
ignores still can't reach a real card. Pre-sanitizing is the primary defense; the post-hoc pass is
the guarantee behind it.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Tuple

from .audio_safety import SOUND_TAG_RE, sound_field_names

__all__ = [
    "ANALYSIS_MARKER",
    "FRONT_MARKER",
    "BACK_MARKER",
    "CSS_MARKER",
    "FieldSample",
    "PromptInput",
    "build_prompt",
]

#: Output section markers. Exact literal lines the model is instructed to emit and
#: ``llm/response.py`` will split on. Kept as module constants so prompt and parser can never
#: drift apart silently.
ANALYSIS_MARKER = "--- ANALYSIS ---"
FRONT_MARKER = "--- FRONT ---"
BACK_MARKER = "--- BACK ---"
CSS_MARKER = "--- CSS ---"

#: Defensive caps on what actually gets rendered into the prompt, independent of how much a
#: caller passes in -- keeps prompt size bounded regardless of a deck's real content length.
_MAX_SAMPLES_PER_FIELD = 5
_MAX_SAMPLE_LEN = 160


@dataclass(frozen=True)
class FieldSample:
    """One field's name plus a few **raw** (unsanitized) sample values.

    Raw on purpose: an embedded ``[sound:...]`` reference or HTML is itself a signal the model
    needs to see (rules 3-5 in the system prompt depend on being able to tell a field apart from
    one that only *looks* like content).
    """

    name: str
    samples: Tuple[str, ...]


@dataclass(frozen=True)
class PromptInput:
    """Everything the model needs to write templates for one notetype.

    ``new_front_fields``/``new_back_fields`` are the ALREADY-DECIDED field placement -- the
    output of :func:`~addon.llm.direction.resolve_direction`, computed before this prompt is
    ever built, not something the model works out. Likewise ``audio_field_name`` names the field
    the conversion itself creates for newly-generated audio (the same "conversion creates an
    empty field, TTS fills it later" policy the old ``audio_fields.py`` used). The model is told
    both as given facts; its job is narrowed to picking which given front field to speak, and
    writing HTML/CSS for a placement it is handed -- never deciding the placement or the
    languages involved itself.
    """

    deck_name: str
    notetype_name: str
    fields: Tuple[FieldSample, ...]
    new_front_fields: Tuple[str, ...]
    new_back_fields: Tuple[str, ...]
    current_css: str
    audio_field_name: str


def build_prompt(data: PromptInput) -> Tuple[str, str]:
    """Return ``(system_prompt, user_prompt)`` for this notetype."""
    return _SYSTEM_PROMPT, _render_user_prompt(data)


def _truncate(text: str, limit: int) -> str:
    text = text if text is not None else ""
    if len(text) <= limit:
        return text
    return text[:limit].rstrip() + "…"


def _clean_for_display(text: str) -> str:
    """Strip ``[sound:...]`` markup before a sample is shown to the model.

    Not a safety mechanism by itself -- ``audio_safety.enforce_audio_safety`` is what actually
    guarantees old audio can't play. This just keeps audio markup out of the text the model has
    to read and reason about, since that reasoning isn't its job any more (see the module
    docstring): a field that strips down to nothing is itself the correct signal that it's
    audio-only, nothing lost by removing the markup rather than leaving it as noise.
    """
    return SOUND_TAG_RE.sub("", text or "").strip()


def _render_field(field: FieldSample) -> str:
    shown = field.samples[:_MAX_SAMPLES_PER_FIELD]
    if not shown:
        return "- %s: (no samples)" % field.name
    lines = ["- %s:" % field.name]
    for sample in shown:
        cleaned = _clean_for_display(sample)
        lines.append("    %r" % _truncate(cleaned, _MAX_SAMPLE_LEN))
    return "\n".join(lines)


def _render_user_prompt(data: PromptInput) -> str:
    fields_block = "\n".join(_render_field(f) for f in data.fields) or "(no fields)"
    audio_fields = sorted(sound_field_names(data.fields))
    audio_fields_block = ", ".join(audio_fields) if audio_fields else "(none)"
    new_front_block = ", ".join(data.new_front_fields) if data.new_front_fields else "(none)"
    new_back_block = ", ".join(data.new_back_fields) if data.new_back_fields else "(none)"
    return """## Deck to convert

Deck name: %s
Notetype name: %s

Fields (in order), with real sample values. [sound:...] audio markup has already been removed
from the samples below -- you do not need to look for it yourself:
%s

Fields that already contain their OWN pre-existing audio (this has already been determined for
you): %s
If you reference one of these fields' text anywhere in your templates, you MUST use
{{text:FieldName}}, never a bare {{FieldName}} -- see hard rule 3.

New FRONT fields (given -- place these on the Front): %s
New BACK fields (given -- place these on the Back): %s
This placement has already been decided for you. Do not move a field to the other side, and do
not use a field's NAME to second-guess it -- a field can be named "Front" or "Back" while
actually belonging on either given side; go strictly by the two lists above.

Current CSS (context and style only):
%s

The empty field reserved for new audio: %s
""" % (
        data.deck_name,
        data.notetype_name,
        fields_block,
        audio_fields_block,
        new_front_block,
        new_back_block,
        data.current_css,
        data.audio_field_name,
    )


_SYSTEM_PROMPT = """You are an expert Anki card template author. You are given a deck's fields, \
real sample content, and the EXACT field placement for the converted card -- which fields go on \
the new Front and which go on the new Back has already been decided for you. Your only job: \
pick which field to speak aloud, and write the Front/Back/CSS templates.

## Anki template syntax (this is the complete syntax you may use)

- `{{FieldName}}` -- inserts that field's content, HTML included, verbatim.
- `{{text:FieldName}}` -- inserts that field's content as PLAIN TEXT: HTML tags and special \
references (audio, images) are stripped. Use this instead of a bare `{{FieldName}}` whenever a \
field might contain audio you do not want to play.
- `{{#FieldName}}...{{/FieldName}}` -- shown only if FieldName is non-empty on a note.
- `{{^FieldName}}...{{/FieldName}}` -- shown only if FieldName IS empty.
- `{{FrontSide}}` -- only usable inside the BACK template. Every Back template must start with it.
- `<hr id=answer>` -- the conventional divider, placed immediately after `{{FrontSide}}`.
- You may only reference a field name that is EXACTLY one of the field names you were given.

## Hard rules

1. The Front template must contain at least one field reference NOT wrapped in a conditional.
2. Copy the given CSS forward exactly as the start of your new CSS, then append new rules.
3. You are told below exactly which fields already contain their own pre-existing audio. For \
those, if you reference their text, use {{text:FieldName}}, never a bare {{FieldName}}.
4. Reference the given "field for new audio" as a BARE reference ({{FieldName}}), never \
{{text:FieldName}}. It is currently empty -- you may wrap it in a conditional.
5. Never reference any OTHER field that already contains audio as a bare {{FieldName}}.
6. Put every given "new front" field somewhere on the Front, and every given "new back" field \
somewhere on the Back -- these placements are decided, not yours to change. A field with no \
real content on most notes (an internal index or ID -- judge this from its sample values) may \
be given little or no visual weight, but every OTHER field with real content should still be \
visible somewhere on its given side.
7. speak_text_from must be one of the given "new front" fields -- prefer a full \
example-sentence field over a single word/term if more than one front field looks like real \
spoken content.

## Output format

Respond with EXACTLY these four sections, in this order, each starting with its own marker line \
and nothing else on that line. Do not write anything before "--- ANALYSIS ---" or after the CSS. \
Do not wrap anything in markdown code fences.

--- ANALYSIS ---
{
  "description": "<one or two plain-language sentences: what this deck is, and how it's being converted -- name the actual languages involved>",
  "speak_text_from": "<exact name of one of the given new-FRONT fields>"
}
--- FRONT ---
<the new Front template HTML>
--- BACK ---
<the new Back template HTML>
--- CSS ---
<the new CSS, starting with the original CSS verbatim>

## Worked example

Given fields Word, Translation, ExampleSentence, new FRONT fields (given): Word, \
ExampleSentence, new BACK fields (given): Translation, current CSS \
`.card { font-size: 20px; }`, field for new audio: ddc-audio -- a correct response looks like:

--- ANALYSIS ---
{
  "description": "A Spanish vocabulary deck, converting to Spanish-front/English-back so the learner practices producing Spanish.",
  "speak_text_from": "ExampleSentence"
}
--- FRONT ---
<div class="word">{{Word}}</div>
{{#ExampleSentence}}<div class="sentence">{{ExampleSentence}}</div>{{/ExampleSentence}}
{{#ddc-audio}}{{ddc-audio}}{{/ddc-audio}}
--- BACK ---
{{FrontSide}}<hr id=answer>
<div class="translation">{{Translation}}</div>
--- CSS ---
.card { font-size: 20px; }
.word { font-size: 32px; font-weight: 600; }
.sentence { font-size: 18px; margin-top: 8px; }
.translation { font-size: 28px; }

IMPORTANT: "Word", "Translation", "ExampleSentence" above are made-up names for illustration \
only. Every {{FieldName}} in your real answer must be copied character-for-character from the \
real deck's Fields list below -- never from this example.

Now do the same for the real deck given below.
"""
