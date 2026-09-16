# TODO

Known open items, not yet worked. See `docs/llm-notes.md` and the LLM overhaul commit history
for full background on the pipeline these refer to.

## Preview still errors right after Analyze, before Convert

Repro: pick a deck, click Analyze, look at the preview pane (before ever clicking Convert).
Anki's own template error shows instead of the card:

```
Front template has a problem:
Found '{{#ddc-audio-<Field>}}', but there is no field called '{{ddc-audio-<Field>}}'
```

The conversion itself is unaffected — Convert and Generate TTS audio both work correctly. Only
the live preview widget is wrong, and only before Convert has run (the field genuinely doesn't
exist on the live notetype yet at that point).

`ui/main_screen.py`'s `_preview_notetype` already tries to work around this: it builds a
throwaway copy of the live notetype via `ops.notetype_manager.shape_notetype` with the
not-yet-created audio field(s) appended, then hands that to `PreviewPanel` instead of the real
notetype. That fix did not resolve it in real Anki, despite looking correct on paper and matching
the pattern `ops/notetype_manager.py`'s own `shape_notetype` is already tested against.

Leading theory, not yet confirmed: `PreviewPanel.render()` calls
`note.ephemeral_card(ord, custom_note_type=model, custom_template=template, fill_empty=False)`
(`ui/preview_panel.py`). The assumption was that `custom_note_type` fully substitutes for the
note's real notetype, including which field names are considered to exist for template
validation. Given the bug still reproduces, that assumption may be wrong — Anki's renderer might
validate template field references against the note's *actual* stored notetype (looked up by id)
even when a `custom_note_type` override is supplied, rather than against the override itself.
Needs verifying against real `aqt`/`anki` source (this dev shell has no `aqt` installed) before
trying another fix — per claude.md's own rule, don't guess at Anki API behavior a second time
without checking.

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
