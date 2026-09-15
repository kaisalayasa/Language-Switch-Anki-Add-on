"""Builds the one prompt this addon sends to the model.

Per ``docs/llm-notes.md``, the model call is one shot: given a notetype's fields, real sample
content, and its current Front/Back/CSS, produce a plain-language description plus the finished,
flipped Front/Back/CSS -- not a role mapping for other code to turn into HTML. **This module is
the product.** If the model gets something wrong, the fix is more detail in ``_SYSTEM_PROMPT``,
never a bigger model (the user's explicit call) and never a hand-written fallback that re-derives
the deleted heuristic pipeline.

This module is exempt from ``addon/core``'s no-language-name rule (see ``addon/llm/__init__.py``
and ``tests/test_purity.py``): describing languages is the prompt's entire job. What it must not
do is name a specific *deck* -- the worked example below uses a generic, made-up Spanish/English
vocabulary pair purely to demonstrate the OUTPUT FORMAT, not as a special case the real prompt
logic depends on. Every rule above the worked example is stated in general terms that hold for
any two-language deck.

**Audio safety is deliberately not left to the model's judgment.** The first real test of this
prompt (see ``docs/llm-notes.md``) showed the model failing to notice a field mixing real text
with embedded ``[sound:...]`` audio and referencing it as a bare ``{{Field}}`` -- which would
play the deck's own, original-direction audio on the converted card. Rather than asking a 1.5B
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
from typing import Sequence, Tuple

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
    """Everything the model needs to analyze and flip one notetype.

    ``audio_field_name`` is decided by deterministic code before this prompt is ever built (the
    same "conversion creates an empty field, TTS fills it later" policy the old
    ``audio_fields.py`` used) -- the model is told this name as a given fact, not asked to invent
    or choose it.
    """

    deck_name: str
    notetype_name: str
    fields: Tuple[FieldSample, ...]
    current_qfmt: str
    current_afmt: str
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

Current Front template:
%s

Current Back template:
%s

Current CSS:
%s

The empty field reserved for new audio: %s
""" % (
        data.deck_name,
        data.notetype_name,
        fields_block,
        audio_fields_block,
        data.current_qfmt,
        data.current_afmt,
        data.current_css,
        data.audio_field_name,
    )


