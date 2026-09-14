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
template on to actually reference the new audio. Two knobs on top of that basic loop, shared
by both this dialog and the single-screen "Generate TTS audio" button below (one runner,
`addon/ops/tts_runner.py`, so the logic exists once): a **"Notes per run" radio (All / 500 at
a time)** stops a run after 500 notes and simply leaves the rest untagged, so a very large
deck can be done in deliberately-sized chunks over several sittings instead of one long run
-- the "500 at a time" option disables itself (falling back to "All") whenever fewer than
500 notes are actually pending, since it wouldn't do anything different; **"Generate
multiple notes at once"** opts into synthesizing several notes' audio concurrently (a small
`ThreadPoolExecutor` of real Piper subprocesses -- capped at 4 workers and scaled down on
small machines by `default_concurrency()`, off by default so it never surprises a low-end
machine). Only the synthesis call itself is ever parallelized; every actual collection
read/write stays on the single background thread the batch already ran on sequentially --
see the module docstring for why that boundary matters. A cold-start first run also
downloads faster now: `PiperProvider.ensure_ready()` fetches the Piper binary and the voice
model at the same time (a small two-worker `ThreadPoolExecutor`) instead of one after the
other, since they're both large, independent transfers into unrelated cache folders.
Progress on either dialog now shows a visible **Stop** button and says plainly that
stopping is safe -- Anki's own progress window has no such button (only Escape or closing
it cancels, which isn't obvious), so this addon draws its own; Stop sets a plain
`threading.Event` the batch polls alongside Anki's own cancellation, and whatever's already
been generated is kept, exactly as if the run had ended on its own. M6 makes a hand-built field mapping
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

