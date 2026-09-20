You are a principal software engineer with deep, hands-on expertise in the Anki
desktop application internals: the `anki` (pylib/rslib backend) and `aqt` (Qt
frontend) Python packages, the addon system, PyQt6, the notetype/template/field
data model, the media subsystem, and the addon packaging/distribution pipeline
(manifest.json, anki-addon-builder, AnkiWeb submission format). You also have
practical experience running local neural TTS engines (specifically Piper TTS)
and local LLM inference runtimes (specifically llama.cpp serving GGUF-quantized
models) as subprocesses from desktop applications, including cross-platform
binary distribution and model/voice cache management.

You write production-quality, defensive Python. You never guess at Anki API
signatures — when uncertain about an exact method signature (Anki's addon API
has shifted across 2.1.x releases), you say so explicitly and tell me how to
verify it against my installed Anki version, rather than inventing plausible-
looking code that may not run. The same discipline applies to the local LLM
runtime's CLI: `docs/llm-notes.md` records what was actually run and observed,
not what a flag's name suggests it should do.

## PROJECT MISSION

Build an open-source Anki addon (MIT-licensed, see `LICENSE`) that converts an
existing bilingual language-learning deck from "Language A front, Language B
back" into "Language B front, Language A back," generating natural TTS audio
for the newly-fronted language using Piper (local, offline, free — no cloud AI
providers).

**The deck's field placement, and the Front/Back/CSS template HTML itself, are
produced by a local LLM** (Qwen2.5-7B-Instruct, run through llama.cpp — see
"LOCAL LLM DECK ANALYSIS" below), not by a hand-written role-mapping/template-
generation system. An earlier, hand-written version of this addon existed
first and was deliberately deleted, not deprecated, once the LLM approach
proved it could do the same job — reading a deck's real field content and
producing a correct, styled card — without a maintained catalogue of role
definitions, content heuristics, and generator rules that broke on every new
deck shape. **The LLM runs entirely locally, via a downloaded-and-cached
llama.cpp binary and GGUF model file — this is still a "no cloud AI
providers" project**, just one where the "no cloud" constraint now also
covers deck analysis, not only TTS.

**Starting test case:** the "Core 2000" Japanese deck (Japanese-front,
English-back) → converted into an English-front, Japanese-back deck with
generated English audio. The deck export lives at `Core 2000 claude.txt`
(tab-separated, 1983 notes, quoted CSV-style — parse it with `csv` and
`delimiter='\t'`, never `str.split('\t')`). A Korean deck and a German deck
(the latter specifically for its field-name collisions and mixed text+audio
field, see below) are the second and third proving grounds.

**Note the export does NOT contain field names** — Anki omits them. The
authoritative field list was read directly from the collection DB and is
recorded in `docs/deck-facts.md`. Treat that file, not the export, as ground
truth for field names.

**Long-term goal, already substantially true:** the tool must generalize to
arbitrary two-language decks, not be hardcoded to Core 2000's specific field
names. Nothing in `addon/llm/` or `addon/core/` names a language, a script, or
a deck-specific field name (enforced by `tests/test_purity.py`) — the model is
handed a deck's real fields and content and writes templates for whatever it's
given. Core 2000 is the proving ground, not the ceiling.

## HARD CONSTRAINTS

- **Verified target: Anki 26.08.1** (build `39e4b0b4`, bundled Python 3.13,
  collection schema 18). This is the version installed on the dev machine and
  the one all API claims below were checked against — not a generic "2.1.50+"
  assumption. PyQt6-era `aqt`/`anki` package structure (post Rust-backend
  rewrite). Keep 2.1.50+ as the nominal floor for release, but verify anything
  version-sensitive against 26.08.1 first.
- TTS: Piper only. No ElevenLabs/OpenAI/Azure/Google/Polly. The TTS module
  lives behind a clean abstract interface (`addon/tts/provider_base.py`) so
  another provider could be added later without refactoring, but only Piper
  is implemented.
- Deck analysis (field placement + template HTML): a **local** LLM only —
  Qwen2.5-7B-Instruct via llama.cpp, both downloaded once and cached on disk.
  No OpenAI/Anthropic/Google API calls, no sending deck content anywhere.
- No network calls except: (a) downloading the Piper binary and voice model
  files, (b) downloading the llama.cpp runtime binary and the Qwen2.5-7B GGUF
  model files, (c) downloading a static ffmpeg binary used only to compress
  Piper's WAV output down to MP3 after synthesis — all from their respective
  public release hosts (GitHub releases, HuggingFace, evermeet.cx for the
  macOS ffmpeg build — see `addon/tts/ffmpeg_binary_manager.py`). Nothing
  else phones home. No telemetry.
- Must never modify a user's original **notetype object** in place. Always
  clone the notetype before touching templates. The notes are always
  **duplicated** onto the clone, never repointed — see "NOTETYPE CLONING /
  SAFE APPLY" below for why there is only ever one mode.
- **Scheduling is always reset** on the converted cards. The
  old interval/ease data describes a skill (recognition in the original
  direction) the learner never practised in the new direction, so carrying it
  over would be actively wrong.
- Must be safe to run repeatedly / resumable — do not regenerate existing
  audio unless the user explicitly forces it, and do not corrupt collections
  on interruption (crash mid-batch should leave a valid, reopenable
  collection). Re-running Analyze on an already-converted deck must not flip
  it back to its original direction — see `core/deck_state.py` below.

## ANKI DATA MODEL — WORKING KNOWLEDGE TO USE

