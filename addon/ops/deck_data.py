"""Reads real note data out of a live collection: which (deck, notetype) pairs exist, and a
few real sample values per field for each.

Relocated out of the old ``ui/convert_dialog.py``, where these lived as private helpers for
the now-deleted role-mapping dialog -- the new ``ui/main_screen.py`` needs the same raw data,
just to build an ``llm.analyze.analyze_deck()`` call instead of a role mapping. Upgraded along
the way to take ``col`` explicitly rather than importing ``mw`` at module scope, matching every
other file in ``ops/`` -- these are pure reads, and doing it this way makes them callable
against ``tests/fake_collection.py`` like the rest of this layer, not only against a running
Anki process.
"""

from __future__ import annotations

from typing import Any, Dict, List, Sequence, Tuple

from ..llm.prompt import FieldSample
from ..tts.sanitize import sanitize_text

__all__ = [
    "addon_config",
    "decks_with_notetypes",
    "collect_samples",
    "collect_raw_samples",
    "collect_field_samples",
]


def addon_config() -> dict:
    """This addon's user config, as a plain dict.

    Genuinely needs a running Anki process (``mw.addonManager``) -- unlike the functions
    below, there is no collection-level equivalent ``fake_collection.py`` could stand in for.
    """
    from aqt import mw  # lazy: keep this module importable without a running Anki process

    return mw.addonManager.getConfig(__name__.split(".")[0]) or {}


def decks_with_notetypes(col: Any) -> List[Tuple[str, str, int]]:
    """Every (deck, notetype, note count) pairing that actually has notes.

    Built from real data rather than from the deck list, because a deck can hold several
    notetypes and a notetype can span several decks -- and a conversion is scoped by the
    pair, not by either alone.
    """
    out: List[Tuple[str, str, int]] = []
    for deck in col.decks.all_names_and_ids():
        for notetype in col.models.all_names_and_ids():
            query = 'deck:"%s" note:"%s"' % (
                deck.name.replace('"', '\\"'), notetype.name.replace('"', '\\"')
            )
            count = len(col.find_notes(query))
            if count:
                out.append((deck.name, notetype.name, count))
    return sorted(out, key=lambda row: (-row[2], row[0]))


def collect_samples(
    col: Any, note_ids: Sequence[int], *, limit_per_field: int = 3, max_len: int = 60
) -> Dict[str, List[str]]:
    """A few real, HTML-stripped sample values per field, for display in a UI list.

    Field names lie (see CLAUDE.md's "Field names are untrusted input"), so showing what a
    field actually *contains* is the only way a human can make sense of it at a glance.
    Reuses the TTS sanitizer in strip-markup-only mode (``allowed_ranges=()``) rather than
    writing a second HTML stripper for display purposes.
    """
    samples: Dict[str, List[str]] = {}
    for nid in note_ids:
        note = col.get_note(nid)
        for name in note.keys():
            bucket = samples.setdefault(name, [])
            if len(bucket) >= limit_per_field:
                continue
            value = sanitize_text(note[name], allowed_ranges=())[:max_len]
            if value:
                bucket.append(value)
    return samples


def collect_raw_samples(
    col: Any, note_ids: Sequence[int], *, limit_per_field: int = 8, max_len: int = 400
) -> Dict[str, List[str]]:
    """Unsanitized per-field samples, keeping markup (``[sound:...]``, HTML) intact.

    Unlike :func:`collect_samples` (the HTML-stripped version used for display), this is for
    anything that needs to see a field's real content verbatim -- ``llm.prompt`` and
    ``llm.direction`` both need to see whether ``[sound:...]`` is present before it's stripped
    for display. Blank values are kept (not skipped) so the bucket's length reflects notes
    actually scanned.
    """
    samples: Dict[str, List[str]] = {}
    for nid in note_ids:
        note = col.get_note(nid)
        for name in note.keys():
            bucket = samples.setdefault(name, [])
            if len(bucket) >= limit_per_field:
                continue
            bucket.append(note[name][:max_len])
    return samples


def collect_field_samples(
    col: Any, note_ids: Sequence[int], *, limit_per_field: int = 8, max_len: int = 400
) -> Tuple[FieldSample, ...]:
    """:func:`collect_raw_samples`, reshaped into what :func:`~addon.llm.analyze.analyze_deck`
    wants directly: one :class:`~addon.llm.prompt.FieldSample` per field, in the notetype's
    own field order (the order ``note.keys()`` returns, which the first note sampled fixes).
    """
    by_field = collect_raw_samples(col, note_ids, limit_per_field=limit_per_field, max_len=max_len)
    return tuple(FieldSample(name=name, samples=tuple(values)) for name, values in by_field.items())
