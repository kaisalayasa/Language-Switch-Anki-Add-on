# Deck Direction Converter

An Anki addon that flips a bilingual deck's direction — *Language A front / Language B back*
becomes *Language B front / Language A back* — and (from M2) generates natural TTS audio for
the newly-fronted language using [Piper](https://github.com/rhasspy/piper), locally and
offline.

**Status: M1-M6 done, M7 partly done** (packaging + menu polish; LICENSE/contribution guide
on hold, see Milestones below). Template generation, the clone-notetype flow, both conversion modes,
a real "Map fields…" mapper UI, and live card preview all work end-to-end in real Anki. M5
connects M2's standalone Piper TTS pipeline to real notes for the first time: **Tools →
Generate TTS audio…** batch-synthesizes and writes audio into a converted deck's notes, with
progress, cancellation, and resumability (a re-run only processes notes that don't already
have generated audio, tracked via the `ddc-tts-generated` tag), then flips the notetype's
template on to actually reference the new audio. M6 makes a hand-built field mapping
reusable: **"Save as profile…"** in the mapper dialog writes the current mapping out as JSON
into `addon/user_files/profiles/`, and every dialog that resolves a mapping (Convert,
Preview, Generate TTS audio) already picks up a saved profile automatically next time the
same notetype comes up — no separate "load" step. M4 adds a per-field language guess (Unicode
script, falling back to `langdetect` for same-script text) shown as a "Detected" column and
summary label in the mapper dialog — informational only; it never sets the Target/Native
language boxes or a Role dropdown on its own, per `claude.md`'s "detection seeds, never
finalizes" rule.

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
  core/        pure logic — role_schema, template_generator, conversion, profiles, language_detect
  tts/         pure logic — Piper binary/voice managers, subprocess provider, sanitizer
  ops/         everything that imports anki/aqt
  ui/          Qt dialogs
  profiles/    shipped field mappings, one JSON per known deck (e.g. core2000.json)
  user_files/  gitignored, per-machine: Piper cache + profiles/ (user-saved mappings, M6)
  vendor/      committed third-party source (langdetect + six, M4) — see vendor/README.md
tests/         stock-Python unit tests, no Anki required
tools/         preview_templates.py, install_dev.py, build_ankiaddon.py
docs/          deck-facts.md (verified ground truth), api-notes.md
```

## Development

```bash
python -m unittest discover -s tests -t .   # 168 tests, no Anki needed, no network needed
python tools/preview_templates.py core2000  # see the generated templates
python tools/install_dev.py --link          # install into Anki (close Anki first)
```

Then in Anki: **Tools → Deck Direction Converter** — one submenu holding **Convert deck
language direction…** (map fields, convert; its "Map fields…" dialog has a **"Save as
profile…"** button, M6), **Preview converted card…** (live preview, no writes), **Generate
TTS audio… (M5)** (batch-writes audio into a converted deck's notes), then a separator and
**Test Piper voice… (M2)** (a standalone synthesis check, not part of the main flow). The
first synthesis of any kind downloads the Piper binary (~20MB) and voice model (~60MB) into
`addon/user_files/` (gitignored, never wiped by an addon update); later runs are cached, and
saved profiles live in that same gitignored directory.

To install a real, packaged copy (rather than the dev symlink above):

```bash
python tools/build_ankiaddon.py   # writes dist/deck_direction_converter-<version>.ankiaddon
```

Then Anki → Tools → Add-ons → Install from file… This is a small local script, not the
community `anki-addon-builder` (`aab`) tool `claude.md` names — `aab` expects the addon to
live under `src/<module_name>/` with a repo-root `addon.json`, and is built around
git-tag-based versioning for publishing to AnkiWeb. Adopting it would mean restructuring this
repo for a public-release workflow this project hasn't committed to yet (license — MIT vs.
GPL — is still an open question, deliberately deferred). This script does the one thing
needed for now: produce a file Anki can actually install, for testing outside the dev
symlink. It excludes `addon/user_files/` (a fresh install shouldn't inherit the packager's
locally-downloaded Piper binary/voice or locally-saved profiles).

Develop against a scratch profile, not your real collection. Every operation is scoped to a
single (deck, notetype) pair, but in-development code writes to the same `collection.anki2`
that holds everything else.

## Milestones

M1 proof of concept · M2 Piper integration · M3 role-mapping UI · M4 language detection ·
M5 batch apply · M6 profiles · M7 release prep

**Done:** M1-M6, plus the packaging half of M7 (`tools/build_ankiaddon.py`, above) and the
Tools-menu consolidation into one **Deck Direction Converter** submenu. **Deliberately not
done as part of M7:** a LICENSE file, `manifest.json` license metadata, and a contribution
guide — all need the MIT-vs-GPL decision, which is intentionally on hold ("I don't know
anything about open source, leave it for later"). Nothing in the addon depends on that
decision; it only blocks the parts of M7 about being ready for outside contributors.

Also on the backlog, deferred as of M4/M5/M6's sessions: the TTS sanitizer only strips
non-Latin script today (`PiperProvider` defaults to `LATIN_RANGES` unconditionally — a real
bug for any non-Latin target language, not yet fixed even though `RoleMapping.target_language`
and now `language_detect.py` both carry the signal that would fix it); orphaned old-language
media cleanup after Flip-in-place + TTS replacement; auto-selecting Target/Native or a Role
dropdown from `language_detect.py`'s guesses (deliberately left manual in M4 pending a second
real test deck to validate against — `test profile` still only has Core 2000).
