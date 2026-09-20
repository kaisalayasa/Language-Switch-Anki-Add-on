# Handoff — Deck Direction Converter (Anki addon)

Written 2026-09-20, for a fresh Claude Code session picking this project up cold.
**Read `CLAUDE.md` first** — it is the full, authoritative project spec (mission,
hard constraints, Anki API facts, module-by-module design rationale) and is kept
up to date after every feature change. This file is a shorter "where things
actually stand right now" companion — it does not replace CLAUDE.md, it points
into it.

## What this project is

An open-source Anki addon that takes an existing bilingual deck ("Language A
front, Language B back") and converts it into the opposite direction ("Language
B front, Language A back"), generating fresh TTS audio for the newly-fronted
language with a local, offline Piper voice. Field placement and the actual
Front/Back/CSS template HTML are produced by a **local LLM** (Qwen2.5-7B-Instruct
via llama.cpp, CPU-only, both downloaded and cached once) — not by a hand-written
heuristic system. No cloud AI providers anywhere in the pipeline; no telemetry;
no network calls except downloading the Piper binary/voices and the llama.cpp
binary/model from their public release hosts.

Repo root: `C:\Users\acer\Desktop\Anki Addon`. Current branch: **`main`**
(the `overhaul` branch was fast-forward-merged into `main` and both are pushed
to `origin` — `main` is the live, current state on GitHub, not a WIP branch).

## Current status: pipeline complete and working end to end

The single-screen UI flow (`ui/main_screen.py`) is: pick a deck/notetype pair →
**Analyze** (one LLM call, with live preview and a review of the AI's placement/
trust rating) → optionally hand-edit via a raw HTML editor or the non-technical
**Hide fields** panel → **Convert** (clone notetype, duplicate notes onto a new
deck, reset scheduling) → **Generate TTS audio** (Piper, batched, resumable).
This has been confirmed working against a real Anki profile (`test profile`),
not just the unit-test suite.

Test suite: **305 tests, all passing** —
`python -m unittest discover -s tests -t .` from the repo root (the `-t .`
matters, it's what makes `addon.*`/`tests.*` imports resolve).
`core/`, `tts/`, and `llm/` (aside from the one real subprocess call, always
injected out in tests) are pure Python, testable with stock Python — no Anki
required to run the suite.

## Module map (current, post-overhaul)

- **`addon/llm/`** — the whole local-LLM deck-analysis pipeline. This is the
  current architecture; there is no older role-mapping system left in the repo
  at all (it was deleted, not deprecated — see CLAUDE.md's BUILD ORDER section
  for that history if it ever matters).
  - `runtime.py` / `model_manager.py` — download+cache the llama.cpp binary and
    the Qwen2.5-7B GGUF, with real verification (a `--version` subprocess run
    for the binary, a size check for the model).
  - `client.py` — one `llama-completion` subprocess call, prompts via temp
    files, `--temp 0`, byte-for-byte-verified stdout parsing.
  - `template_fields.py` — pure regex parsing of `{{Field}}` references
    (`referenced_fields`, `current_sides`, `content_reference_pattern`).
  - `direction.py` — **the deterministic core**. Computes per-field new-side
    placement, target/native language, and one `AudioTarget(source_field,
    audio_field)` pair per field placed on the new front. Direction, language,
    and placement are **never asked of the model** — it reliably failed that
    exact task in real testing (see CLAUDE.md's "LOCAL LLM DECK ANALYSIS"
    section for the specifics). Also excludes any field never referenced
    anywhere in the original template from placement (deck bookkeeping/index
    fields the original card never showed).
  - `audio_safety.py` — force-rewrites any reference to a field with detected
    pre-existing audio into `{{text:Field}}`. Still present and still runs, but
    is **no longer the real safety guarantee** — see "Audio safety" below.
  - `prompt.py` / `response.py` / `validate.py` — build the one system+user
    prompt, parse the model's `--- ANALYSIS/FRONT/BACK/CSS ---` reply, and
    validate it against real observed failure modes (hallucinated field refs,
    unbalanced conditionals, empty front, misplaced field).
  - `analyze.py` — orchestrates one full Analyze call: direction → prompt →
    model → parse → audio-safety rewrite → validate → retry (≤3 attempts) →
    append the generated-audio conditional block → attach a trust rating
    (5/4/3/1 stars based on retries needed) → return `DeckAnalysis`.
  - `field_visibility.py` — the "Hide fields" panel's logic. Hiding a field
    **removes its rendering reference entirely**, replacing it with an inert
    HTML comment marker (`<!--ddc-hidden:Field:filter-->`) that records the
    original reference for exact restoration on unhide. (This used to wrap the
    reference in a `<span class="ddc-hidden">` + CSS `display:none` instead —
    superseded, see "Audio safety" below for why that's safe now.)
- **`addon/core/`** — pure, deck-agnostic logic: `conversion.py`
  (`ConversionPlan`/`build_plan`/`validate` — **one mode only**, see below),
  `audio_fields.py` (generated-audio-field naming, `ddc-audio-<Field>`),
  `deck_state.py` (reads/writes a CSS marker + note tag so re-Analyzing an
  already-converted notetype is idempotent, never double-flips), and
  `language_detect.py` (script-range fast pass + vendored `langdetect` for
  same-script disambiguation, `DetectorFactory.seed` pinned for determinism).
- **`addon/ops/`** — Anki-facing (`aqt`/`anki` imports). `notetype_manager.py`
  (clone, duplicate notes, strip audio, reset scheduling), `convert_op.py`
  (the one hand-rolled `CollectionOp` wrapping a conversion), `tts_batch.py` /
  `tts_runner.py` (Piper batch generation with skip-if-done caching),
  `deck_data.py` (sample collection helpers for the UI/prompt).
- **`addon/tts/`** — Piper as a subprocess (never a bundled Python binding —
  avoids dragging in onnxruntime's per-platform compiled wheels). Binary/voice
  managers, `sanitize.py` (HTML/entity stripping + script-based character
  filtering before synthesis, keyed off the **voice's** locale not the deck's
  language), `script_ranges.py`.
- **`addon/ui/main_screen.py`** — the single-screen UI. Everything else UI-side
  from the old multi-dialog design (`convert_dialog.py`, `field_list.py`,
  `role_mapper.py`, `tts_batch_dialog.py`, `card_preview.py`, `preview.py`) is
  deleted, not lingering as dead code.
- **`addon/vendor/`** — vendored `langdetect` (pure Python; deliberately not
  `py3langid`, which pulls in numpy).

## Design decisions worth knowing before touching anything

- **The model never decides direction, language, or placement — only writes
  HTML for a placement it's told.** This was hard-won across four failed
  prompt-phrasing attempts in real testing; don't try to hand more judgment
  back to the model without re-reading CLAUDE.md's rationale first.
- **There is only one conversion mode: duplicate onto a new deck.** "Flip in
  place" (repoint existing notes onto the clone without duplicating) existed
  earlier and was **removed** (2026-09-17 session) once the audio-safety fix
  came to depend on duplication happening unconditionally — see next bullet.
- **Audio safety is enforced at the data layer, not the template layer.**
  Confirmed against real Anki source (`ankitects/anki` on GitHub): no Anki
  template filter (`text`, `furigana`, `cloze`, `type*`, `hint`, `tts` — the
  complete list) strips `[sound:...]`, and `extract_av_tags` scans the fully
  rendered card text for `[sound:...]` unconditionally regardless of what
  filter or markup wraps the reference. So the old `{{text:Field}}`-forcing
  approach (`audio_safety.py`, and an earlier CSS-hide mechanism that also
  leaned on it) never actually stopped playback. The real fix:
  `ops/notetype_manager.py`'s `_strip_pre_existing_audio` strips
  `[sound:...]` out of **every** field's value, unconditionally, while it's
  copied onto the clone — independent of any per-field detection accuracy,
  and preserving real text in a field that mixes content with its own audio
  (`"Hund [sound:hund.mp3]"` → `"Hund"`). This is why Flip-in-place had to go:
  it never created a fresh copy to write the stripped value into.
- **Scheduling is always reset** on converted cards (old interval/ease data
  describes a skill — recognition in the original direction — the learner
  never practiced in the new direction).
- **A generated-audio field is always new, never a reused old one.** Reusing
  an existing field would arrive at TTS time already holding the *old*
  language's audio, with a window between conversion and successful synthesis
  where the wrong language could still play. A freshly created field starts
  empty, so the worst case is silence, never wrong-language audio.
- **Never guess Anki API signatures.** Two confirmed traps already hit and
  documented in CLAUDE.md/`docs/api-notes.md`: `col.models.ensure_name_unique`
  takes a notetype dict and mutates it in place (not a string in, string out);
  `col.sched.forget_cards` does not exist on the collection in 26.08.1 (only
  as a GUI wrapper). If uncertain about a signature, say so and check
  `docs/api-notes.md` or the installed `anki` package source rather than
  inventing plausible-looking code.

## Known open item (not yet fixed)

`llm/validate.py` doesn't yet check full placement compliance against the
given `new_front_fields`/`new_back_fields` — only field existence, conditional
balance, and misplaced-field leaks. The field-name-collision German test deck
(fields literally named `Front`/`Back`) is still occasionally flaky on
direction at the model layer (confirmed real llama.cpp non-determinism at
temp 0, not a prompt defect). Intended fix: extend `validate.py` to catch a
placement violation and retry, the same pattern already used for audio safety.
Full detail in `TODO.md`.

## Dev environment

- Two Anki profiles: **`User 1`** (real ~13.8k-note collection — never write to
  it from in-development code) and **`test profile`** (iterate here). Seeded
  with three decks on purpose: Core 2000 (Japanese/English, the main proving
  ground), a Korean deck, and a German deck (specifically for its field-name
  collisions and a mixed text+audio field).
- Verified target: **Anki 26.08.1**, bundled Python 3.13, collection schema 18.
- Run the pure test suite from repo root:
  `python -m unittest discover -s tests -t .`
- `docs/api-notes.md` and `docs/llm-notes.md` record verified API/runtime facts
  — check there before assuming a flag or signature behaves as its name
  suggests, same discipline as CLAUDE.md itself demands.
- Packaging uses a local script, `tools/build_ankiaddon.py` — not
  `anki-addon-builder`, which would require restructuring the repo (a
  publish-workflow decision intentionally on hold).
- LICENSE / MIT-vs-GPL decision: intentionally deferred, nothing depends on it
  yet.

## Loose end, not project-critical

An empty `package.json` (`{}`) and `package-lock.json` (no dependencies) sit
**untracked** at the repo root — origin unknown, don't appear to belong to a
pure-Python Anki addon, never committed. Safe to delete or investigate; just
hasn't been asked for yet.