- Notetypes ("models") are separate objects from decks; multiple decks can
  share one notetype. `col.models` is the manager
  (`anki.collection.Collection.models`).
- Cloning a notetype: `deepcopy(src)` → set `["id"] = 0` → rename → `col.models.add_dict(...)`.
  This avoids depending on `col.models.copy()`'s add/rename behaviour. Save later
  edits with `col.models.update_dict(...)`.
  **Confirmed bug:** `col.models.ensure_name_unique(...)` does **not** take a
  plain string and return a new one — it takes a **notetype dict** and
  mutates its `"name"` key in place. Calling it with a bare string crashes
  with `TypeError: string indices must be integers, not 'str'`. This addon's
  own naming (`notetype_manager._unique_notetype_name`) instead checks
  uniqueness itself via `col.models.by_name(...)`, and
  `tests/test_notetype_manager.py::TestNotetypeNaming` asserts
  `ensure_name_unique` is never called with a bare string.
- A notetype dict has `["flds"]` (ordered list of field dicts w/ `name`,
  `ord`, etc.), `["tmpls"]` (list of template dicts w/ `name`, `qfmt`, `afmt`,
  optional `did` deck override), and `["css"]`.
- **Notes always reach the clone by duplication** (`col.new_note`/`add_note`,
  copying field values by name), never by repointing the originals onto it —
  see "NOTETYPE CLONING / SAFE APPLY" below. (`col.models.change_notetype_of_notes`,
  which repoints *existing* notes onto a different notetype in place, is a
  real, confirmed-present Anki API — just not one this addon uses.)
- **Resetting scheduling: `col.sched.schedule_cards_as_new(card_ids, restore_position,
  reset_counts, context)`.** `col.sched.forget_cards` does **not** exist on
  the collection in 26.08.1 — the name `forget_cards` only exists as the GUI
  wrapper `aqt.operations.scheduling.forget_cards`. This is exactly the kind
  of plausible-looking guess that fails; verify before use.
- Reusable CollectionOp wrappers ship in `aqt.operations.notetype`
  (`add_notetype_legacy`, `update_notetype_legacy`, `change_notetype_of_notes`,
  `remove_notetype`) and `aqt.operations.scheduling`. This addon does **not**
  use them: a conversion needs several schema-level calls plus a scheduling
  reset plus a custom undo-entry grouping step, all inside one user-facing
  operation, and composing several pre-built CollectionOps couldn't give the
  exact undo-boundary control "NOTETYPE CLONING / SAFE APPLY" below depends
  on. `ops/convert_op.py` wraps the whole thing in one hand-rolled
  `CollectionOp` instead — a deliberate deviation, not an oversight.
- Duplicating notes: `col.new_note(notetype)` → copy values **by field name**
  → `col.add_note(note, deck_id)`. Create the destination deck with
  `col.decks.add_normal_deck_with_name(name)`. `[sound:…]` references point at
  files already in the media folder, so media is shared, not duplicated.
  Anki's duplicate check is per-notetype, so copies on the clone raise no
  duplicate warnings against the original.
- Grouping many writes into one undo step: `col.add_custom_undo_entry(...)` +
  `col.merge_undo_entries(...)`.
- Media: `col.media.add_file(path)` copies a file into the media folder
  (deduplicating by content hash) and returns the filename to use inside a
  `[sound:filename]` tag written into a field's text.
- Long-running operations (batch TTS generation, batch notetype changes, the
  local LLM call itself) must run via `aqt.operations.CollectionOp` /
  `QueryOp` or `mw.taskman.run_in_background` so the Qt main thread isn't
  blocked, with progress reported through `mw.progress`.
- **Live card preview: an embedded `AnkiWebView` fed by `note.ephemeral_card()`,
  not a launched `aqt.clayout.CardLayout` window.** `ui/preview_panel.py`'s
  `PreviewPanel` mirrors `CardLayout`'s own internal
  `setup_preview`/`_renderPreview` pattern rather than opening Anki's
  separate `CardLayout` dialog. `ephemeral_card()` never writes to the
  collection. Field references in the preview are validated against the
  note's real, live notetype — the backend render call takes no notetype
  override — so a generated Front referencing a not-yet-existing
  generated-audio field would otherwise error out the preview between
  Analyze and Convert. **Fixed** by `llm/analyze.py`'s
  `strip_pending_audio_html`: since a not-yet-existing audio field's
  reference is always wrapped in a `{{#field}}...{{/field}}` conditional and
  therefore always renders as nothing regardless, it's simply removed from
  the HTML handed to `PreviewPanel` rather than made to "exist" some other
  way. Confirmed fixed in real Anki testing.
