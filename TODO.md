# TODO

Known open items, not yet worked. See `docs/llm-notes.md` and the LLM overhaul commit history
for full background on the pipeline these refer to.

## Preview error right after Analyze, before Convert — fixed, confirmed in real Anki

Repro was: pick a deck, click Analyze, look at the preview pane (before ever clicking Convert).
Anki's own template error showed instead of the card:

```
Front template has a problem:
Found '{{#ddc-audio-<Field>}}', but there is no field called '{{ddc-audio-<Field>}}'
```

**Root cause confirmed against real source** (`pip download anki==26.8.1 --no-deps`, then read
`anki/notes.py`'s `ephemeral_card` and `anki/template.py`'s `TemplateRenderContext` directly —
the earlier "leading theory" below was right in spirit and is now verified, not guessed): the
first fix attempt (`_preview_notetype`, shaping a throwaway notetype copy with the missing
field appended, and handing that to `PreviewPanel` as `custom_note_type`) could never have
worked. `custom_note_type` is only used to pick which template dict renders and, afterward, to
patch the CSS — the actual backend call,
`self._col._backend.render_uncommitted_card_legacy(note=self._note._to_backend_note(), card_ord=..., template=..., fill_empty=..., partial_render=True)`,
takes **no notetype parameter at all**. Field-existence validation happens purely from the
note's own real, stored notetype (resolved via its `mid` inside `_to_backend_note()`), and
`ui/main_screen.py`'s preview always uses a real note (`mw.col.get_note(...)`) whose `mid`
points at the live, pre-conversion notetype — so the override notetype was never what the
error was checking against, regardless of its shape.

**Fix:** stop trying to make the field "exist" for preview purposes at all. The audio block
(`{{#ddc-audio-Word}}{{ddc-audio-Word}}{{/ddc-audio-Word}}`) is wrapped in a conditional and is
therefore guaranteed to render as nothing whether the field doesn't exist yet or exists but is
still empty pre-TTS — so it carries zero real preview information at this stage either way.
`llm/analyze.py`'s new `strip_pending_audio_html` (the inverse of `_append_audio_html`) removes
each not-yet-existing audio field's block from the Front HTML handed to `PreviewPanel`, and
`ui/main_screen.py`'s `_refresh_preview` now previews against the real, unmodified live
notetype directly — no throwaway notetype shaping, no collection interaction at all, matching
the project's existing "preview writes nothing" invariant. Covered by
`tests/test_llm_analyze.py::TestStripPendingAudioHtml`; the full 293-test pure suite passes.
**Confirmed fixed in real Anki testing.**

## Original deck's own audio playing on the converted card, and a hidden audio field's play button not actually silencing it — fixed at the root, pending real-Anki confirmation

Two bugs found in real Anki testing (2026-09-17), traced to one shared root cause.

**Root cause, confirmed against real Anki source (`ankitects/anki` on GitHub, not guessed):**
`{{text:Field}}` compiles to `strip_html(text)`, whose regex only matches HTML tags (`<...>`) --
it has never touched `[sound:...]`, which is Anki's own bracket notation, not HTML. Separately,
`extract_av_tags` (which decides what autoplays) scans the *fully rendered* card text for
`[sound:...]`/`[anki:tts...]` patterns unconditionally, with no awareness of what filter
referenced the field or what HTML/CSS wraps it. Checked the complete Anki filter list --
`text`, `furigana`/`kanji`/`kana`, `cloze`/`cloze-only`, `type*`, `hint`, `tts` -- none of them
strip `[sound:...]`. This meant `llm/audio_safety.py`'s `{{text:Field}}`-forcing mechanism, and
`llm/field_visibility.py`'s hide-via-CSS-wrap (which also forced `{{text:Field}}`, on the same
mistaken assumption), never actually prevented playback -- both bugs were the same gap surfacing
two different ways.

**Fix:** since no template filter can suppress `[sound:...]`, the only thing that reliably works
is removing it from the *data* before a template can ever reference it. Every field's value now
has `[sound:...]` stripped out while it's copied onto the clone, unconditionally, for every
field -- `ops/notetype_manager.py`'s `_strip_pre_existing_audio` (called from
`_copy_fields_by_name`). This doesn't depend on detecting which fields carry audio (removing that
whole class of "sampling missed it" risk), and it preserves real text in a field that mixes
content with its own audio (the German-deck `"Hund [sound:hund.mp3]"` case) -- only the marker
goes. `llm/field_visibility.py`'s hide mechanism is unchanged in code but is now correct as a
side effect: by the time a converted card exists to hide fields on, none of its fields have
`[sound:...]` left in their values regardless of hide state.

**This required removing "Flip in place" as a mode.** The stripping fix needs a fresh copy to
write the stripped value into; Flip in place never created one (it repoints the user's *existing*
notes onto the clone without touching field values at all). Giving it the same guarantee would
have meant rewriting the literal content of the user's real notes in place -- a bigger, more
sensitive kind of change than anything else this addon does. Rather than ship an asymmetric
guarantee (one mode safe, one not), Flip in place was cut entirely; there is now only one mode
(duplicate onto a new deck). See `core/conversion.py`'s and `CLAUDE.md`'s "NOTETYPE CLONING /
SAFE APPLY" section for the full writeup.

Covered by `tests/test_notetype_manager.py::TestPreExistingAudioIsStrippedFromCopies` (pure
audio field → empty; mixed field → text survives, audio gone; original notes' own audio
untouched; no media file deleted). Full suite: 308 tests, all green.
**Not yet re-tested in real Anki** — do that before considering this fully closed.

## Extend `validate.py` to check placement compliance

The German test deck (fields literally named "Front"/"Back", colliding with Anki's own
template-side vocabulary) is still flaky on the 7B model — direction sometimes comes out
inverted, run to run, even at temp 0 (confirmed as real llama.cpp non-determinism, not a fixed
prompt defect). `validate.py` currently checks field existence, conditional balance, and
misplaced-field leaks, but not full placement compliance against `new_front_fields`/
`new_back_fields`. Extending it to catch and retry on a placement violation is the intended fix
-- the same pattern that already worked for audio safety.

## Documentation pass — done

`CLAUDE.md`, `README.md`, `docs/api-notes.md`, and `docs/llm-notes.md` have been rewritten/
updated to describe the current LLM-first pipeline instead of the deleted role-mapping system.
`docs/llm-notes.md`'s model facts were also corrected (it still described the original 1.5B
model; the addon has shipped the 7B model since early in the LLM overhaul). If the pipeline
changes again in a way that makes any of these stale, update them again rather than letting
drift accumulate a second time.