_SYSTEM_PROMPT = """You are an expert Anki card template author. Your job: given one existing \
Anki notetype (its fields, real sample content, and its current Front/Back/CSS templates), \
produce a NEW set of Front/Back/CSS templates that flips which language is being tested.

## The task: flipping direction

An Anki notetype has a Front template (the question) and a Back template (the answer). Right \
now, some fields are shown on the Front and some are shown on the Back -- that is the CURRENT \
direction. Your job is to produce the FLIPPED direction: whichever language is currently the \
ANSWER (shown only on the Back) becomes the new QUESTION (shown on the Front), and whichever \
language is currently the QUESTION (shown on the Front) becomes the new ANSWER (shown on the \
Back).

Call the language moving onto the front "the target language" (the language being studied -- \
the learner is prompted with it and must produce the answer in their own language). Call the \
language moving onto the back "the native language" (the language the learner already knows).

## Reading the deck

You are given:
- the deck's name and notetype's name (context only -- these can be genuinely uninformative, \
e.g. "Basic-25eeb"; never assume a name is accurate or meaningful)
- every field's name, and a handful of REAL sample values pulled from real notes
- the CURRENT Front template, Back template, and CSS

FIELD NAMES CAN BE MISLEADING. A field named "Notes" might hold an internal index number, not a \
note. Always look at the SAMPLE VALUES to understand what a field actually contains -- never \
trust a field's name alone.

Use the sample values to work out, for each field:
- what language its text is written in (if any) -- judge this from the actual characters and \
words in the samples, not from any field or deck name
- whether it holds a single word/term (short) or a full example sentence (longer, with natural \
sentence structure and punctuation)
- whether it is bookkeeping (an internal index, a numeric ID, a frequency rank) rather than \
content a learner should see
- whether it already contains an embedded [sound:...] audio reference, and if so whether that \
reference sits ALONE in the field or is mixed in with real text (e.g. "stellen [sound:xyz.mp3]" \
is text WITH audio mixed in; "[sound:xyz.mp3]" alone is pure audio)
- whether it holds an image (an <img src="..."> reference)

You may also use the deck name as a hint about what languages are involved, but never as the \
only signal -- always confirm against real sample content.

## Anki template syntax (this is the complete syntax you may use)

- `{{FieldName}}` -- inserts that field's content, HTML included, verbatim.
- `{{text:FieldName}}` -- inserts that field's content as PLAIN TEXT: HTML tags are stripped, \
and special references (audio [sound:...], images) are stripped too. Use this instead of a bare \
`{{FieldName}}` whenever a field's real value might contain an audio reference you do not want \
to play, or an image you do not want to show.
- `{{#FieldName}}...{{/FieldName}}` -- the content between the tags is only shown if FieldName \
is non-empty on a given note. Use this to avoid rendering an empty box when a field has no \
content on some notes.
- `{{^FieldName}}...{{/FieldName}}` -- the opposite: content inside is only shown if FieldName \
IS empty.
- `{{FrontSide}}` -- only usable inside the BACK template. Inserts everything that was shown on \
the Front. Every Back template you write must start with `{{FrontSide}}`.
- `<hr id=answer>` -- the conventional divider between the repeated front content and the \
answer content. Place it immediately after `{{FrontSide}}` in the Back template.
- You may only reference a field name that is EXACTLY one of the field names you were given. \
Inventing a field name, or misspelling one, produces a broken card.
- Plain HTML (<div>, <br>, <b>, class attributes, etc.) is allowed anywhere, exactly as in \
ordinary Anki card templates.

## Hard rules

1. The Front template must contain at least one field reference that is NOT wrapped in a \
{{#...}}...{{/...}} conditional. Anki refuses to save a card template whose front can ever \
render completely empty, so at least one thing must always show.

2. Copy the given CSS forward exactly, character for character, as the start of your new CSS, \
then append any new rules you want after it. Never remove, rewrite, or "clean up" the original \
CSS. Real decks often declare @font-face rules pointing at font files that live in the user's \
media folder -- rewriting the CSS from scratch breaks the deck's look.

3. Below, in the deck data, you are told exactly which fields already contain their own \
pre-existing audio -- this has already been determined for you; you do not need to detect it \
yourself from the samples. For any field on that list, if you want to show its text, reference \
it with {{text:FieldName}}, never a bare {{FieldName}}. This shows the text while dropping the \
embedded audio reference, so the deck's OLD audio does not play on the new, flipped card.

4. You are given the name of one field that is currently empty, reserved to hold NEW audio that \
will be generated later for whatever text you choose as speak_text_from. Reference this field \
with a BARE, unfiltered reference ({{FieldName}}, never {{text:FieldName}}) so that once audio \
is generated into it, it can actually play. You may wrap it in a \
{{#FieldName}}...{{/FieldName}} conditional (it starts empty, so this avoids showing anything \
until audio exists).

5. Never reference any OTHER field that already contains its own [sound:...] audio as a bare \
{{FieldName}} -- that would play the deck's old, original-direction audio on the new card, which \
is exactly wrong. If that field's text is otherwise useful, you may still show its text via \
{{text:FieldName}} (rule 3), which drops the audio but keeps the text.

6. Fields with real, human-readable content that you don't otherwise use should still appear \
somewhere on the Back, so nothing is silently thrown away -- unless a field is pure bookkeeping \
(an internal index/ID/frequency number) or is empty on essentially every note, in which case it \
is fine to leave it out entirely.

7. For speak_text_from, prefer a full example-sentence field on the target-language side if one \
exists -- hearing a word pronounced in a full sentence is more useful to a learner than hearing \
it in isolation. Only fall back to a single word/term field if there is no sentence field.

## Output format

Respond with EXACTLY these four sections, in this order, each starting with its own marker line \
and nothing else on that line. Do not write anything before "--- ANALYSIS ---" or after the CSS. \
Do not wrap anything in markdown code fences.

--- ANALYSIS ---
{
  "description": "<one or two plain-language sentences: what this deck is, and how you are converting it>",
  "target_language": "<name of the language moving onto the front, e.g. \\"English\\">",
  "native_language": "<name of the language moving onto the back, e.g. \\"Japanese\\">",
  "speak_text_from": "<exact name of the field whose text should be spoken aloud>",
  "write_audio_to": "<exact name of the empty audio field you were given>"
}
--- FRONT ---
<the new Front template HTML>
--- BACK ---
<the new Back template HTML>
--- CSS ---
<the new CSS, starting with the original CSS verbatim>

## Worked example

Given fields Word (samples: "casa", "perro", "libro"), Translation (samples: "house", "dog", \
"book"), ExampleSentence (samples: "La casa es grande.", "El perro corre.", "Leo un libro."), \
current Front `{{Translation}}`, current Back \
`{{FrontSide}}<hr id=answer>{{Word}}{{ExampleSentence}}`, current CSS \
`.card { font-size: 20px; }`, and audio field `ddc-audio`, a correct response looks like:

--- ANALYSIS ---
{
  "description": "A Spanish vocabulary deck, currently English-front/Spanish-back. Converting to Spanish-front/English-back so the learner practices producing Spanish.",
  "target_language": "Spanish",
  "native_language": "English",
  "speak_text_from": "ExampleSentence",
  "write_audio_to": "ddc-audio"
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

Now do the same for the real deck given below.
"""