A conversion's real writes (the clone, the notes, the scheduling reset) always happen
before Anki's own undo-grouping step, which is why a `merge_undo_entries` hiccup there
(Anki's `"target undo op not found"`, seen occasionally in real use despite the ordering
fix in `docs/api-notes.md`) is now recovered from with a same-tick retry instead of being
reported as a failed conversion — see `apply_plan` in `notetype_manager.py`. This mattered
beyond the scary error message: since that exception used to propagate out of the whole
`CollectionOp`, it also silently skipped the screen's own "done" message and its refresh of
the deck/notetype list (and Anki's own deck browser refresh), so it could look like nothing
had happened even though the conversion had already fully succeeded. Confirmed fixed in
real use — the retry is invisible to the user (no mention of it in the "done" message,
just the plain success text) and only stays noted in `docs/api-notes.md` in case its
still-unidentified underlying cause is ever worth chasing for real. Separately, the
screen now also calls `mw.deckBrowser.refresh()` itself right after a successful
conversion, and again when the screen is closed as a safety net — `CollectionOp`'s own
change-driven refresh wasn't reliably enough to make Anki's deck browser show a
newly-created deck without a manual refresh, even though the addon's own dropdown always
updated correctly.

The original **notetype object** is never modified in either mode.

## Design rules

- **`addon/core/` and `addon/tts/` are pure.** No `anki`, no `aqt`, no language name, no
  script name, no deck-specific field name in `core/`. Enforced by `tests/test_purity.py`,
  not by convention.
- **Field names are untrusted.** Most decks are `Front`/`Back` or `Field 1`. Even
  descriptive names lie: the real Core 2000 notetype has a field called `Notes` holding
  `"Core 2000 Step 01 - 001"` and one called `Core-Index` holding an integer. Roles are the
  only abstraction; no code matches on a name that came from a *deck*. The one exception is
  `core/audio_fields.py`, which recognises the field **it created itself** by the `ddc-`
  name it chose — its own artifact, like the generated CSS marker, and the only way to find
  that field again later, since it holds nothing until TTS runs.
- **Never guess an Anki API.** See `docs/api-notes.md`. (`col.sched.forget_cards` does not
  exist — it's `schedule_cards_as_new`.)
- **Refuse rather than misalign.** If a profile's stored field name and ord disagree with
  the live notetype, the run stops instead of writing the wrong content into every note.

## Layout

```
addon/
  core/        pure logic — role_schema, template_generator, conversion, profiles,
               language_detect, role_detect (auto-mapping), audio_fields (audio policy)
  tts/         pure logic — Piper binary/voice managers, subprocess provider, sanitizer,
               script_ranges (voice locale -> pronounceable characters)
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
python -m unittest discover -s tests -t .   # 270 tests, no Anki needed, no network needed
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

There's also **Tools → "Deck Direction Converter — new single screen (testing)…"**, a
second, separate entry rolled out *alongside* the submenu above, not replacing it yet: one
dialog (`addon/ui/main_screen.py`) combining the deck/mode picker, an editable **"New deck
name"** field (pre-filled with the usual `<deck> (<suffix>)` default, disabled and locked to
the source deck's own name in Flip-in-place mode, since that mode never creates a separate
deck), a language-detection banner with a manual override, a field/role list on the left
(or, toggled on, a raw Front/Back/Styling HTML editor) next to a live preview pane on the
right, voice sampling, and **two buttons -- Convert and Generate TTS audio -- kept
deliberately separate**, one step each. The field list hides a profile's `hidden` fields by
default (Core 2000 alone marks nine bookkeeping fields this way -- real clutter in a list
that long) behind a "Show hidden fields" checkbox above it. An earlier drag-to-reorder
feature on that list was tried and then deliberately dropped after review, so field order
now just follows a saved profile's order or plain notetype order, never an ad hoc drag.

Role assignment comes from three sources, in priority order, per `claude.md`'s Role Mapping
System: a saved profile (`addon/core/profiles.py`'s `match_profile`, exact field-name-list
match only, e.g. Core 2000's shipped profile); the user's own edit in the field list, always
authoritative; and, when neither applies, a content-based seed
(`addon/core/role_detect.py`'s `guess_role_mapping`) that reads the notetype's own *live*
template to know which fields are currently on the front (-> become Native after a flip) vs.
the back (-> become Target) -- a structural fact, not a guess -- then sorts each side's
fields into Term/Sentence/Reading from real (unsanitized) sample content. The direction
banner reads the live template the same structural way, independent of the mapping, to show
what's actually on the card *right now* alongside what it will become -- e.g. "Current: ko ->
en | after converting: en -> ko" for the Korean deck -- rather than only ever showing the
one, post-conversion direction.

### Where generated audio goes

**A conversion creates the field its generated audio will live in.** It is never written into
a field the deck already had. The deck's own audio keeps its contents and is bound to a
*native* audio role, which the generated template never emits -- so it is hidden, not deleted
and not overwritten. All of this is `addon/core/audio_fields.py`, and every mapping (profile,
hand-edited or auto-detected) is passed through its `resolve_audio_fields` before use, so no
mapping can express anything else.

This replaced an earlier "repurpose the deck's existing audio field" design that failed on a
real Korean deck converted for someone studying English: the card kept playing the original
Korean. A repurposed field arrives at TTS time already holding the old language's audio, so
between the conversion and a successful synthesis run the card plays exactly the language the
conversion was meant to retire -- and if synthesis fails or is interrupted for a note, that
window never closes. A field the addon just created is empty, so the worst case is silence
until the audio exists rather than the wrong language. Two useful consequences: the deck's
original recordings survive a conversion instead of being overwritten, and switching the
template's audio on is safe after a partial or cancelled run, because unprocessed notes
simply render nothing.

Two smaller fixes went with it, both of which could make a whole run finish in seconds,
report zero failures and produce no audio at all:

- `PiperProvider` used to filter text to a hardcoded **Latin** character range whatever the
  voice was, which deletes non-Latin text letter by letter. It now derives the range set
  from the voice's own locale (`addon/tts/script_ranges.py`), and an unlisted locale filters
  nothing rather than guessing.
- Filtering to the wrong script rarely leaves an *empty* string, because punctuation is kept
  regardless of script -- a Korean sentence under a Latin filter reduces to `"."`, which is
  truthy, so Piper was asked to speak a bare full stop and wrote a meaningless file that then
  counted as that note's audio. `sanitize_text` now treats "nothing but punctuation survived"
  as empty. Notes skipped this way are counted and reported rather than passed over silently.
"Sample" next to the voice picker speaks the *current note's* own target-language text --
sentence first, falling back to the term only for a word-only note -- rather than a canned
phrase, so it previews this deck's real content and how it actually sounds in context; the
default voice everywhere a voice picker appears is the UK voice, "alba"
(`en_GB-alba-medium`, `addon/config.json`'s `tts_voice`).

**"Generate TTS audio" stays disabled until the deck's direction is actually already
switched**, with a label above the buttons spelling out why: *"Step 1: click Convert to
switch this deck's direction. Step 2: once switched, come back here and click Generate TTS
audio."* This closes a real bug that briefly existed here: with two independent buttons
sharing one deck/notetype dropdown, nothing stopped "Generate TTS audio" from running
against a pair that was still in its original, pre-conversion direction -- and separately,
Flip-in-place's own success handler was looking up a deck id that mode never sets
(`result.target_deck_id` stays `0` for it), so the screen didn't always even land on the
right pair after converting. The gate (`MainScreen._direction_is_correct`,
`core.template_generator.referenced_fields`) checks the *live* front template on the
currently selected notetype for the mapped Target-language field, not just "was Convert
clicked" -- so a deck that was already in the right direction needs no gating, and, after a
real conversion, the mapping used carries forward onto the new pair automatically even
without a saved profile (cloning preserves field names 1:1), so the button unlocks
immediately rather than looking freshly-unmapped. Once unlocked, Generate TTS audio shows a
visible **Stop** button while running, with the reassurance text Anki's own progress window
doesn't give you -- audio already generated is kept, and clicking Generate again later just
continues where it left off.

It reuses the exact same `core`/`ops` logic as the submenu's dialogs — nothing about what a
conversion or a TTS batch *does* changes, only how you reach it. Once it's confirmed working
end to end, the old submenu and the six dialog files it opens are removed and this becomes
the only entry point.

**Confirmed bug, found in real use and fixed:** switching from a pair with a valid mapping to
one without (e.g. selecting Core 2000, then a not-yet-mapped deck) left the *previous* pair's
card sitting in the preview instead of updating -- `_refresh_preview`'s early-return paths
(no notes selected; the new mapping fails validation) never touched the preview widget at
all. `PreviewPanel` gained a `clear(message="")` method, called from both of those paths, so
switching decks now always shows either the new card or an explicit blank/placeholder state,
never a stale one.

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

Also on the backlog: orphaned old-language media cleanup after Flip-in-place + TTS
replacement. (The TTS sanitizer's hardcoded Latin range list, listed here for several
sessions, is **fixed** — see "Where generated audio goes" above.)

**Known limitation, not a bug:** the curated voice list
(`addon/tts/piper_voice_manager.py`) ships English voices only — `en_US-lessac-medium` and
`en_GB-alba-medium` — matching `claude.md`'s "ship a small curated list of known-good
English voice IDs" for v1. So a deck whose *newly-fronted* language is not English has no
voice to generate with yet, even though everything else about it now works. Converting
*into* English (the Korean deck's case) is fully supported. Piper publishes voices for many
languages; widening the curated list is a data change, not a code one.
