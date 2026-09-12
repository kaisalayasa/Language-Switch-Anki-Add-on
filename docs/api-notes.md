# Anki API notes

`claude.md` forbids guessing Anki API signatures. This file records what was **verified**,
how, and what still needs confirming.

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
`notetype_manager.py`), in both conversion modes, via `run_conversion` in
`convert_op.py` — it had simply not been exercised by a real (non-dry) run yet.
`tests/test_convert_op.py` (previously nonexistent — only the inner `apply_plan` was
tested, which bypasses the undo wrapping entirely) now covers `run_conversion` directly
in both modes.

**The fix, applied everywhere `add_custom_undo_entry`/`merge_undo_entries` is used in this
addon:** the marker may only span calls that are *not* notetype-schema-level.
- `col.models.add_dict` / `update_dict` / `change_notetype_of_notes` — schema-level, never
  wrap.
- `col.decks.add_normal_deck_with_name`, `col.new_note`/`add_note`/`update_note`,
  `col.sched.schedule_cards_as_new` — not schema-level (deck creation has never required a
  full resync, unlike notetype changes), safe to wrap.

Concretely: `apply_plan` now creates the clone (schema change) *before* setting the marker,
and for Flip-in-place also runs `change_notetype_of_notes` (also schema change) *before*
setting the marker; only the scheduling reset (and, in New-deck mode, the note
duplication) sit inside the wrapped span. `ConvertDialog._preview` was restructured the
same way: `ensure_preview_scaffold` (schema change) runs unwrapped, then the marker is set,
then `write_preview_note` (pure data) is wrapped.

**Consequence for the UI:** a conversion is no longer a single undo step. It's two (New
deck) or three (Flip in place) separate ones — see `apply_plan`'s comments for exactly
where the boundaries fall. `claude.md` §8 has been corrected to state this rather than
promise single-keystroke undo.

`tests/fake_collection.py` now simulates this: `add_dict`/`update_dict`/
`change_notetype_of_notes` bump a `_schema_generation` counter, and
`merge_undo_entries` raises `RuntimeError("target undo op not found")` if the generation
has moved since the matching `add_custom_undo_entry` call — reproducing the real bug
faithfully enough that both the original failure and the fix are now provable in the pure
test suite, without needing a real Anki collection to notice a regression.

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

## `aqt.clayout.CardLayout` — pulled forward from M3

`claude.md` schedules the live-preview pane for M3 ("investigate reusing/subclassing
`aqt.clayout.CardLayout`"). At the user's explicit request, M1 now has one "Preview card…"
button that opens it early, against one real note — see `addon/ui/preview.py`.

Its constructor is the **one call in this addon not behaviourally verified** the way the
rest of M1 is: byte-scanning confirmed the class exists, but its `__init__` could not be
introspected ahead of time (the compiled module targets Python 3.13, and this repo's tooling
runs 3.11 — `inspect.signature` needs a live, matching interpreter, which only exists
inside a running Anki process).

Rather than guess one call shape and let a mismatch crash the dialog, `open_card_layout()`
tries progressively simpler keyword sets (`{ord, fill_empty}` → `{ord}` → `{}`) and, if none
of those match, raises with the *installed* signature (introspected live, since by then
we're inside real Anki) plus every attempt that failed — actionable, not a guess. If you
hit that error, paste it here.

To confirm ahead of time instead of waiting for a failure, run in the debug console:

```python
import inspect
from aqt.clayout import CardLayout
print(inspect.signature(CardLayout.__init__))
```

### CardLayout signature

_(not yet run)_

## Design notes

- **Cloning**: `deepcopy(source)` → `["id"] = 0` → name it → `add_dict`, rather than
  `col.models.copy()`. Naming is our own `_unique_notetype_name()` (see the trap above),
  not `ensure_name_unique`. Gives us control of the name and one predictable add step
  across versions.
- **Field maps**: because the clone preserves field names, order and count, the
  change-notetype map Anki prefills is an **identity** map. That is the correct explicit
  map here — role mapping drives template HTML only, never field migration. Non-identity
  maps only become relevant if we add or remove fields on the clone.
- **`trash_files` vs delete**: trashing is recoverable, which is why the (M5) media cleanup
  uses it.
- **Preview never deletes**: `build_preview_note()` only adds or updates a dedicated,
  deterministically-named scratch notetype/deck/note (suffixed `" (Preview)"`). Repeated
  previews reuse the same note rather than accumulating, and nothing is ever deleted — so a
  preview the user starts tinkering with inside Card Types is never silently destroyed.
