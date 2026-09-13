# Deck Direction Converter

An Anki addon that flips a bilingual deck's direction — *Language A front / Language B back*
becomes *Language B front / Language A back* — and (from M2) generates natural TTS audio for
the newly-fronted language using [Piper](https://github.com/rhasspy/piper), locally and
offline.

**Status: M1-M3 done, M5 in progress** (M4, language-detection-seeded mapping suggestions,
deferred in favor of M5 — see `docs/api-notes.md`). Template generation, the clone-notetype
flow, both conversion modes, a real "Map fields…" mapper UI, and live card preview all work
end-to-end in real Anki. M5 connects M2's standalone Piper TTS pipeline to real notes for the
first time: **Tools → Generate TTS audio…** batch-synthesizes and writes audio into a
converted deck's notes, with progress, cancellation, and resumability (a re-run only
processes notes that don't already have generated audio, tracked via the `ddc-tts-generated`
tag), then flips the notetype's template on to actually reference the new audio.

## The two modes

| | What happens | Original deck |
|---|---|---|
| **New deck** (default) | Clones the notetype, rewrites the template, **duplicates** the notes into a brand-new deck | untouched |
| **Flip in place** | Clones the notetype, rewrites the template, **repoints** the existing notes onto the clone | its cards are replaced |

Scheduling is **always** reset, in both modes. The old interval and ease describe a skill —
recognition in the original direction — that was never practised in the new direction, so
carrying it over would be actively misleading.

The original **notetype object** is never modified in either mode.

## Design rules

- **`addon/core/` and `addon/tts/` are pure.** No `anki`, no `aqt`, no language name, no
  script name, no deck-specific field name in `core/`. Enforced by `tests/test_purity.py`,
  not by convention.
- **Field names are untrusted.** Most decks are `Front`/`Back` or `Field 1`. Even
  descriptive names lie: the real Core 2000 notetype has a field called `Notes` holding
  `"Core 2000 Step 01 - 001"` and one called `Core-Index` holding an integer. Roles are the
  only abstraction; no code matches on a field name.
- **Never guess an Anki API.** See `docs/api-notes.md`. (`col.sched.forget_cards` does not
  exist — it's `schedule_cards_as_new`.)
- **Refuse rather than misalign.** If a profile's stored field name and ord disagree with
  the live notetype, the run stops instead of writing the wrong content into every note.

## Layout

```
addon/
  core/        pure logic — role_schema, template_generator, conversion, profiles
  tts/         pure logic — Piper binary/voice managers, subprocess provider, sanitizer
  ops/         everything that imports anki/aqt
  ui/          Qt dialogs
  profiles/    field mappings, one JSON per known deck
tests/         stock-Python unit tests, no Anki required
tools/         preview_templates.py, install_dev.py
docs/          deck-facts.md (verified ground truth), api-notes.md
```

## Development

```bash
python -m unittest discover -s tests -t .   # 139 tests, no Anki needed, no network needed
python tools/preview_templates.py core2000  # see the generated templates
python tools/install_dev.py --link          # install into Anki (close Anki first)
```

Then in Anki's Tools menu: **Convert deck language direction…** (map fields, convert),
**Preview converted card…** (live preview, no writes), **Test Piper voice… (M2)** (standalone
synthesis check), or **Generate TTS audio… (M5)** (batch-writes audio into a converted
deck's notes). The first synthesis of any kind downloads the Piper binary (~20MB) and voice
model (~60MB) into `addon/user_files/` (gitignored, never wiped by an addon update); later
runs are cached.

Develop against a scratch profile, not your real collection. Every operation is scoped to a
single (deck, notetype) pair, but in-development code writes to the same `collection.anki2`
that holds everything else.

## Milestones

M1 proof of concept · M2 Piper integration · M3 role-mapping UI · M4 language detection ·
M5 batch apply · M6 profiles · M7 release prep
