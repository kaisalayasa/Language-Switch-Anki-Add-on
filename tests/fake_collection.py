"""A minimal stand-in for an Anki collection.

Only models the behaviour ``addon.ops.notetype_manager`` actually relies on. Keyword names
mirror the real Anki 26.08.1 signatures, because ``_call_checked`` introspects them.

This is a test double, not an emulator -- it exists so the destructive paths (repointing
notes, duplicating notes, resetting scheduling) can be exercised without a real profile.
"""

from __future__ import annotations

import itertools
from collections import namedtuple
from typing import Any, Dict, List, Optional, Sequence

#: Matches the shape of real Anki's NotetypeNameId/DeckNameId -- a plain .id/.name pair.
_NameId = namedtuple("_NameId", ["id", "name"])


class FakeNote:
    def __init__(self, nid: int, mid: int, field_names: Sequence[str], values: Sequence[str], tags=None):
        self.id = nid
        self.mid = mid
        self._names = list(field_names)
        self.fields = list(values) + [""] * (len(field_names) - len(values))
        self.tags: List[str] = list(tags or [])

    def _index(self, key: str) -> int:
        """Raises ``KeyError`` for an unknown field, like a mapping -- and like the
        production code that reads notes has always been written to expect.

        A bare ``list.index`` raises ``ValueError`` instead, which no caller catches, so a
        notetype that legitimately has a field the *note* doesn't (exactly what a clone with
        a freshly added audio field looks like mid-conversion) blew up here rather than
        being skipped. That was this double being unfaithful, not the addon being wrong.
        """
        try:
            return self._names.index(key)
        except ValueError:
            raise KeyError(key) from None

    def __getitem__(self, key: str) -> str:
        return self.fields[self._index(key)]

    def __setitem__(self, key: str, value: str) -> None:
        self.fields[self._index(key)] = value

    def keys(self) -> List[str]:
        return list(self._names)


class _OpChangesWithId:
    def __init__(self, id: int):
        self.id = id


class _ChangeNotetypeRequest:
    def __init__(self, old_notetype_id: int, new_notetype_id: int):
        self.old_notetype_id = old_notetype_id
        self.new_notetype_id = new_notetype_id
        self.note_ids: List[int] = []


class _ChangeNotetypeInfo:
    def __init__(self, request: _ChangeNotetypeRequest):
        self.input = request


class FakeModels:
    def __init__(self, col: "FakeCollection"):
        self.col = col

    def by_name(self, name: str) -> Optional[dict]:
        for nt in self.col.notetypes.values():
            if nt["name"] == name:
                return nt
        return None

    def all_names_and_ids(self) -> List[_NameId]:
        return [_NameId(nt["id"], nt["name"]) for nt in self.col.notetypes.values()]

    def ensure_name_unique(self, notetype: dict) -> None:
        """Matches real Anki: mutates ``notetype["name"]`` in place, returns nothing.

        Production code does not call this (see ``_unique_notetype_name`` in
        ``notetype_manager.py``) -- it exists here only so a test that calls it directly
        exercises the *real* contract, not the string-in/string-out shape this addon
        originally guessed and crashed on.
        """
        taken = {nt["name"] for nt in self.col.notetypes.values() if nt["id"] != notetype.get("id")}
        if notetype["name"] in taken:
            for n in itertools.count(2):
                candidate = "%s (%d)" % (notetype["name"], n)
                if candidate not in taken:
                    notetype["name"] = candidate
                    return

    def add_dict(self, notetype: dict) -> _OpChangesWithId:
        new_id = next(self.col._ids)
        stored = dict(notetype)
        stored["id"] = new_id
        self.col.notetypes[new_id] = stored
        self.col._bump_schema_generation()
        return _OpChangesWithId(new_id)

    def update_dict(self, notetype: dict) -> None:
        self.col.notetypes[notetype["id"]] = dict(notetype)
        self.col._bump_schema_generation()

    def field_names(self, notetype: dict) -> List[str]:
        return [f["name"] for f in notetype["flds"]]

    def new_field(self, name: str) -> dict:
        """Mirrors real Anki's ``ModelManager.new_field``: builds a detached field dict.

        Confirmed present on the target build (docs/api-notes.md). It is modelled here
        because ``notetype_manager.make_field_factory`` prefers it, so the fake has to offer
        it for the tests to exercise the path production actually takes. The ord is left for
        whoever inserts it into a notetype to assign, exactly as real Anki does.
        """
        return {"name": name, "ord": None, "sticky": False, "rtl": False,
                "font": "Arial", "size": 20, "description": ""}

    def change_notetype_info(self, old_notetype_id: int, new_notetype_id: int) -> _ChangeNotetypeInfo:
        return _ChangeNotetypeInfo(_ChangeNotetypeRequest(old_notetype_id, new_notetype_id))

    def change_notetype_of_notes(self, request: _ChangeNotetypeRequest) -> None:
        self.col.change_notetype_calls.append(request)
        target = self.col.notetypes[request.new_notetype_id]
        names = [f["name"] for f in target["flds"]]
        for nid in request.note_ids:
            note = self.col.notes[nid]
            note.mid = request.new_notetype_id
            note._names = list(names)
            # Real Anki resizes each note's values to the new notetype's field count, with
            # fields that have no counterpart in the old notetype arriving empty. Modelled
            # because the clone is now a *superset* of the source (it appends the generated
            # audio field), so without this a flipped note would have a name for that field
            # and no slot behind it.
            if len(note.fields) < len(names):
                note.fields += [""] * (len(names) - len(note.fields))
            del note.fields[len(names):]
        for card in self.col.cards:
            if card["nid"] in set(request.note_ids):
                card["mid"] = request.new_notetype_id
        # Real Anki: bulk-changing which notetype notes use is itself notetype/schema-
        # level, same as add_dict/update_dict -- it invalidates outstanding undo markers
        # too. See docs/api-notes.md and TestUndoEntryNeverSpansASchemaChange.
        self.col._bump_schema_generation()