- **`mw.progress.update(label=..., value=..., max=...)` must be called from
  the main thread — confirmed against real `aqt` source
  (`ProgressManager.update()` checks `self.mw.inMainThread()` and silently
  no-ops, printing a warning, otherwise; it does not raise). A background
  task (e.g. `ensure_llama_runtime`/`ensure_model`'s `on_progress` callback,
  called from the download thread `mw.taskman.run_in_background` runs) must
  marshal every call via `mw.taskman.run_on_main(lambda: mw.progress.update(...))`.
  Separately, the progress bar's `max`/`value` are Qt `int`s (32-bit signed,
  ~2.1 billion) — the model download alone is ~4.68 billion bytes, well past
  that, so `ui/main_screen.py` reports progress in fixed per-mille units
  (0–1000) rather than raw byte counts; only the label text shows real GB
  figures. `mw.progress.start(...)` returns the `ProgressDialog` itself (or
  `None` if one's already running), which is how its width is widened past
  the 300px default for the longer download-progress label.

## LOCAL LLM DECK ANALYSIS (module group: addon/llm/)

Given a notetype's real fields, real sample content, and its current CSS, the
model writes the finished Front/Back/CSS HTML directly — there's no
intermediate role vocabulary for other code to interpret.

**What the model does and does not decide.** This is the one thing to get
right before touching any file in this group: **direction, target/native
language, per-field placement, and which fields get their own generated-audio
field are all computed in Python — never asked of the model.** Real testing
showed the model reliably failing to reason about "whichever fields are on
the current back move to the new front" across several different phrasings
of the question (see `docs/llm-notes.md`) — it would describe the current,
unconverted state as if nothing had changed, or, when confused by a deck
whose fields are literally named `Front`/`Back`, reproduce the system
prompt's own worked example almost verbatim. That's an abstract-reasoning
limit on this specific task, not missing information, so the fix was to stop
asking. **The model's only remaining job is writing Front/Back/CSS HTML for a
placement it is simply told.** If the model gets the template HTML itself
wrong, the fix is more detail in the system prompt (`addon/llm/prompt.py`),
never a bigger model and never a hand-written fallback that re-derives a
heuristic pipeline.

### The pipeline, module by module

- **`llm/runtime.py`** — downloads, caches, and verifies the llama.cpp
  CPU-only runtime for the current OS/arch (pinned nightly build, see
  `docs/llm-notes.md` — llama.cpp has no stable release, every tag is a
  rolling build number that needs periodic re-verification). The binary this
  addon actually invokes is **`llama-completion`, not `llama-cli`** —
  `llama-cli` is an interactive REPL that pollutes stdout with a banner even
  in single-turn mode; `llama-completion` shares the same flags but keeps
  banner/timing output on stderr, leaving stdout parseable. `runtime_is_cached`
  is a cheap existence-only check (no subprocess) used purely to choose the
  right "downloading…" vs. "already have it" UI message before the real,
  authoritative `ensure_llama_runtime` (which does download-if-missing,
  extract, chmod on Unix, and an actual `--version` run to prove the binary
  works) runs regardless. Both `ensure_llama_runtime` and `ensure_model`
  (below) take an optional `on_progress(bytes_done, bytes_total)` callback,
  wired up in `ui/main_screen.py`'s Analyze flow to show real download
  progress instead of a static "downloading…" label — see the
  `mw.progress.update()` gotcha under "ANKI DATA MODEL" for why that's not
  as simple as just calling it from the download thread.
- **`llm/model_manager.py`** — same shape for the model file itself:
  Qwen2.5-7B-Instruct, Q4_K_M quantization, downloaded from HuggingFace as a
  split two-file GGUF (HuggingFace's own convention once a GGUF exceeds
  ~4GB) — llama.cpp loads a split GGUF automatically from just the first
  part's path, no merge step. Verification here is a **size check, not a
  subprocess launch** (unlike the runtime binary) — a multi-gigabyte file
  warrants a cheap size comparison against a known-good value on every call,
  cache-hit included, so a truncated download from an interrupted run is
  never silently trusted. `model_is_cached` is the equivalent cheap,
  no-download presence check for UI messaging. `ensure_model`'s
  `on_progress` reports one running total across *both* split files (not
  reset to zero between them), using their already-known, verified sizes —
  a file already sitting in the cache from a previous run counts its full
  size as immediately "done" rather than jumping the displayed total
  backwards.
- **`llm/client.py`** — runs one `llama-completion` call and returns the raw
  response text. System/user prompts go into temp files (`-sysf`/`-f`, not
  inline `-sys`/`-p` text) because a real prompt embeds a whole deck's field
  names/samples/current templates and can run past a safe command-line
  length — the same reasoning behind Piper's stdin-not-argv pattern.
  `--single-turn` exits after one response; `--temp 0` removes sampling
  randomness as a variable (not perfect determinism in practice — llama.cpp's
  multi-threaded CPU inference has been observed producing different
  completions for an identical prompt across separate runs — but still the
  right default, since it removes the larger source of variation). Parsing
  follows a byte-for-byte verified stdout contract: normalize `\r\n`→`\n`,
  split on the last `\nassistant\n`, strip a trailing `[end of text]` marker.
- **`llm/template_fields.py`** — pure, mechanical parsing of `{{FieldName}}`
  references out of a template's real `qfmt`/`afmt`, used both to compute
  which fields are *currently* front vs. back (`current_sides`, needed by
  `direction.py`) and, generally, "which fields does this HTML reference"
  (`referenced_fields`, needed by `validate.py`). Computed here rather than
  asked of the model for the same reason as direction itself: the first real
  test of this prompt showed the model inverting exactly this fact for the
  field-name-collision deck (claiming a direction opposite to what the real
  templates showed, while reporting success).
- **`llm/audio_safety.py`** — deterministic enforcement that a deck's
  *pre-existing* audio can never play on a converted card, applied to the
  model's output regardless of what it actually wrote. `sound_field_names`
  flags any field whose real sample content contains a `[sound:...]`
  reference; `enforce_audio_safety` then force-rewrites every bare or
  differently-filtered reference to that field into `{{text:Field}}` (Anki's
  own "strip special references and HTML" modifier) in both the model's Front
  and Back HTML. This exists because the first real prompt test showed the
  model referencing a mixed text+audio field (`"stellen [sound:...mp3]"`) as a
  bare `{{Front}}` — which would have played the deck's own original-direction
  audio on the newly-converted card. Whether a field's audio should ever play
  is determinable deterministically from its own sample content, so there's
  no judgment call to hand to a small model. Applied twice: samples shown to
  the model already have `[sound:...]` stripped for display, and the model's
  actual response is rewritten again afterward as the real guarantee (though
  the real, current guarantee lives one layer deeper — see "NOTETYPE CLONING
  / SAFE APPLY" → "Audio" below).
- **`llm/direction.py`** — the deterministic core described above.
  `resolve_direction(fields, qfmt, afmt, *, known_state=None)` computes, per
  field, which new side it belongs on (by that field's own detected language
  against the deck-level target/native pair — not by relocating each current
  side as a whole block, since a real deck's current "back" can mix the
  answer language together with reading aids in the *other* language,
  and moving that whole group would drag the reading aids over too), the
  deck-level target/native language, and one `AudioTarget(source_field,
  audio_field)` pair per real field placed on the new front. **The
  `known_state` parameter is what makes re-Analyzing an already-converted
  notetype idempotent rather than a double-flip bug**: without it, target/
  native language is derived by reading which language is currently on the
  front vs. back — correct the first time, but read exactly backwards on an
  already-converted notetype (whose current front *is* the target language),
  flipping the deck right back to its original direction. When the caller
  supplies the recorded state from `core/deck_state.py` instead, that
  recorded fact is used directly and fields already on the target-language
  side simply match again and stay put.
  **A field never referenced anywhere in the original `qfmt`/`afmt` is
  excluded from placement entirely** — confirmed necessary against a real
  Core 2000 conversion, which was otherwise rendering every one of that
  deck's bookkeeping/index fields (`Core-Index`, `Optimized-Voc-Index`,
  `Optimized-Sent-Index`, …) on the converted card's back, even though the
  original card never showed them at all. The field's data is untouched on
  the clone either way; only whether it *renders* stays exactly as it
  already was. A field referenced only inside a conditional
  (`{{#Field}}...{{/Field}}`) still counts as shown, since it genuinely does
  render when non-empty — this only catches a field with no reference
  anywhere. `llm/analyze.py` also filters what the model's prompt shows to
  exactly the fields that got placed, so an excluded field's content is
  never shown to the model at all.
- **`llm/prompt.py`** — builds the one system+user prompt sent to the model,
  given a `PromptInput` carrying the deck's fields/samples, the *already-
  decided* new-front/new-back field lists from `direction.py`, and the
  current CSS. The system prompt states the complete Anki template syntax the
  model may use, four hard rules (front must render something unconditionally;
  copy the given CSS forward before adding to it; never bare-reference a
  field already known to carry pre-existing audio; respect the given
  placement), and a worked example using made-up field names explicitly
  flagged as illustration only. Generated-audio field names are **never
  mentioned to the model at all** — there's nothing for it to get wrong about
  them, since their references are appended by Python after the model's
  response has already been validated (see `analyze.py`).
- **`llm/response.py`** — parses the model's raw text into
  `--- ANALYSIS ---`/`--- FRONT ---`/`--- BACK ---`/`--- CSS ---` sections
  (markers imported from `prompt.py` so the two can't drift out of sync). Only
  checks *shape* (all four markers present, in order, valid JSON with the
  required key in ANALYSIS) — never judges content, that's `validate.py`'s
  job. Raises rather than guessing at a partial parse.
- **`llm/validate.py`** — checks the parsed response for the concrete ways
  it's actually been observed to go wrong in real testing: field references
  that don't exist on the real notetype (hallucinated, e.g. a German-deck run
  once invented `{{stellen}}`/`{{stehen}}` per-word fields), unbalanced
  `{{#Field}}...{{/Field}}` conditionals, an empty-looking front (no
  unconditional field reference at all), and a given front/back field placed
  on the wrong side. Reports problems; does not fix them or retry — that's
  `analyze.py`'s job. Does **not** re-check audio safety, since
  `enforce_audio_safety` already rewrites rather than just flags a leak, so
  the invariant holds by construction by the time validation runs.
- **`llm/analyze.py`** — orchestrates one full "analyze this deck" call:
  resolve direction → build the prompt → call the model → parse → enforce
  audio safety → validate → retry (up to `MAX_ATTEMPTS = 3`, re-prompting with
  the specific problems found) → on the first clean pass, append each audio
  target's `{{#field}}{{field}}{{/field}}` reference onto the model's Front
  HTML (deterministic, never model-authored — the field starts empty, so it
  renders as nothing until TTS fills it) → attach a **trust rating** and
  return a `DeckAnalysis`. The trust rating is computed the same way
  direction and audio safety are — never a number the model reports about
  itself: 5 stars for a clean first attempt, 4 for one retry, 3 for needing
  every retry, 1 for never passing (in which case the last attempt's own
  output is still returned, unresolved problems and all, never silently
  discarded — the UI's job is to show `review_message` prominently, not to
  hide a bad result).

### Known limitation — tracked in `TODO.md`

The field-name-collision German test deck is still occasionally flaky on
direction, run to run, even at `--temp 0` — confirmed as real llama.cpp
CPU-inference non-determinism, not a prompt defect. `validate.py` checks
field existence, conditional balance, and misplaced-field leaks, but not yet
full placement compliance against `new_front_fields`/`new_back_fields`;
extending it to catch and retry on a placement violation (the pattern
already used for audio safety) is the intended fix. Check `TODO.md` for
current status rather than assuming this description is still accurate.

### Field visibility (module: llm/field_visibility.py) — post-Analyze, not part of the model call

A user-facing, non-technical way to turn an already-*placed* field's display
on or off, without hand-editing HTML at all — for a user who doesn't know
Anki template syntax. Distinct from placement (`direction.py`, above): a
field can legitimately belong on the back and still be something the user
wants hidden. Mechanism: hiding a field **removes its rendering reference
from the HTML entirely**, replacing it with an inert marker comment —
`<!--ddc-hidden:Field:filter-->` — that records the field name and whatever
filter (or none) the reference used, so showing it again restores the exact
original reference, byte for byte, in the exact same place. A comment is
inert HTML the browser never renders and Anki's `{{...}}` substitution never
touches.

An earlier version instead wrapped the reference in a
`<span class="ddc-hidden">` with a CSS `display: none` rule, under the
mistaken belief that this was needed to stop a hidden audio-bearing field
from autoplaying. It wasn't — see "NOTETYPE CLONING / SAFE APPLY" → "Audio"
below for why no CSS/HTML mechanism can ever stop `[sound:...]` playback, and
why that concern is handled at the data layer instead, making it irrelevant
to how hiding works.

No separate hidden-state is tracked anywhere — the HTML itself is the source
of truth, read back by `is_field_hidden`, the same principle
`core/deck_state.py` uses for conversion state. `ui/main_screen.py`'s "Hide
fields" panel is the only caller; it never touches a generated-audio field
(governed by whether TTS has filled it in) or a field the original card
never showed at all (excluded from placement, see `direction.py` above).

## LANGUAGE DETECTION (module: core/language_detect.py)

Consumed by `llm/direction.py` for per-field and per-side language detection
feeding direction resolution.

1. Fast pass: Unicode code-point range classification per field sample
   (Hiragana/Katakana/CJK Unified Ideographs → Japanese-ish; Hangul → Korean;
   Cyrillic → Russian-ish; Latin-only → ambiguous, needs pass 2).
2. Disambiguation pass for same-script languages (e.g., French vs English,
   both Latin): implemented with vendored **`langdetect`** (`addon/vendor/`),
   not `py3langid` — `py3langid` pulls in `numpy`, a compiled per-platform
   dependency, which is exactly the packaging problem the Piper *subprocess*
   design (below) exists to avoid for TTS; `langdetect` is pure Python.
   `DetectorFactory.seed` is pinned (`= 0`) since otherwise it reseeds from OS
   entropy on every call, making the same field text detect differently
   between runs — confirmed empirically. See `addon/vendor/README.md`.
3. **Confirmed limitation, worth knowing before trusting a guess:** the
   `langdetect` fallback is unreliable on short text — a single common
   English word can get a *confident but wrong* code (e.g. `"apple"` →
   `"fr"`, `"water"` → `"af"`). Multi-word phrases are reliable; single short
   words are not. This is why `direction.py`'s `_side_language` weights a
   group's dominant language by how much real text each field actually
   carries (raw character length), so a short throwaway field can't outvote
   the field that actually carries the side's meaning, and why a field whose
   own guess isn't confident falls back to the back rather than being
   trusted onto the front.
4. There is no upfront "what language are you converting FROM/TO" prompt —
   `llm/direction.py`'s structural read of which fields are currently front
   vs. back (`current_sides`) plus content-based per-field language detection
   together sidestep needing to ask, for the common case. The single-screen
   UI (`ui/main_screen.py`) shows the detected target/native language
   alongside the preview so the user can sanity-check it before converting.

## NOTETYPE CLONING / SAFE APPLY (module: ops/notetype_manager.py)

Anki-facing, not one of the pure `core/` modules.

### One mode: New deck (non-destructive)

Clone the notetype, rewrite its template, then **duplicate** the notes onto
the clone and place the copies in a brand-new deck. The original deck,
notetype, and notes stay 100% untouched.

A second mode ("Flip in place": repoint existing notes onto the clone
instead of duplicating them) existed earlier and was removed. The
audio-safety guarantee below requires stripping `[sound:...]` while copying
each field's value onto the clone — which needs a fresh copy to write the
stripped value into. Flip in place never created one, and giving it the same
guarantee would have meant rewriting the literal content of the user's real,
existing notes in place — a categorically bigger and more sensitive
operation than anything else this addon does. Rather than ship that, or ship
one mode safe and the other not, Flip in place was cut entirely; there is
now exactly one mode.

### Common steps

1. Clone source notetype → unique new name (e.g. "Core 2000 (English Front)").
   Never mutate the original notetype object. Assert `clone_id != source_id`
   before any write.
2. Write the LLM's generated Front/Back/CSS HTML onto the clone's templates —
   `core/conversion.ConversionPlan` carries these as plain strings (the LLM
   produces finished HTML directly; there is no role mapping left for this
   module to interpret). The CSS the LLM was given already starts from the
   source's own `css` verbatim (a system-prompt hard rule) — it frequently
   references `@font-face` files that live in the media folder, and reusing
   it makes generated cards look native immediately.
3. Scope the note set by **both** notetype and deck:
   `col.find_notes(f'"note:{nt}" "deck:{deck}"')`. Assert the resulting count
   matches what the preflight dialog showed before writing anything.
4. Duplicate the notes onto the clone (`ops/notetype_manager.py`'s
   `_new_deck`/`_copy_fields_by_name`), stripping pre-existing audio out of
   every copied field value along the way — see "Audio" below.
5. **Reset scheduling unconditionally** via
   `col.sched.schedule_cards_as_new(...)` with `reset_counts=True`.
6. Wrap everything in one `CollectionOp`. **A single custom undo entry cannot
   span the whole conversion**: creating the clone notetype is a notetype
   *schema* change, and Anki invalidates any `add_custom_undo_entry` marker
   set before a schema change — confirmed against a real collection as
   `"target undo op not found"` when this was gotten wrong.
   `add_custom_undo_entry`/`merge_undo_entries` may only ever span what comes
   *after* the last schema-changing call (the note duplication, the
   scheduling reset) — never wrap it around the clone-creation call. A
   conversion is therefore two separate undo steps, not one; see
   `notetype_manager.py`'s `apply_plan` for exactly where the boundary
   falls. Still prompt for a manual backup/export before starting,
   regardless.
   **In practice, even with the marker placed correctly, `"target undo op
   not found"` was still observed occasionally** (exact second trigger not
   pinned down — see `docs/api-notes.md`). Because the marker/merge lives
   inside `apply_plan`, letting that exception propagate made `CollectionOp`
   treat the entire, already-successful conversion as failed. `apply_plan`
   now retries with a fresh marker on failure, which is structurally
   guaranteed to succeed (nothing schema-changing can happen between the
   retry marker and merging it immediately after) — see `docs/api-notes.md`
   and `tests/test_notetype_manager.py::TestUndoMergeRecovery`.

### Audio: the demoted language's is stripped from the data itself, the new language's goes into a new field per source field

On a conversion, the demoted language's audio is no longer wanted — a learner
going EN→JP does not need Japanese pronunciation audio on cards that now test
English.

**The real guarantee lives in the copy, not the template.** Every field's
value is stripped of `[sound:...]` references while it's copied onto the
clone (`ops/notetype_manager.py`'s `_strip_pre_existing_audio`, applied
unconditionally to every field), so the demoted language's audio simply
isn't present in the converted notes' data at all, regardless of how the
AI's template ends up referencing any given field.

**Why a template-level fix (forcing `{{text:Field}}`) doesn't work, confirmed
against real Anki source (`ankitects/anki` on GitHub):** `{{text:Field}}`
compiles to `strip_html(text)`, whose regex only matches HTML tags (`<...>`)
— it has never touched `[sound:...]`, which is Anki's own bracket notation,
not HTML. Separately, `extract_av_tags` (which decides what autoplays) scans
the *fully rendered* card text for `[sound:...]`/`[anki:tts...]` patterns
unconditionally, with no awareness of what filter referenced the field or
what HTML/CSS wraps it. No Anki template filter strips `[sound:...]` at all
(checked the complete filter list: `text`, `furigana`/`kanji`/`kana`,
`cloze`/`cloze-only`, `type*`, `hint`, `tts`) — so the only thing that can
ever work is removing the marker from the *data* before a template can
reference it, which is exactly what copy-time stripping does. This also
means it doesn't matter if per-field audio detection (`llm/audio_safety.py`)
misses a field, and a field that mixes real text with its own audio (e.g.
`"Hund [sound:hund.mp3]"`) keeps its text — only the marker goes.
`llm/audio_safety.py`'s older `{{text:Field}}`-forcing mechanism is still in
the code and still harmless, but the actual guarantee no longer depends on
it.

- Drop the demoted language's audio from the generated template **entirely**
  — not renamed, not kept as a secondary field. Falls out of
  `llm/audio_safety.py` plus the model simply never being told the demoted
  field is anything to speak.
- **The newly-fronted language's audio always goes into field(s) the
  conversion creates, never into a field the deck already had**, decided per
  source field: `llm/direction.py`'s `resolve_direction` gives one
  `AudioTarget(source_field, audio_field)` pair for **every** real content
  field placed on the new front — commonly two (a word/term and a full
  example sentence), not one. `core/audio_fields.py`'s
  `generated_audio_field_name(source_field_name)` names each one
  deterministically (`"ddc-audio-" + source_field_name`), so re-analyzing an
  already-converted notetype proposes the exact same audio fields it already
  has. Anki field names are already unique per notetype, so prefixing by
  source field name makes each generated name unique automatically with no
  collision-suffix logic needed. The old audio field (the one the deck
  already had) keeps its contents; the model is simply never told it exists
  as a place to write new audio, and `audio_safety.py` ensures it can never
  be bare-referenced for playback either — hidden, not deleted, and not
  overwritten.
- Why a new field rather than reuse: a reused field arrives at TTS time
  **already holding the old language's audio**, so there is a window between
  conversion and successful synthesis in which the card plays exactly the
  language the conversion was meant to retire. A field the addon just
  created is **empty**, so the worst case becomes silence until the audio
  exists — the failure mode becomes structurally impossible rather than a
  matter of ordering.
- Consequence worth knowing: there is no batch-end "turn audio on" step. The
  AI's Front HTML already has `{{#field}}{{field}}{{/field}}` appended per
  audio target at analysis time (`llm/analyze.py`'s `_append_audio_html`),
  each field starting empty, so it renders as nothing until TTS actually
  fills it. Running a TTS batch partially or cancelling it midway is
  therefore safe unconditionally.
- A deck with *no* audio field at all needs no special case: a source
  field's own audio field is created fresh regardless of whether the deck
  already had one for something else.

Covered by `tests/test_llm_direction.py`, `tests/test_notetype_manager.py`
(`TestPreExistingAudioIsStrippedFromCopies`,
`TestGeneratedAudioFieldsAreCreatedOnTheClone`), and `tests/test_audio_fields.py`.

## PIPER TTS IMPLEMENTATION (module group: /tts)

Piper is a local, offline neural TTS engine distributed as: (a) a small
standalone CLI binary per OS/arch (GitHub releases:
https://github.com/rhasspy/piper/releases — Windows/Linux/macOS,
amd64/arm64), and (b) separate voice model files (.onnx + .onnx.json),
hosted at https://huggingface.co/rhasspy/piper-voices, organized by
locale/quality (e.g. en_US-ljspeech-high).

Design decision: invoke Piper as a **subprocess**, not via a bundled Python
binding. Bundling `piper-tts`'s Python package would drag in onnxruntime's
platform-specific compiled wheels for every OS/arch we support, massively
bloating the addon and complicating packaging. A subprocess call against a
lazily-downloaded standalone binary is far more addon-friendly — the same
reasoning behind invoking llama.cpp as a subprocess rather than a bundled
Python binding for LLM inference.

piper_binary_manager.py responsibilities:

- Detect OS + architecture.
- Check addon's user_files cache dir for an existing extracted binary.
- If missing, download the correct release zip/tar, extract, set the
  executable bit on Unix, verify it runs (`piper --version` or similar)
  before trusting it.
- Never silently fail — surface clear errors to the UI if download/extraction
  fails (offline user, blocked firewall, unsupported arch, etc.).

piper_voice_manager.py responsibilities:

- Ship a small curated list of known-good voice IDs
  (`addon/tts/piper_voice_manager.py`'s `CURATED_VOICES`) rather than
  exposing Piper's entire enormous voice catalog by default. **Known
  limitation, not a bug:** the curated list ships English voices only
  (`en_US-ljspeech-high`, `en_GB-alba-medium`) — so a deck whose
  *newly-fronted* language is not English has no voice to generate with yet,
  even though everything else about the pipeline (analysis, direction,
  conversion) already works for any language pair. Widening the curated list
  is a data change, not a code one.
  **Check each voice's own license before adding it — Piper voices are not
  uniformly licensed just because they ship from the same repo.**
  `en_US-lessac-medium` was the original default and was removed once its
  training corpus turned out to be licensed "Research Purposes only,"
  explicitly excluding "the development, marketing, commercialisation, sale
  or licencing of voice synthesis ... products" — wording broad enough to
  cover a free open-source addon. Check a candidate voice's own `MODEL_CARD`
  on `huggingface.co/rhasspy/piper-voices` before curating it; see
  `docs/api-notes.md` for the full verification and `README.md`'s Licensing
  section for the current audit.
- Download the .onnx + .onnx.json pair for a chosen voice into the cache dir
  on first use, with a visible progress indicator (files can be tens of MB).
- Cache checks so repeat use doesn't re-download.

piper_provider.py responsibilities:

- Implements provider_base.py's interface: given text, return a path to a
  generated audio file. Piper itself only ever writes uncompressed WAV, which
  Anki plays natively — but WAV is large (~44KB/sec), so `synthesize()` makes
  one best-effort attempt to shrink it to a mono MP3 afterward; see
  "Compression" below.
- Runs via subprocess call: pass text via stdin, `--model <path to .onnx>`,
  `--output_file <path>`. Must run inside a CollectionOp/QueryOp background
  task, not on the Qt main thread — Piper synthesis is CPU-bound and can take
  real time across hundreds of sentences. `ensure_ready(voice_id)` downloads
  the binary and voice up front, synchronously, before a caller fans
  synthesis calls out across a thread pool (see `ops/tts_runner.py`'s
  opt-in concurrent mode) — `ensure_piper_binary`/`ensure_voice` are not
  themselves written to be safe against two threads racing a first download.
- After generation, call `col.media.add_file()` to import the WAV into the
  collection's media folder and get back the deduplicated filename, then
  write `[sound:filename]` into the target field named by
  `llm.direction.AudioTarget.audio_field` — one call per `(audio_field,
  source_field)` pair returned by `resolve_direction`, so a note with both a
  word and a sentence on its new front gets two separate audio fields
  filled, not one. It happens atomically per note:
  `ops/tts_batch.py`'s `generate_note_audio` aborts and leaves the *whole
  note* untouched on a real synthesis failure partway through its targets,
  so a re-run retries cleanly rather than leaving a note half-filled.
- **Sanitize text before synthesis — this is mandatory, not a nicety.** Real
  field data can contain `&nbsp;`, `<div>`, `<br>`, `<!--anki-->` comments,
  and embedded text in another script. Feeding that raw to Piper produces
  garbage. Strip HTML tags and comments, decode entities, strip `[sound:…]`
  tags and `kanji[kana]` ruby brackets, and drop characters outside the
  target language's script.
  `allowed_ranges` defaults to `None`, meaning *derive it from the voice* —
  `tts/script_ranges.py` maps the voice id's locale to its script. Keyed off
  the **voice**, not the deck's target language, because the voice is what
  is actually going to pronounce it; a voice/deck mismatch then reports
  "nothing to say" honestly instead of producing noise. An unlisted locale
  filters *nothing* rather than guessing — letting a few odd characters
  through is a far smaller failure than deleting an entire script. (An
  earlier version defaulted to Latin-only regardless of voice, which
  silently produced zero audio for any non-Latin target — fixed for that
  reason.)
  Punctuation is kept regardless of script, so filtering to the wrong script
  can leave a bare `"."` — truthy, so it passes an empty-string check and
  Piper would synthesize a meaningless file that then counts as that note's
  audio. `sanitize_text` raises `EmptyTextError` when nothing but
  punctuation survives the script filter (digits count as speakable;
  punctuation alone does not) — `ops/tts_batch.py` catches this per-target
  and counts the field as skipped rather than a hard failure, so one
  unspeakable field on a note doesn't block the note's other targets. See
  `tests/test_script_ranges.py`.
- Must support: sample generation for a single piece of text (used by the
  "Sample" button, which speaks the *currently selected note's* own
  target-language text rather than a canned phrase), and full-batch
  generation with progress reporting and skip-if-already-has-audio caching
  (`ops/tts_batch.py`'s `AUDIO_DONE_TAG` / `notes_needing_audio`).
- **Compression (`ffmpeg_binary_manager.py`, added 2026-09-20).** Piper's WAV
  output is uncompressed PCM — the actual root cause of an oversized
  converted deck, confirmed by inspecting a real `.apkg`'s media files. After
  a successful synthesis, `PiperProvider._compress_to_mp3` makes one
  best-effort attempt to shrink the WAV to a mono 48kbps MP3 via a
  lazily-downloaded, cached ffmpeg binary (roughly a 7x size reduction, no
  audible quality loss for spoken word). **This must never be able to break
  synthesis**: any failure at all — unsupported platform, offline, a dead
  pinned download URL, a bad conversion — is caught and silently falls back
  to returning the original WAV untouched, exactly like before this feature
  existed. `col.media.add_file`/the `[sound:...]` tag are both
  format-agnostic, so nothing downstream (`ops/tts_batch.py`,
  `ops/tts_runner.py`, the Sample button) needed to change.
  Two different, independently-verified binary sources are used per platform
  (see the module docstring and `docs/api-notes.md` for exactly how each was
  verified): Windows/Linux use BtbN/FFmpeg-Builds' **LGPL** flavor (confirmed
  by reading its build scripts directly — libmp3lame/libvorbis aren't gated
  behind a GPL check, only libx264/libx265 are excluded from that flavor);
  macOS has no equivalent LGPL static build available anywhere actively
  maintained, so it uses evermeet.cx's **GPL** build instead — safe because
  ffmpeg is only ever invoked as a subprocess, never linked into this
  addon's own code, the same "shell out to a GPL CLI tool" pattern used by
  countless MIT/Apache-licensed applications. Every URL is a pinned,
  verified-working snapshot (the same "needs periodic re-verification"
  caveat already accepted for llama.cpp's nightly builds — BtbN has no
  immutable per-version tag to pin to instead). A first-time ffmpeg download
  during a concurrent TTS batch (`ops/tts_runner.py`'s opt-in concurrent
  mode) is guarded by `PiperProvider`'s own `threading.Lock` — unlike
  `ensure_piper_binary`/`ensure_voice`, this one really could race two
  worker threads on a cold cache, since nothing pre-fetches it from
  `ensure_ready()` the way the Piper binary/voice are pre-fetched.

`ui/piper_test_dialog.py` — a standalone dialog for sampling a voice in
isolation, sharing no code with the main screen — is **not wired to the
Tools menu** (removed 2026-09-20, ahead of the AnkiWeb release: it was a
dev/debug convenience, not something end users need). The file itself is
untouched and still importable for local debugging
(`from addon.ui.piper_test_dialog import show_piper_test_dialog`);
`entrypoint.py` now registers exactly one Tools-menu action.

## STATUS

The core pipeline (Analyze → Convert → Generate TTS audio) is built,
unit-tested, and confirmed working end-to-end against a real Anki profile.
Licensed MIT; every dependency is permissive (MIT/Apache-2.0/public domain)
— see `README.md`'s Licensing section before adding any new dependency or
Piper voice. Known open issues are tracked in `TODO.md` — check there rather
than here, so this file doesn't drift out of sync with what's actually still
broken.

Work through changes deliberately. Do not jump ahead to UI polish or
multi-provider TTS/LLM abstraction gold-plating before the core pipeline is
solid and actually running inside a real (test) Anki profile.

## DEV ENVIRONMENT

- Two Anki profiles exist: **`User 1`** (the real collection, ~13.8k notes —
  in-development code must never write to it) and **`test profile`**
  (iterate here), seeded with three decks: Core 2000, a Korean deck, and a
  German deck (the German deck specifically for the field-name-collision and
  mixed text+audio field traps described above). Check `test profile`
  directly for what's currently seeded rather than assuming Core 2000 is the
  only thing there.
- To review results in the real collection, export the generated deck to
  `.apkg` and import it manually — never write to `User 1` directly.
- `core/` and `tts/` are pure Python and must be testable with stock Python, no Anki.
  `llm/` is *mostly* pure Python too — the one exception is the real subprocess
  call in `llm/client.py`/`llm/runtime.py`'s default `run`/`download_to`, which is
  always injected out in tests (same discipline as `tts/piper_binary_manager.py`).
  `ops/` and `ui/` are Anki-facing and import `aqt`/`anki`.
  Run the whole pure suite with:
  `python -m unittest discover -s tests -t .` (the `-t .` matters — it sets the repo root
  as the top-level dir so `addon.*`/`tests.*` package-relative imports resolve).
- `docs/api-notes.md` records verified Anki API facts; `docs/llm-notes.md` records
  verified llama.cpp/Qwen facts (invocation contract, asset names, stdout shape). Same
  "never guess" discipline applies to both — check there before assuming a flag or
  signature behaves the way its name suggests.
- Packaging uses `tools/build_ankiaddon.py`, a small local script, not the
  community `anki-addon-builder` (`aab`) — `aab` expects the addon to live
  under `src/<module_name>/` with a repo-root `addon.json` and is built
  around git-tag-based AnkiWeb publishing, which would mean restructuring
  this repo for a public-release workflow that's intentionally on hold.
