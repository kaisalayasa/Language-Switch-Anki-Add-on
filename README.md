# Deck Direction Converter

An Anki addon that flips a bilingual deck's direction — *Language A front / Language B back*
becomes *Language B front / Language A back* — using a local LLM (Qwen2.5-7B-Instruct, run
through [llama.cpp](https://github.com/ggml-org/llama.cpp)) to read the deck's real content and
write the converted card's templates directly, then generates natural TTS audio for the
newly-fronted language using [Piper](https://github.com/rhasspy/piper), locally and offline.
Both models run entirely on-device — nothing about a deck's content, or how it gets converted,
is ever sent anywhere.

**Status:** the core pipeline — Analyze, Convert (both modes), Generate TTS audio, live preview
(including right after Analyze and before Convert), reopening an already-converted deck to
generate more audio without a fresh AI call, and hiding/showing individual fields without
touching HTML — is built, covered by 316 unit tests (`python -m unittest discover -s tests -t .`,
no Anki or network required), and confirmed working end-to-end against a real Anki profile. One
known item is still open, tracked in [`TODO.md`](TODO.md): a placement-compliance check
`llm/validate.py` doesn't yet make (one specific test deck is occasionally flaky on direction).
License (MIT vs. GPL) is still an open, deliberately deferred decision — see Milestones below.

## Why a local LLM instead of a hand-written mapping system

This addon used to work the other way around: a role-mapping system (`Word`/`Sentence`/`Audio`/…
role tags, content-based auto-detection heuristics, and a template generator that turned a
role→field dict into HTML) drove the whole conversion. It worked, but every new deck shape
tended to expose a heuristic that didn't generalize — a field mixing real text with its own
embedded audio, a deck whose fields were literally named `Front`/`Back`, and so on each needed
its own special case.

The LLM approach replaces all of that: given a notetype's real fields, real sample values, and
its current CSS, the model writes the finished Front/Back/CSS HTML directly. What it does
**not** do is decide direction, language, or field placement — those are computed in Python
(`addon/llm/direction.py`) and simply handed to the model as a given fact. That split exists
because the model was tested on deciding direction itself, four separately-phrased ways, and
failed all four identically (see `docs/llm-notes.md` and `CLAUDE.md`'s "LOCAL LLM DECK ANALYSIS"
section for the full account) — it's an abstraction the model reliably gets right, wrapped
around a reasoning task it reliably does not.

## The two modes

| | What happens | Original deck |
|---|---|---|
| **New deck** (default) | Clones the notetype, rewrites the template, **duplicates** the notes into a brand-new deck | untouched |
| **Flip in place** | Clones the notetype, rewrites the template, **repoints** the existing notes onto the clone | its cards are replaced |

Scheduling is **always** reset, in both modes. The old interval and ease describe a skill —
recognition in the original direction — that was never practised in the new direction, so
carrying it over would be actively misleading.

A conversion's real writes (the clone, the notes, the scheduling reset) always happen before
Anki's own undo-grouping step, which is why a `merge_undo_entries` hiccup there (Anki's
`"target undo op not found"`, seen occasionally in real use) is recovered from with a
same-tick retry instead of being reported as a failed conversion — see `apply_plan` in
`notetype_manager.py` and `docs/api-notes.md` for the full writeup.

The original **notetype object** is never modified in either mode.

## Design rules

- **`addon/core/` and `addon/tts/` are pure**, and `addon/llm/` is pure except for the one
  real subprocess call each in `llm/client.py`/`llm/runtime.py`, always dependency-injected out
  in tests. No `anki`, no `aqt`, no language name, no script name, no deck-specific field name in
  `core/` (the LLM prompt itself is the one deliberate exception — `llm/prompt.py` — since
  describing languages is its entire job; see its module docstring). Enforced by
  `tests/test_purity.py`, not by convention.
- **Never guess an Anki or llama.cpp API.** See `docs/api-notes.md` and `docs/llm-notes.md`.
  (`col.sched.forget_cards` does not exist — it's `schedule_cards_as_new`. `-no-cnv` does not
  exist on the pinned llama.cpp build — it's `--single-turn`.)
- **Direction, language, and audio-field placement are computed, never asked of the model or
  the user.** `llm/direction.py`'s `resolve_direction` is the single source of truth; see
  `CLAUDE.md` for why.
- **A deck's pre-existing audio can never play on the converted card.** Enforced deterministically
  (`llm/audio_safety.py`) on the model's output regardless of what it actually wrote — see
  `CLAUDE.md`'s "LOCAL LLM DECK ANALYSIS" section.
- **Refuse rather than misalign.** If a plan's field names disagree with the live notetype, or
  a schema-level Anki call returns something this addon doesn't recognise, the run stops
  (`ApiMismatch`) instead of writing the wrong content into every note.

## Layout

```
addon/
  core/        pure logic — conversion (plan/validate/preflight), deck_state (records which
               notetypes this addon already converted, and to what), audio_fields (naming and
               recognition for generated-audio fields), language_detect
  llm/         local LLM pipeline — runtime/model_manager (download+cache llama.cpp and
               Qwen2.5-7B), client (runs one completion), prompt/response (build the prompt,
               parse the reply), direction/template_fields/audio_safety (deterministic
               direction, placement, and audio-safety logic the model never decides), validate
               (checks the model's output against real failure modes), analyze (orchestrates a
               full Analyze call, retries, trust rating)
  tts/         pure logic — Piper binary/voice managers, subprocess provider, sanitizer,
               script_ranges (voice locale -> pronounceable characters)
  ops/         everything that imports anki/aqt — notetype cloning/safe-apply, conversion as a
               CollectionOp, TTS batch execution and its Qt-facing runner, deck_data (reads
               real note samples for the LLM prompt)
  ui/          main_screen (the single entry point), preview_panel (embedded live preview),
               piper_test_dialog (standalone dev/debug voice check)
  user_files/  gitignored, per-machine: Piper binary/voice cache, llama.cpp runtime + model cache
  vendor/      committed third-party source (langdetect + six) — see vendor/README.md
tests/         stock-Python unit tests, no Anki required, no network required
tools/         install_dev.py, build_ankiaddon.py
docs/          deck-facts.md (verified ground truth for the Core 2000 test deck), api-notes.md
               (verified Anki API facts), llm-notes.md (verified llama.cpp/Qwen facts)
```

## Development

```bash
python -m unittest discover -s tests -t .   # 289 tests, no Anki needed, no network needed
python tools/install_dev.py --link          # install into Anki (close Anki first)
```

Then in Anki: **Tools → Deck Direction Converter…** — the single entry point
(`addon/ui/main_screen.py`). There's also a separate **Test Piper voice… (dev)** action for
standalone voice sampling; it shares no code with the main screen and exists purely as a dev/
debug tool. (An earlier Tools submenu holding separate Convert/Preview/Generate-TTS dialogs,
built around the deleted role-mapping system, has been removed — the single screen replaced it,
exactly as originally planned.)

### The single screen, top to bottom

1. **Pick a (deck, notetype) pair and a mode** (New deck / Flip in place — see above). Pairs are
   read live from the collection (`ops/deck_data.decks_with_notetypes`), scoped by both deck and
   notetype since either alone can be ambiguous.
2. **Analyze** — sends a handful of real sample notes (`ops/deck_data.collect_field_samples`,
   capped to match what the prompt actually uses) plus the current templates to the local model
   and gets back a `DeckAnalysis`: a plain-language description, the finished Front/Back/CSS, the
   detected target/native language, which fields ended up on which new side, and a 1-5 star trust
   rating (never something the model reports about itself — computed from how many retries
   validation actually needed; see `CLAUDE.md`). Below 5 stars, a plain-language review message
   nudges the user to check the preview carefully, or at 1 star to consider hand-editing before
   converting.
   If this (deck, notetype) pair was **already converted** in an earlier session, Analyze reads
   that back (`core/deck_state.state_from_notetype` — a tag plus a CSS comment marker, either one
   surviving `.apkg` export/import) and feeds it in as `known_state`, so re-Analyzing an
   already-converted deck can't flip it back to its original direction — see `llm/direction.py`.
3. **Review** — the right-hand pane is a live, embeddable preview (`ui/preview_panel.PreviewPanel`,
   an `AnkiWebView` fed via `note.ephemeral_card()`, never writing to the collection) showing the
   card Analyze just produced. The left-hand pane defaults to a read-only summary of the analysis;
   toggling "HTML" swaps it for a raw Front/Back/Styling text editor pre-filled with the same
   content — whatever that editor holds is exactly what Convert uses, whether it came straight
   from Analyze, hand-edited, or changed via "Hide fields" below.
   **Only fields the original card actually showed are placed at all:** a field never referenced
   anywhere in the *original* `qfmt`/`afmt` (a deck's own bookkeeping/index columns — Core 2000's
   `Core-Index`, `Optimized-Voc-Index`, etc., confirmed against the real deck export never being
   referenced in its own template) is excluded from placement entirely by `llm/direction.py`, the
   same way a not-yet-existing generated-audio field already was — and never shown to the model at
   all (`llm/analyze.py` filters the prompt down to exactly what got placed). Fixing this stopped a
   real, observed bug where every one of a deck's hidden bookkeeping fields was rendering on the
   converted card's back, something the original card never did.
   **Hide fields** — a third left-pane mode, next to "HTML", for turning an already-placed field's
   display on or off without touching HTML at all: a checkbox list of exactly the fields currently
   referenced on the Front/Back (`llm/field_visibility.py`'s `visible_fields`), split by side.
   Unchecking one wraps its `{{Field}}` reference in a small, reversible `<span class="ddc-hidden">`
   (paired with one `display: none` CSS rule, the same technique the real Core 2000 CSS already
   uses for its own `.ios-only`/`.mac-only` toggles) — the field's data and its surrounding markup
   are untouched, only whether it renders changes, and re-checking the box restores the exact
   original HTML. A field the deck never showed in the first place (per the paragraph above) isn't
   listed — there's nothing to toggle for a field that was never part of the card.
4. **Convert** — writes the reviewed templates via a clone-notetype flow (`ops/notetype_manager`,
   `ops/convert_op`) in the chosen mode, always resetting scheduling, and records the conversion
   (`core/deck_state`) so this pair is recognised as converted from now on.
5. **Generate TTS audio** — batch-synthesizes audio for every field `llm/direction.py` decided
   needs it (usually two per note: a word/term and a full sentence, each into its own field —
   never one shared field) via Piper, with visible progress and a real Stop button, resumable
   (a re-run only touches notes without the `ddc-tts-generated` tag), and safe to run partially or
   repeatedly — every audio field starts empty and is referenced with a conditional, so a note
   the batch hasn't reached yet simply renders nothing rather than stale or wrong-language audio.
   For a pair **already known converted**, this step needs no fresh Analyze call at all: which
   fields to speak and which fields their audio goes into is fully recomputed locally
   (`llm.direction.resolve_direction`, no AI call) every time the pair changes, so adding more TTS
   to an already-converted deck later is instant. Voice selection defaults to `en_GB-alba-medium`
   (`addon/config.json`'s `tts_voice`); "Sample" speaks the *currently selected note's* own
   target-language text rather than a canned phrase.

Both the local LLM and Piper download their binary/model files on first use into
`addon/user_files/` (gitignored, never wiped by an addon update) — a few hundred MB for Piper's
binary + voice, roughly 4.7GB for the split Qwen2.5-7B-Instruct GGUF plus the llama.cpp runtime.
Every later run reuses the cache. The Analyze progress message distinguishes "downloading the
model" from "already downloaded, just analyzing" so a long first run doesn't look identical to a
stuck one.

To install a real, packaged copy (rather than the dev symlink above):

```bash
python tools/build_ankiaddon.py   # writes dist/deck_direction_converter-<version>.ankiaddon
```

Then Anki → Tools → Add-ons → Install from file… This is a small local script, not the
community `anki-addon-builder` (`aab`) tool — `aab` expects the addon to live under
`src/<module_name>/` with a repo-root `addon.json`, and is built around git-tag-based versioning
for publishing to AnkiWeb. Adopting it would mean restructuring this repo for a public-release
workflow this project hasn't committed to yet (license — MIT vs. GPL — is still an open
question, deliberately deferred). This script does the one thing needed for now: produce a file
Anki can actually install, for testing outside the dev symlink. It excludes `addon/user_files/`
(a fresh install shouldn't inherit the packager's locally-downloaded model/voice cache).

Develop against a scratch profile, not your real collection. Every operation is scoped to a
single (deck, notetype) pair, but in-development code writes to the same `collection.anki2`
that holds everything else.

## Milestones

M1 proof of concept · M2 Piper integration · M3 role-mapping UI · M4 language detection ·
M5 batch apply · M6 profiles · M7 release prep

**M1-M6 and the packaging half of M7 were completed against the original role-mapping
architecture, then that entire architecture was deleted.** The LLM overhaul (see "Why a local
LLM" above) replaced the role schema, content-based role detection, the template generator, the
profiles system, and the role-mapper/field-list UI wholesale with the pipeline described in
`CLAUDE.md`'s "LOCAL LLM DECK ANALYSIS" section, and replaced the old Tools submenu of separate
dialogs with the single screen described above. What M1-M7 actually discovered along the way —
the notetype-cloning safety rules, the undo-boundary rules, the "audio in a new field, not a
reused one" policy, the wrong-script-filter trap — all still hold for the system that replaced
it; only the specific role-mapping code is gone.

**Deliberately not done:** a LICENSE file, `manifest.json` license metadata, and a contribution
guide — all need the MIT-vs-GPL decision, which is intentionally on hold ("I don't know
anything about open source, leave it for later"). Nothing in the addon depends on that
decision; it only blocks the parts of a public release about being ready for outside
contributors.

**Known limitation, not a bug:** the curated Piper voice list (`addon/tts/piper_voice_manager.py`)
ships English voices only — `en_US-lessac-medium` and `en_GB-alba-medium`. A deck whose
*newly-fronted* language is not English has no voice to generate with yet, even though Analyze
and Convert already work for any language pair the local LLM can read. Piper publishes voices
for many languages; widening the curated list is a data change, not a code one.

See [`TODO.md`](TODO.md) for the current, actively-tracked list of open bugs and follow-ups.