class FakeDecks:
    def __init__(self, col: "FakeCollection"):
        self.col = col

    def by_name(self, name: str) -> Optional[dict]:
        for deck in self.col.decks_by_id.values():
            if deck["name"] == name:
                return deck
        return None

    def add_normal_deck_with_name(self, name: str) -> _OpChangesWithId:
        new_id = next(self.col._ids)
        self.col.decks_by_id[new_id] = {"id": new_id, "name": name}
        return _OpChangesWithId(new_id)

    def id_for_name(self, name: str) -> Optional[int]:
        deck = self.by_name(name)
        return deck["id"] if deck else None

    def all_names_and_ids(self) -> List[_NameId]:
        return [_NameId(d["id"], d["name"]) for d in self.col.decks_by_id.values()]


class FakeSched:
    def __init__(self, col: "FakeCollection"):
        self.col = col

    def schedule_cards_as_new(
        self,
        card_ids: Sequence[int],
        restore_position: bool = False,
        reset_counts: bool = False,
        context: Any = None,
    ) -> None:
        self.col.reset_calls.append(
            {"card_ids": list(card_ids), "restore_position": restore_position,
             "reset_counts": reset_counts}
        )
        for card in self.col.cards:
            if card["id"] in set(card_ids):
                card["type"] = 0      # new
                card["queue"] = 0
                card["ivl"] = 0
                card["reps"] = 0


class FakeMedia:
    def __init__(self, col: "FakeCollection"):
        self.col = col
        self.trashed: List[str] = []

    def add_file(self, path: str) -> str:
        return path.rsplit("/", 1)[-1]

    def trash_files(self, fnames: Sequence[str]) -> None:
        self.trashed.extend(fnames)


