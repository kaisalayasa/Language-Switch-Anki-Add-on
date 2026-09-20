# Anki API notes

`claude.md` forbids guessing Anki API signatures. This file records what was **verified**,
how, and what still needs confirming.

Companion file for the local-LLM side of this project (llama.cpp invocation contract, GGUF
model facts, HuggingFace/GitHub asset names): `docs/llm-notes.md`. Same discipline, different
API surface.

Target build: **Anki 26.08.1** (build `39e4b0b4`), bundled Python 3.13, collection schema 18.
Verified by string-inspecting the compiled modules under
`%LOCALAPPDATA%\Programs\Anki\app_packages\`.

## Confirmed present

| Area | Symbol |
|---|---|
| Notetypes | `col.models.add_dict` · `update_dict` · `by_name` · `copy` · `change` (legacy) · `ensure_name_unique` · `field_names` · `all_names_and_ids` · `new_field` · `add_field` · `new_template` · `add_template` · `set_sort_index` |
| Change notetype | `col.models.change_notetype_info` · `change_notetype_of_notes`; proto `ChangeNotetypeRequest` / `ChangeNotetypeInfo` with `old_notetype_id`, `new_notetype_id`, `note_ids`, `new_fields`, `new_templates` |
| **Scheduling reset** | **`col.sched.schedule_cards_as_new`** with `card_ids`, `restore_position`, `reset_counts`, `context`; plus `ScheduleCardsAsNewDefaults` |
| Decks | `col.decks.add_normal_deck_with_name` · `by_name` · `id_for_name` · `all_names_and_ids` · `new_deck` · `add_deck` |
| Notes | `col.new_note` · `add_note` · `get_note` · `update_note` · `update_notes` · `find_notes` · `remove_notes` |
| Media | `col.media.add_file` · `check` (→ `CheckMediaResponse`) · `trash_files` · `empty_trash` · `restore_trash` · `have` |
| Undo | `col.add_custom_undo_entry` · `merge_undo_entries` · `col.create_backup` |
| Ops | `aqt.operations.CollectionOp` · `QueryOp` · `OpChanges`; `aqt.operations.notetype.{add_notetype_legacy, update_notetype_legacy, change_notetype_of_notes, remove_notetype}`; `aqt.operations.scheduling.{forget_cards, reposition_new_cards, set_due_date}` |
| GUI | `gui_hooks.main_window_did_init` · `profile_did_open` · `browser_menus_did_init`; `mw.form.menuTools`; `mw.progress.{start, update, finish, want_cancel, set_title}` |
| Later milestones | `aqt.clayout.CardLayout` (M3 preview) · `aqt/import_export/import_dialog` (M3 prior art) · `aqt.import_export.exporting.ApkgExporter` with `with_media` / `with_scheduling` |

## Background progress/threading (M5) — confirmed against real `aqt` source, 2026-09-13

Batch TTS generation needed incremental per-note progress and cancellation, which
`CollectionOp`/`QueryOp` don't expose (they bracket one atomic `op(col) -> result`, not a
running counter). Pulled the same `aqt==26.8.1` wheel used for the `CardLayout` work and read
`aqt/taskman.py` + `aqt/progress.py` directly rather than guess at the lower-level API:

- `mw.taskman.run_in_background(task, on_done=None, args=None, uses_collection=True) -> Future`
  — `task` runs on a single dedicated collection-executor worker thread (all
  collection-touching background work in Anki is serialized through that one thread, so a
  long batch blocking it for its whole duration is expected, not a problem). `on_done` is
  called back on the **main thread** (`taskman.py`'s `run_on_main` marshals it via a queued
  Qt signal — confirmed the same way for `CollectionOp`'s own `success()` during the
  `CardLayout` investigation).
- `mw.progress.start(max=0, min=0, label=None, parent=None, immediate=False, title="Anki")`
  / `.finish()` — call from the **main thread**, bracketing the whole background task, the
  same way `CollectionOp` does internally via `taskman.with_progress`.
- `mw.progress.update(label=None, value=None, max=None, ...)` **must** run on the main
  thread — it has an explicit `if not self.mw.inMainThread(): print(...); return` guard.
  From the worker thread, call it via `mw.taskman.run_on_main(lambda: mw.progress.update(...))`.
- `mw.progress.want_cancel() -> bool` has **no** such guard — it's a plain read of
  `ProgressDialog.wantCancel` (set when the user presses Escape or tries to close the
  progress window), safe to poll directly from the background loop with no marshaling.
- `col.create_backup(*, backup_folder: str, force: bool, wait_for_completion: bool) -> bool`
  (confirmed from `anki/collection.py`) plus `mw.pm.backupFolder() -> str` (confirmed from
  `aqt/profiles.py`) together back the batch dialog's "Create backup now" button.

## Piper facts (M2) — verified against the real GitHub/HuggingFace APIs, 2026-09-12

Not Anki APIs, but the same "never guess" discipline applies: getting a download URL or
archive layout wrong wastes a build cycle the same way a wrong Anki signature does.

**GitHub releases** (`GET api.github.com/repos/rhasspy/piper/releases/latest`):

- Tag **`2023.11.14-2`** is genuinely the newest release — Piper is lightly maintained, this
  is not a fetch failure.
- Complete asset list (confirmed, not partial):
  `piper_windows_amd64.zip` · `piper_linux_x86_64.tar.gz` · `piper_linux_aarch64.tar.gz` ·
  `piper_linux_armv7l.tar.gz` · `piper_macos_x64.tar.gz` · `piper_macos_aarch64.tar.gz`.
- **No native Windows ARM64 asset exists.** `piper_binary_manager.resolve_asset_name` raises
  `UnsupportedPlatform` on that combination rather than falling back to the amd64 build.

**HuggingFace voice repo** (`rhasspy/piper-voices`):

- Path/filename convention confirmed by listing a real folder and file:
  `en/en_US/ljspeech/high/en_US-ljspeech-high.onnx` genuinely exists (114,199,011 bytes),
  alongside `en_US-ljspeech-high.onnx.json` (4,970 bytes). General pattern:
  `en/{locale}/{voice}/{quality}/{locale}-{voice}-{quality}.onnx{,.json}`.
- Download via `https://huggingface.co/rhasspy/piper-voices/resolve/main/<path>`.
- `en_GB/alba` folder confirmed to exist as a locale/voice pairing; its `medium` quality
  subfolder was not independently re-checked byte-for-byte the way `ljspeech/high` was — if
  `piper_test_dialog.py`'s "Speak" button fails to download it, that's the first thing to
  check, and this note should be updated with the real result either way.
