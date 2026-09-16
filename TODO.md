# TODO

Known open items, not yet worked. See `docs/llm-notes.md` and the LLM overhaul commit history
for full background on the pipeline these refer to.

## Preview error right after Analyze, before Convert — fixed, pending real-Anki confirmation

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