class FakeCollection:
    """Holds notetypes, decks, notes and cards, and records destructive calls."""

    def __init__(self) -> None:
        self._ids = itertools.count(1000)
        self.notetypes: Dict[int, dict] = {}
        self.decks_by_id: Dict[int, dict] = {}
        self.notes: Dict[int, FakeNote] = {}
        self.cards: List[dict] = []
        self.models = FakeModels(self)
        self.decks = FakeDecks(self)
        self.sched = FakeSched(self)
        self.media = FakeMedia(self)
        self.change_notetype_calls: List[Any] = []
        self.reset_calls: List[dict] = []
        self.undo_entries: List[str] = []
        # Simulates real Anki: a notetype schema change (add_dict/update_dict)
        # invalidates any outstanding add_custom_undo_entry marker. A "generation" that
        # bumps on every schema change reproduces this: a marker recorded at generation N
        # can only be merged while the generation is still N.
        self._schema_generation = 0
        self._pending_undo_tokens: Dict[int, int] = {}  # token -> generation when issued

    def _bump_schema_generation(self) -> None:
        self._schema_generation += 1

    # -- construction helpers ------------------------------------------------

    def add_notetype(self, name: str, field_names: Sequence[str], css: str = "",
                     templates: Optional[List[dict]] = None) -> dict:
        ntid = next(self._ids)
        nt = {
            "id": ntid,
            "name": name,
            "css": css,
            "flds": [{"name": n, "ord": i} for i, n in enumerate(field_names)],
            "tmpls": templates or [{"name": "Card 1", "qfmt": "{{%s}}" % field_names[0],
                                    "afmt": "{{FrontSide}}", "ord": 0}],
        }
        self.notetypes[ntid] = nt
        return nt

    def add_deck(self, name: str) -> dict:
        did = next(self._ids)
        deck = {"id": did, "name": name}
        self.decks_by_id[did] = deck
        return deck

    def seed_note(self, notetype: dict, deck: dict, values: Sequence[str], tags=None) -> FakeNote:
        nid = next(self._ids)
        names = [f["name"] for f in notetype["flds"]]
        note = FakeNote(nid, notetype["id"], names, values, tags)
        self.notes[nid] = note
        self.cards.append(
            {"id": next(self._ids), "nid": nid, "did": deck["id"], "mid": notetype["id"],
             "type": 2, "queue": 2, "ivl": 30, "reps": 12}
        )
        return note

    # -- collection API ------------------------------------------------------

    def new_note(self, notetype: dict) -> FakeNote:
        names = [f["name"] for f in notetype["flds"]]
        return FakeNote(0, notetype["id"], names, [""] * len(names))

    def add_note(self, note: FakeNote, deck_id: int) -> None:
        note.id = next(self._ids)
        self.notes[note.id] = note
        self.cards.append(
            {"id": next(self._ids), "nid": note.id, "did": deck_id, "mid": note.mid,
             "type": 0, "queue": 0, "ivl": 0, "reps": 0}
        )

    def get_note(self, nid: int) -> FakeNote:
        return self.notes[nid]

    def update_note(self, note: FakeNote) -> None:
        self.notes[note.id] = note

    def find_notes(self, query: str) -> List[int]:
        nt_name = _quoted(query, "note:")
        deck_name = _quoted(query, "deck:")
        out = []
        for nid, note in self.notes.items():
            nt = self.notetypes.get(note.mid)
            if nt_name is not None and (nt is None or nt["name"] != nt_name):
                continue
            if deck_name is not None:
                decks = {self.decks_by_id[c["did"]]["name"] for c in self.cards if c["nid"] == nid}
                if deck_name not in decks:
                    continue
            out.append(nid)
        return sorted(out)

    def find_cards(self, query: str) -> List[int]:
        if query.startswith("mid:"):
            mid = int(query.split(":", 1)[1])
            return sorted(c["id"] for c in self.cards if c["mid"] == mid)
        return sorted(c["id"] for c in self.cards)

    def add_custom_undo_entry(self, name: str) -> int:
        """Matches real Anki: returns a token identifying the current undo position.

        The token is only valid for as long as the schema hasn't changed underneath it --
        see ``_bump_schema_generation`` on ``add_dict``/``update_dict``.
        """
        self.undo_entries.append(name)
        token = len(self.undo_entries)
        self._pending_undo_tokens[token] = self._schema_generation
        return token

    def merge_undo_entries(self, target: int) -> None:
        """Matches real Anki: raises if the marked position no longer exists.

        A notetype schema change between ``add_custom_undo_entry`` and this call
        invalidates the marker, exactly reproducing the real
        ``"target undo op not found"`` error this addon hit in practice.
        """
        issued_at = self._pending_undo_tokens.pop(target, None)
        if issued_at is None or issued_at != self._schema_generation:
            raise RuntimeError("target undo op not found")


def _quoted(query: str, prefix: str) -> Optional[str]:
    """Pull ``prefix"value"`` out of a search string, undoing our escaping."""
    at = query.find(prefix + '"')
    if at == -1:
        return None
    start = at + len(prefix) + 1
    out = []
    i = start
    while i < len(query):
        ch = query[i]
        if ch == "\\" and i + 1 < len(query):
            out.append(query[i + 1])
            i += 2
            continue
        if ch == '"':
            break
        out.append(ch)
        i += 1
    return "".join(out)