- **Per-voice license check (2026-09-20), don't skip this when adding a voice:** each voice's
  `MODEL_CARD` on this repo states its own license, and they are not uniform.
  `en_US-lessac-medium` (the addon's original default) was checked and found to be trained on
  the Blizzard 2013/Lessac corpus, licensed "Research Purposes only" and explicitly excluding
  "the development, marketing, commercialisation, sale or licencing of voice synthesis ...
  products" — see https://www.cstr.ed.ac.uk/projects/blizzard/2013/lessac_blizzard2013/license.html.
  Removed from `CURATED_VOICES` for that reason. `en_US-ljspeech-high`'s model card states
  "License: public domain" — confirmed clean, now the default. `en_GB-alba-medium`'s model card
  states "License: https://creativecommons.org/licenses/by/4.0/" (CC BY 4.0) — usable, requires
  attribution (given in `README.md`'s Licensing section), not yet independently re-verified
  beyond the model-card text itself.

**Still unverified (signature/behavioural level, not presence)**:

- `aqt.sound.av_player.play_file(path)` — long-standing, widely-used Anki addon sound API,
  but not independently confirmed against this 26.08.1 build. `addon/ui/piper_test_dialog.py`
  degrades to an actionable `showWarning` (not a crash) if the call shape has changed —
  mirrors `preview.py`'s handling of the unverified `CardLayout` constructor.
- The exact Piper CLI invocation shape. `piper_provider.py` currently assumes: text on
  **stdin**, `--model <path to .onnx>`, `--output_file <path>` (Piper's documented usage as of
  the `2023.11.14-2` release notes). Confirm for real once the binary is actually downloaded,
  by running `piper --help` in a terminal, and record the real output here.

### Piper CLI `--help` output

_(not yet run — see `docs/api-notes.md`'s probe instructions above for the Anki-side
equivalent; this one just needs the extracted `piper`/`piper.exe` run directly, no Anki
required)_

## ffmpeg facts (compression feature, added 2026-09-20) — verified against real APIs and a real download this session

Same "never guess" discipline as the Piper facts above. `addon/tts/ffmpeg_binary_manager.py`
downloads ffmpeg only to shrink Piper's WAV output to MP3 (see `piper_provider.py`'s
`_compress_to_mp3`) — never for anything else.

**Windows/Linux — BtbN/FFmpeg-Builds** (`GET api.github.com/repos/BtbN/FFmpeg-Builds/releases/latest`):

- This repo publishes to a single continuously-updated release literally tagged `latest`, not
  discrete per-version tags — there is no immutable release to pin the way Piper's
  `2023.11.14-2` tag is. Same situation already accepted for llama.cpp's nightly builds: the
  asset filenames below need periodic re-verification, not a one-time check.
- Confirmed real asset filenames (`n9.0` family, i.e. tracking ffmpeg's `9.0` stable branch):
  `ffmpeg-n9.0-latest-win64-lgpl-9.0.zip` and `ffmpeg-n9.0-latest-linux64-lgpl-9.0.tar.xz`
  (also `-linuxarm64-` for `aarch64`). No 32-bit ARM Linux build exists — `resolve_asset`
  raises `UnsupportedPlatform` for `armv7l`, unlike Piper which does support that platform;
  compression is just unavailable there.
- **LGPL vs GPL confirmed by reading the actual build scripts**, not the repo's marketing
  description: `scripts.d/50-libmp3lame.sh` and `scripts.d/45-libvorbis.sh` in
  `BtbN/FFmpeg-Builds` have no `$LICENSE`/`$GPL` gate at all — both encoders build
  unconditionally in every flavor. Only `libx264`/`libx265` (GPL-only) are excluded from the
  `lgpl` flavor. This addon only ever downloads the `lgpl` asset, so the binary it runs is
  genuinely LGPL-2.1+, not GPL.
- **Real download confirmed this session** (Windows amd64, `2026-09-20`): downloaded, extracted,
  and ran successfully. Real executable path inside the archive:
  `ffmpeg-n9.0-latest-win64-lgpl-9.0/bin/ffmpeg.exe` (nested — `_find_executable`'s recursive
  search handles this regardless of the archive's internal layout, so this path isn't
  hardcoded anywhere). Real `-version` output:
  `ffmpeg version n9.0.2-3-ga5923073bf-20260920 Copyright (c) 2000-2026 the FFmpeg developers`.

**macOS — evermeet.cx** (`GET https://evermeet.cx/ffmpeg/info/ffmpeg/release`):

- No Apple Silicon native build exists from this source ("I do not plan to provide native
  ffmpeg binaries for Apple Silicon ARM" — the site's own words). Apple Silicon Macs run the
  x86_64 build under Rosetta 2, which macOS installs automatically on first launch of an Intel
  binary.
- Confirmed via the JSON info API this session: current version `9.0.2`, built with
  `--enable-gpl --enable-version3` (GPL, not LGPL) and both `--enable-libmp3lame` and
  `--enable-libvorbis` present. Pinned URL: `https://evermeet.cx/ffmpeg/ffmpeg-9.0.2.zip`.
  This is a versioned filename, not a "latest" alias — it will need updating once 9.0.2 is
  superseded, same caveat as the BtbN builds above.
- Used despite being GPL because it's invoked as a subprocess only, never linked — see the
  module docstring in `ffmpeg_binary_manager.py` and `README.md`'s Licensing section for the
  full reasoning.

**Real end-to-end synthesis + compression confirmed this session** (Windows amd64): a real
Piper synthesis followed by real ffmpeg compression produced a working `.mp3` file; the
original `.wav` was removed as designed. Exact before/after byte sizes not yet recorded here —
see the commit that introduced this feature for the numbers observed at the time.

## ⚠️ The traps (found by actually running M1)

**`col.sched.forget_cards` does not exist.** The obvious guess fails.

- Backend method: `col.sched.schedule_cards_as_new(...)`
- `forget_cards` exists **only** as the GUI wrapper `aqt.operations.scheduling.forget_cards`

**`col.models.ensure_name_unique` does not take a name and return a new one.** It takes a
**notetype dict** and mutates `notetype["name"]` **in place**, returning `None`:

```python
def ensure_name_unique(self, notetype: NotetypeDict) -> None: ...
```

Calling it with a bare string (`col.models.ensure_name_unique(plan.clone_notetype)`) fails
with `TypeError: string indices must be integers, not 'str'` — internally it tries
`that_string["name"]`. This crashed on the first real run against `test profile`.

Presence-only verification (byte-scanning the compiled module) confirmed the symbol
*exists* but said nothing about its calling convention — that gap is exactly how this got
guessed wrong. Fixed in `notetype_manager.py` by not depending on the exact signature at
all: `_unique_notetype_name()` builds uniqueness from `col.models.by_name`, which *is*
behaviourally verified (used successfully elsewhere first). The corresponding test double
in `tests/fake_collection.py` was also wrong in the same way and has been corrected to
match the real (dict-in, mutate-in-place) contract, with a regression test
(`TestNotetypeNaming` in `tests/test_notetype_manager.py`) asserting production code never
calls it with anything but a dict.

**`col.merge_undo_entries(target)` raises `"target undo op not found"` if a notetype
schema change happens between `add_custom_undo_entry(...)` and the merge.** Confirmed
against a real collection, exact wording captured. Root cause: creating or modifying a
notetype (`col.models.add_dict`/`update_dict`) — and, it turns out,
`change_notetype_of_notes` too — bumps Anki's schema modification time, which invalidates
*any* outstanding custom undo marker, not just the one that triggered the change. A marker
set before such a call can never be merged afterward, no matter what runs in between.

This was first noticed via the "Preview card…" flow (an earlier fix moved that call into a
`CollectionOp`, which was a real improvement on its own merits but **did not address this**
— the marker was still being set before the notetype write). Investigating properly turned
up that the **exact same bug was latent in the real Apply flow** (`apply_plan` in
`notetype_manager.py`), in both conversion modes that existed at the time (Flip-in-place
was later removed entirely — see below), via `run_conversion` in `convert_op.py` — it had
simply not been exercised by a real (non-dry) run yet. `tests/test_convert_op.py`
(previously nonexistent — only the inner `apply_plan` was tested, which bypasses the undo
wrapping entirely) now covers `run_conversion` directly.

**The fix, applied everywhere `add_custom_undo_entry`/`merge_undo_entries` is used in this
addon:** the marker may only span calls that are *not* notetype-schema-level.
- `col.models.add_dict` / `update_dict` / `change_notetype_of_notes` — schema-level, never
  wrap.
- `col.decks.add_normal_deck_with_name`, `col.new_note`/`add_note`/`update_note`,
  `col.sched.schedule_cards_as_new` — not schema-level (deck creation has never required a
  full resync, unlike notetype changes), safe to wrap.

Concretely: `apply_plan` now creates the clone (schema change) *before* setting the marker;
only the scheduling reset and the note duplication sit inside the wrapped span.
`ConvertDialog._preview` was restructured the same way: `ensure_preview_scaffold` (schema
change) runs unwrapped, then the marker is set, then `write_preview_note` (pure data) is
wrapped. (At the time this was written there was also a Flip-in-place mode, which ran
`change_notetype_of_notes` -- also a schema change -- before setting its own marker the
same way. That mode has since been removed entirely; see `core/conversion.py`'s module
docstring.)

**Consequence for the UI:** a conversion is no longer a single undo step. It's two separate
ones — see `apply_plan`'s comments for exactly where the boundary falls. `claude.md` §8 has
been corrected to state this rather than promise single-keystroke undo.

`tests/fake_collection.py` now simulates this: `add_dict`/`update_dict`/
`change_notetype_of_notes` bump a `_schema_generation` counter, and
`merge_undo_entries` raises `RuntimeError("target undo op not found")` if the generation
has moved since the matching `add_custom_undo_entry` call — reproducing the real bug
faithfully enough that both the original failure and the fix are now provable in the pure
test suite, without needing a real Anki collection to notice a regression.

**Update: the user still hit this error in real use, in `main_screen.py`'s single-screen
flow, on a conversion that had visibly succeeded** — despite `apply_plan`'s marker already
sitting after every schema-level call this module makes, per the fix above. The exact
second cause could not be pinned down without a live repro (no schema-level call this
module doesn't already know about was found on inspection), but the consequence turned out
to be worse than a stray error dialog: because the marker/merge lives inside `apply_plan`,
an uncaught failure there propagates all the way out through `run_conversion` and
`convert_op._op`, so `CollectionOp` sees the *whole operation* as having thrown — it never
calls `on_success`, even though every real write had already committed. That's why the
error dialog was the *only* thing visible: `main_screen.py`'s success handler (which shows
the "done" message and re-points the screen at the new deck/notetype) never ran, and
neither did `CollectionOp`'s own `mw.col.op_made_changes(changes)` UI refresh, which
explains the deck browser also not showing the new deck.

**Fix:** `apply_plan` now wraps the merge in try/except and retries with a *fresh* marker
placed at that exact point on failure. This needs no further guessing about the root cause
-- nothing schema-changing can execute between the retry marker and merging it two lines
later (single-threaded, no calls in between), so the retry is structurally guaranteed to
succeed. Confirmed working in real use (2026-09-13): the error dialog is gone, and the
conversion completes normally. The retry is *not* surfaced in `result.messages` -- to the
user it's invisible and irrelevant, since the conversion already fully succeeded either
way -- it's only noted here in case the underlying (still-unidentified) second cause of the
original merge failure ever needs a real fix rather than a retry working around it.
Covered by `tests/test_notetype_manager.py::TestUndoMergeRecovery`, using a
`FakeCollection` subclass whose first `merge_undo_entries` call always fails regardless of
generation, to exercise the retry path independent of whatever the real second cause is.

**Related, separately confirmed in the same real-use report:** even with the retry above,
`CollectionOp`'s own `mw.col.op_made_changes(changes)`-driven UI refresh did not reliably
make Anki's deck browser show the newly-created/changed deck (a manual refresh in Anki was
needed) -- even though the addon's own screen *did* correctly refresh and re-select the new
pair. Likely because the deck itself is created via `col.decks.add_normal_deck_with_name`
before the undo marker that `op_changes` actually describes, so the `OpChanges` returned
from `merge_undo_entries` may not carry a "decks changed" flag Anki's hook-based refresh
looks for. Rather than chase the exact flag, `main_screen.py` now calls
`mw.deckBrowser.refresh()` directly after a successful conversion, and again from
`closeEvent` as a safety net -- both defensively wrapped (see
`MainScreen._refresh_anki_main_window`), since `deckBrowser.refresh` is stable/long-standing
but not worth a hard crash if a future build ever renames it.

## The `text:` field modifier — CORRECTED: does not strip `[sound:...]`, confirmed against real source

**This section originally claimed `{{text:Field}}` strips special references including sound —
that claim was never independently verified (see the original text preserved below) and turned
out to be wrong.** Confirmed by reading the actual `ankitects/anki` Rust source (`rslib/src/
template_filters.rs`, `rslib/src/text.rs`, `rslib/src/card_rendering/mod.rs`) during a real
Anki bug investigation (2026-09-17), not guessed:

- `{{text:Field}}` compiles to exactly `"text" => strip_html(text)`.
- `strip_html`'s regex is `(<!--.*?-->)|(<style.*?>.*?</style>)|(<script.*?>.*?</script>)|(<.*?>)`
  — HTML tags/comments/style/script blocks only. It never matches `[sound:...]`, which is
  Anki's own bracket notation, not HTML syntax.
- A *different* function, `html_to_text_line` (used for things like the browser's preview
  column, not template rendering), does strip `[sound:...]` first via `SOUND_TAG.replace_all()`
  — but `{{text:Field}}` does not call it. The two are easy to conflate; only one is what the
  `text:` template filter actually runs.
- Separately, `extract_av_tags` (`rslib/src/card_rendering/mod.rs`) is what decides what
  autoplays. It runs on the *fully rendered* card text, after all field filters have already
  applied, and finds `[sound:...]`/`[anki:tts...]` patterns by plain regex match — completely
  unconditional, with no awareness of what filter referenced the field or what HTML/CSS
  surrounds it.
- The complete Anki template filter list was checked for anything that does strip
  `[sound:...]`: `text`, `furigana`/`kanji`/`kana`, `cloze`/`cloze-only`, `type`/`type-cloze`/
  `type-nc`, `hint`, `tts`. None of them do.

**Consequence:** `llm/audio_safety.py`'s `enforce_audio_safety` (force a sound-bearing field's
reference into `{{text:Field}}`) never actually prevented that field's audio from autoplaying —
it only stripped incidental HTML formatting, which was never the real risk. This was the root
cause of two real bugs (the original deck's audio still playing on a converted card, and hiding
an audio field via the "Hide fields" panel silencing its play button but not the sound). The
real fix moved to the data layer instead: `ops/notetype_manager.py`'s
`_strip_pre_existing_audio` removes `[sound:...]` from every field's value while it's copied
onto the clone, unconditionally, so there's nothing left for any template filter to fail to
suppress. See `CLAUDE.md`'s "NOTETYPE CLONING / SAFE APPLY" → "Audio" section and `TODO.md` for
the full writeup, including why this also required removing the "Flip in place" mode (it never
duplicated notes, so it had no copy step to strip audio during).

`enforce_audio_safety` itself is still in the code and still harmless (rewriting a reference to
`{{text:Field}}` doesn't hurt anything), but it is no longer load-bearing for audio safety.

<details>
<summary>Original text (2026-09, before the correction above) — kept for the record</summary>

> `{{text:Field}}` strips special references (sound, images) and HTML from a field's rendered
> value. This addon force-rewrites any bare or differently-filtered reference to a field with
> known pre-existing audio into this form — see `llm/audio_safety.py`'s `enforce_audio_safety`.
>
> **Superseded home, same underlying fact:** this used to be owned by the deleted role-mapping
> system's `core/role_detect.py` (`_filter_for`, applied selectively per field during role
> detection). The LLM overhaul moved the same mechanism to `llm/audio_safety.py`, where it's
> applied uniformly and deterministically to whatever the model wrote, regardless of what the
> model itself referenced or how — see `CLAUDE.md`'s "LOCAL LLM DECK ANALYSIS" section.
>
> **This is core Anki template syntax, not a Python/aqt API** — documented, long-standing,
> part of the template-rendering language itself rather than something that has shifted across
> `aqt`/`anki` releases the way Python method signatures have (see the traps below). It was not
> independently re-verified against a real running Anki 26.08.1 instance; confirmed only by
> documentation and by the reproduction in `tests/test_llm_audio_safety.py`. **Verify it
> visually** the first time a mixed-field deck (the German test deck, `"Hund [sound:hund.mp3]"`
> in one field) goes through the live preview: the term should render as plain text with no
> leftover play icon for its own original-language audio. If `{{text:Field}}` behaves
> differently than documented on this build, that would show up immediately there, before any
> real note is touched.

That last line turned out to be exactly right, in the end — it just took a real bug report to
actually check it.

</details>

## Adding fields to the clone (generated audio fields)

A conversion creates the field(s) its generated audio will live in, rather than reusing
one the deck already had (see `addon/core/audio_fields.py` for why). That makes this the
first place the addon changes a notetype's **field list**, not just its templates.

**Naming, current scheme:** one field per real front-content field, named
`core.audio_fields.generated_audio_field_name(source_field_name)` — e.g.
`generated_audio_field_name("Example Sentence")` → `"ddc-audio-Example Sentence"`. Decided by
`llm.direction.resolve_direction` (one `AudioTarget` per new-front field), not a single
shared `ddc-audio` field per notetype the way an earlier design had it — see `CLAUDE.md`.
`ConversionPlan.new_fields` (built by `ui/main_screen.py` from `DeckAnalysis.audio_targets`)
is what actually reaches `build_clone` below; everything in this section about *how* those
names get onto the saved notetype is unchanged by that history.

**What it does, and why this shape:**

- The field is added to the clone dict **before** `col.models.add_dict(...)` — i.e. the
  notetype is *created* with the extra field, rather than the field being added to an
  already-saved notetype. That avoids a second schema-level write entirely, and avoids
  Anki's "add field to existing notetype" path (which rewrites every note of that type).
- `notetype_manager.make_field_factory` prefers **`col.models.new_field(name)`** (confirmed
  present above), so the dict carries whatever keys this Anki build expects. If that call
  doesn't accept a bare name on some build, it falls back to deep-copying an existing field
  dict and clearing the keys that identify it (`id`, `ord`, `description`, `tag`) — copying
  the `id` would make the new field indistinguishable from the one it was copied from.
  `col.models.add_field` is deliberately **not** used: it's documented as operating on a
  notetype, and appending to `flds` ourselves needs no assumption about its signature.
- Fields are **appended after** the source's own fields, never inserted among them, so every
  original field keeps its own ord and the new one(s) are easy to spot at a glance in Anki's
  field-list editor. (This ordering used to also matter for a since-removed "Flip in place"
  mode's positional field map -- that mode is gone, so this is now purely a legibility choice,
  not a correctness requirement, but there's no reason to insert among the source fields
  either.)
- `build_clone` verifies the fields are actually present on the saved notetype and raises
  `ApiMismatch` naming them if not — the generated templates reference them, so a silent
  drop would leave every card rendering a dangling `{{Field}}`.

**To confirm on the first real run** (fails loudly rather than corrupts anything):

1. **A generated field name can contain a space** whenever the source field's own name does
   (e.g. `ddc-audio-Example Sentence`), which is ordinary for Anki field names and renders as
   `{{ddc-audio-Example Sentence}}` / `{{#ddc-audio-Example Sentence}}` — no character
   `generated_audio_field_name` produces is special to the template parser, and no forbidden
   character (`:`, `"`, `{`, `}`) is used. If Anki nonetheless rejects or renames it,
   `build_clone`'s verification raises `ApiMismatch` immediately, and the fix is in
   `addon/core/audio_fields.py`'s `generated_audio_field_name`.

## Still to confirm (exact signatures, not just presence)

Presence is verified; the precise parameter lists are not, because the shipped modules are
`.pyc` compiled for Python 3.13 and cannot be unmarshalled by the 3.11 available here.

`addon/ops/notetype_manager.py` handles this defensively: `_call_checked()` introspects the
real signature and passes only accepted keywords, and `_extract_id()` copes with either a
bare `int` or an `OpChangesWithId`. A genuine mismatch raises `ApiMismatch` naming what to
re-check, rather than writing something wrong.

To fill in the exact signatures, open the **`test profile`** in Anki, press
`Ctrl+Shift+;` for the debug console, and run:

```python
from addon.ops.notetype_manager import probe_api
from aqt import mw
print("\n".join(probe_api(mw.col)))
```

Or, without the addon installed:

```python
import inspect
from aqt import mw
for obj, names in [
    (mw.col.models, ["copy", "add_dict", "update_dict", "change_notetype_info",
                     "change_notetype_of_notes", "ensure_name_unique", "field_names"]),
    (mw.col.decks,  ["add_normal_deck_with_name", "by_name", "id_for_name"]),
    (mw.col.sched,  ["schedule_cards_as_new"]),
    (mw.col.media,  ["add_file", "check", "trash_files"]),
    (mw.col,        ["new_note", "add_note", "find_notes", "find_cards",
                     "add_custom_undo_entry", "merge_undo_entries"]),
]:
    for n in names:
        f = getattr(obj, n, None)
        print(n, inspect.signature(f) if f else "MISSING")
```

Paste the output below this line.

### Probe output

_(not yet run)_

## `aqt.clayout.CardLayout` — pulled forward from M3, now verified against real source

**SUPERSEDED AGAIN, by the single-screen redesign (current architecture, read this first):**
`addon/ui/preview_panel.py`'s `PreviewPanel` doesn't launch `CardLayout` at all any more --
`addon/ui/preview.py` and `addon/ui/card_preview.py`, described in the "RESOLVED" paragraph
below, were both deleted along with the rest of the role-mapping-era UI. `PreviewPanel` is an
*embedded* widget (the single screen's own right-hand pane) built by reading `CardLayout`'s own
internal preview implementation (`setup_preview`/`_renderPreview` in the real `aqt` 26.8.1
source) and reproducing just the part it needed: a plain `AnkiWebView` fed via
`note.ephemeral_card(ord, custom_note_type=model, custom_template=template, fill_empty=False)`
-- nothing about that rendering path requires `CardLayout`'s dialog chrome, so there was no
need to open `CardLayout` as a separate window at all. Same safety property either way:
`ephemeral_card()` never writes to the collection, so the hang bug documented below (a
collection *write* racing a newly-opened *modal dialog*) structurally cannot recur -- this
widget is not a dialog, and it never writes anything. See `CLAUDE.md`'s "ANKI DATA MODEL"
section for the one *new* preview bug this current architecture has (a template error shown
right after Analyze, before Convert, tracked in `TODO.md` -- unrelated to anything below).

The rest of this section, including the now-superseded "RESOLVED" paragraph directly below,
is kept as the investigation trail that got here, because it explains *why*
this design was chosen over the much more elaborate one that preceded it (a scratch
notetype/deck/note built via `CollectionOp`, in `notetype_manager.py`'s now-deleted
`ensure_preview_scaffold`/`write_preview_note`/`build_preview_note`). That approach caused a
real, repeatedly-reproduced Anki hang -- bad enough that it once left even Anki's own native
`Ctrl+L` shortcut unresponsive until restart -- across five separate attempts to fix it in
place. It was never a timing bug to patch; the fix was removing the collection write
entirely, which the paragraph above describes.

`claude.md` schedules the live-preview pane for M3 ("investigate reusing/subclassing
`aqt.clayout.CardLayout`"). At the user's explicit request, M1 pulled it forward early.

The installed build's compiled module targets Python 3.13 and can't be introspected from
this repo's Python 3.11 tooling, so byte-scanning could only confirm the class *exists*, not
its calling convention — the same gap that caused the `ensure_name_unique` trap earlier.
**Fixed properly this time**: rather than guess, the actual `aqt` package for this exact
Anki version was pulled from PyPI (`pip download aqt==26.8.1 --no-deps`, a real published
Anki wheel — matches the installed `26.08.1`) and its source read directly.

### Confirmed real signature (`aqt/clayout.py`, `aqt==26.8.1`)

```python
class CardLayout(QDialog):
    def __init__(
        self,
        mw: AnkiQt,
        note: Note,
        ord: int = 0,
        parent: QWidget | None = None,
        fill_empty: bool = False,
    ) -> None:
        QDialog.__init__(self, parent or mw, Qt.WindowType.Window)
        mw.garbage_collect_on_dialog_finish(self)
        ...
        self.setWindowModality(Qt.WindowModality.ApplicationModal)
        self.show()
        self.setFocus()
```

`open_card_layout()`'s first (now-confirmed, not just first-guessed) attempt in `_ATTEMPTS`
already matches this exactly. The fallback attempts stay in place for resilience against
other Anki versions this addon might run against, not because this one is still in doubt.

### ⚠️ Trap: calling `.exec()` / `.show()` on the returned dialog hangs Anki with nothing visible

Hit **twice** during testing, with two different "fixes" that both produced the identical
symptom — nothing else in Anki could be clicked, every click outside produced Windows' "an
action requires a modal dialog to be closed first" sound, and no window was visible to
close or interact with:

1. First attempt: `dialog.exec()`.
2. Second attempt (reasoned to be a fix, but wasn't): `dialog.show()` + `dialog.raise_()` +
   `dialog.activateWindow()`.

Both were wrong for the same underlying reason, now confirmed from the real source above:
**`CardLayout.__init__` already calls `self.show()` and sets
`Qt.WindowModality.ApplicationModal` itself**, before returning. Grepping every real call
site in Anki's own codebase confirms this is the intended usage — none of them do anything
with the constructor's return value at all:

```python
# aqt/editor.py, onCardLayout
CardLayout(self.mw, note, ord=ord, parent=self.parentWindow, fill_empty=False)
if is_win:
    self.parentWindow.activateWindow()   # note: the PARENT, not the CardLayout dialog

# aqt/models.py
CardLayout(self.mw, n, ord=0, parent=self, fill_empty=True)

# aqt/browser/sidebar/tree.py
CardLayout(self.mw, note, ord=item.id, parent=self, fill_empty=True)
```

Calling `.exec()` on an already-shown, already-application-modal `QDialog` starts a second,
redundant nested Qt event loop for the same widget — a known Qt anti-pattern, and the actual
cause of attempt 1's hang. Attempt 2's extra `.show()`/`.raise_()`/`.activateWindow()` calls
were likely harmless individually, but still deviated from real Anki's own usage, and in
particular **called `.activateWindow()` on the wrong object** — Anki's own `editor.py` calls
it on the *parent* window, on Windows specifically, as a documented post-construction step.

**Fix**: `preview_plan()`'s `on_success` (in `addon/ui/preview.py`, the single shared
implementation both `ConvertDialog` and `RoleMapperDialog` call) now does exactly what real
Anki does — construct `CardLayout` as a bare statement, touch nothing on the return value,
and on Windows only, call `parent.activateWindow()` (the caller's own window, e.g. the
`ConvertDialog`/`RoleMapperDialog` instance) afterward, mirroring `editor.py`'s `is_win`
branch verbatim. `mw.garbage_collect_on_dialog_finish(self)` inside `CardLayout.__init__`
means this addon doesn't need to hold its own reference for GC safety either — an earlier
`_open_windows` list serving that purpose was removed as unnecessary once this was clear.

**This fix (attempt 3) still did not resolve it.** Reported symptom this time: clicking
Preview visibly took focus away from the calling dialog (described as looking like Alt-Tab),
then the same "everything blocked, nothing visible, Windows plays its can't-click sound"
state as before — despite this now matching real Anki's own usage exactly.

Traced the full call chain against real source (`aqt.operations.CollectionOp`,
`aqt.taskman.TaskManager`, `aqt.progress.ProgressManager`, `anki.notes.Note`) to rule out
several plausible theories:
- **Threading**: `CollectionOp`'s `success()` callback is confirmed to run on the main GUI
  thread (`taskman.py`'s `run_on_main` marshals it via a queued Qt signal), not the
  background worker thread. Not the cause.
- **Stale collection reference**: `Note.col` (`anki/notes.py`) is a `weakref` to whatever
  `col` it was constructed with; `CollectionOp` always passes the real `mw.col`
  (`operations/__init__.py`: `self._op(mw.col)`), which stays alive for Anki's whole
  session. Not stale by the time `on_success` runs.
- **Progress-dialog race**: Anki's own `ProgressManager.start()` schedules its "please wait"
  window to actually become visible only after a 600ms `_show_timer` (`progress.py`), to
  avoid flicker on fast ops — but `finish()` explicitly stops that timer before our
  `on_success` runs if the op already completed (which a tiny scratch-note write does well
  under 600ms). Anki's own code already guards against this race. Not the cause.

Working theory, not yet confirmed: `CardLayout` embeds a Chromium-based `AnkiWebView` for
its live preview pane. Constructing that specific kind of widget *synchronously inside*
`CollectionOp`'s still-unwinding future-done callback chain (rather than as a fresh
top-level event-loop tick, which is how every direct-button-click call site in real Anki
invokes it) is a plausible, known class of Qt/WebEngine flakiness. **Attempt 4**: defer the
construction with `QTimer.singleShot(0, show_card_layout)` from inside `on_success`, so it
runs on a clean event-loop iteration instead. Not yet confirmed against a real run.

**Diagnostic result (confirmed by the user)**: Anki's own native Card Types editor
(Browser → select a card → `Ctrl+L`) opens fine with this addon not involved. This rules out
graphics driver / QtWebEngine-on-this-machine as the cause, and rules out "any CardLayout
anywhere freezes" — it's specific to opening one via this addon's call path. **Attempt 4
(the 0ms `QTimer.singleShot` deferral above) still did not fix it either**, same symptom.

**Attempt 5, current**: the one thing genuinely different between our path and every real
Anki call site (`editor.py`, `models.py`, `browser/sidebar/tree.py`) is that ours opens
`CardLayout` immediately after a `CollectionOp` that just created/updated a **notetype**.
`CollectionOp`'s completion handler (`on_op_finished` in `aqt/operations/__init__.py`) fires
Anki's `state_did_reset` hook whenever `mw.col.op_made_changes(changes)` says the change was
broad enough — a notetype change qualifies. That hook can trigger a wider main-window UI
reset, plausibly colliding with a freshly-opening modal `CardLayout` (which itself evicts
the notetype from Anki's model cache in its own `__init__`, another point of contact with
the same reset). A same-tick `QTimer.singleShot(0, ...)` apparently wasn't enough separation
from that cascade.

Anki's own source hits this same category of problem elsewhere and has a real, concrete fix
for it: `aqt/taskman.py`'s `with_backend_progress` schedules its own `on_done` via
`self.mw.progress.single_shot(100, lambda: on_done(fut), ...)`, commented **"allow the event
loop to close the window before we proceed."** Applied the same real value as attempt 5 —
`QTimer.singleShot(100, show_card_layout)` instead of `0`. **This also did not fix it**
(confirmed by the user). At that point the timing-delay direction was abandoned as a dead
end: every attempt shared the same root cause (a real collection write triggering
`state_did_reset` right as a new modal dialog opens), and no amount of delay tuning is a
structural fix for that -- only removing the write is. See the resolution banner at the top
of this section for what actually shipped.

## Design notes

- **Cloning**: `deepcopy(source)` → `["id"] = 0` → name it → `add_dict`, rather than
  `col.models.copy()`. Naming is our own `_unique_notetype_name()` (see the trap above),
  not `ensure_name_unique`. Gives us control of the name and one predictable add step
  across versions.
- **Field maps**: the clone keeps every source field at its own name and ord, so the
  change-notetype map Anki prefills is an identity map *for those fields*. Role mapping
  drives template HTML only, never field migration.
  **This is now the "we add fields on the clone" case the line below used to say was
  hypothetical** — see the next section.
- **`trash_files` vs delete**: trashing is recoverable, which is why the (M5) media cleanup
  uses it.
- **Preview writes nothing at all**: see the `aqt.clayout.CardLayout` section above --
  `open_live_preview()` opens a real note's real notetype and injects generated templates
  via `CardLayout`'s own in-memory live-editing mechanism, never touching the collection.
