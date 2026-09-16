"""Execute a :class:`~addon.core.conversion.ConversionPlan` against a collection.

Decisions are made in ``addon.core.conversion``; this module only carries them out.

Verified against **Anki 26.08.1** (see ``docs/api-notes.md``). Where a signature was not
verifiable ahead of time, the call is wrapped by :func:`_call_checked`, which raises a
message naming the exact thing to re-verify rather than silently doing something wrong --
``claude.md`` forbids guessing at Anki APIs, and a wrong guess here rewrites every note.
"""

from __future__ import annotations

import copy
import inspect
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional, Sequence

from ..core.audio_fields import AUDIO_DONE_TAG
from ..core.conversion import ConversionMode, ConversionPlan
from ..core.deck_state import conversion_tag

__all__ = ["ConversionResult", "ApiMismatch", "apply_plan", "probe_api", "shape_notetype"]


class ApiMismatch(RuntimeError):
    """An Anki API did not look the way this addon expects.

    Raised instead of proceeding, because every operation here is collection-wide.
    """


@dataclass
class ConversionResult:
    clone_notetype_id: int = 0
    clone_notetype_name: str = ""
    target_deck_id: int = 0
    notes_converted: int = 0
    cards_reset: int = 0
    dry_run: bool = False
    messages: List[str] = field(default_factory=list)
    #: Whatever ``col.merge_undo_entries`` returned. ``None`` for a dry run. A
    #: ``CollectionOp`` needs this to refresh the UI; see ``run_conversion`` in
    #: ``convert_op.py``.
    op_changes: Any = None


# ---------------------------------------------------------------------------
# defensive helpers
# ---------------------------------------------------------------------------


def _call_checked(fn: Callable[..., Any], what: str, **kwargs: Any) -> Any:
    """Call ``fn`` with ``kwargs``, dropping any the installed build doesn't accept.

    Anki's Python API shifts between releases. Rather than pinning ourselves to one
    spelling, we introspect and pass only what the installed build declares. If a keyword
    we consider essential is missing, the caller checks for it explicitly.
    """
    try:
        sig = inspect.signature(fn)
    except (TypeError, ValueError):  # builtins / C-implemented
        return fn(**kwargs)

    accepts_var_kw = any(
        p.kind is inspect.Parameter.VAR_KEYWORD for p in sig.parameters.values()
    )
    if accepts_var_kw:
        return fn(**kwargs)

    usable = {k: v for k, v in kwargs.items() if k in sig.parameters}
    dropped = sorted(set(kwargs) - set(usable))
    if dropped:
        # Not fatal by itself, but worth surfacing -- it means this build differs.
        pass
    try:
        return fn(**usable)
    except TypeError as exc:
        raise ApiMismatch(
            "%s: call failed with %s. Installed signature is %s. "
            "Re-run the probe in docs/api-notes.md and adjust." % (what, exc, sig)
        ) from exc


def _extract_id(value: Any, what: str) -> int:
    """Pull an integer id out of whatever an Anki 'add' call returned.

    Modern builds return an ``OpChangesWithId`` proto; older ones returned a bare int.
    """
    if isinstance(value, int):
        return int(value)
    for attr in ("id", "notetype_id", "deck_id"):
        got = getattr(value, attr, None)
        if isinstance(got, int):
            return int(got)
    raise ApiMismatch(
        "%s returned %r, which has no usable id. Verify the return type against your "
        "Anki build (see docs/api-notes.md)." % (what, type(value).__name__)
    )


def probe_api(col: Any) -> List[str]:
    """Report the signatures this module depends on. Used by ``docs/api-notes.md``."""
    out: List[str] = []
    targets = [
        (col.models, ["add_dict", "update_dict", "by_name", "ensure_name_unique",
                      "change_notetype_info", "change_notetype_of_notes", "field_names"]),
        (col.decks, ["add_normal_deck_with_name", "by_name", "id_for_name"]),
        (col.sched, ["schedule_cards_as_new"]),
        (col.media, ["add_file", "check", "trash_files"]),
        (col, ["new_note", "add_note", "find_notes", "find_cards",
               "add_custom_undo_entry", "merge_undo_entries", "update_note"]),
    ]
    for obj, names in targets:
        for name in names:
            fn = getattr(obj, name, None)
            if fn is None:
                out.append("%-28s MISSING" % name)
                continue
            try:
                out.append("%-28s %s" % (name, inspect.signature(fn)))
            except (TypeError, ValueError):
                out.append("%-28s <builtin>" % name)
    return out


# ---------------------------------------------------------------------------
# the clone
# ---------------------------------------------------------------------------


def _unique_notetype_name(col: Any, desired: str) -> str:
    """Find a name for the clone that doesn't collide with an existing notetype.

    We do this ourselves rather than call ``col.models.ensure_name_unique`` directly:
    across Anki versions that method takes a **notetype dict** and mutates its ``"name"``
    key in place -- it does not take a plain string and return a new one. Passing a bare
    string there fails with ``TypeError: string indices must be integers, not 'str'``,
    because it tries ``that_string["name"]``. Rather than re-guess the exact calling
    convention, this is built from ``col.models.by_name``, which is directly verified and
    already used elsewhere in this module.
    """
    if col.models.by_name(desired) is None:
        return desired
    suffix = 2
    while True:
        candidate = "%s (%d)" % (desired, suffix)
        if col.models.by_name(candidate) is None:
            return candidate
        suffix += 1


#: Keys on an existing field dict that describe *that* field rather than a new one. ``id``
#: matters most: modern Anki tracks fields by it across a change-notetype, so a copy that
#: kept its template's id would be indistinguishable from the field it was copied from.
_FIELD_IDENTITY_KEYS = ("id", "ord", "description", "tag")


def _field_from_template(name: str, template: Dict[str, Any]) -> Dict[str, Any]:
    """A new field dict shaped like one the notetype already has.

    The fallback for when ``col.models.new_field`` isn't usable. Copying an existing field
    means the new one inherits this deck's font and size (so it looks native in the editor)
    and, more importantly, carries whatever keys *this* Anki build expects, without this
    module having to know what they are.
    """
    field = copy.deepcopy(template)
    for key in _FIELD_IDENTITY_KEYS:
        field.pop(key, None)
    field["name"] = name
    field["sticky"] = False
    return field


def make_field_factory(col: Any) -> Callable[[str, Dict[str, Any]], Dict[str, Any]]:
    """Return ``make_field(name, template) -> field dict`` for this collection.

    Prefers ``col.models.new_field``, which ``docs/api-notes.md`` confirms is present on the
    target build (presence verified by byte-scanning; the exact signature was not, hence the
    guarded call). Falls back to copying an existing field rather than hand-building a dict
    whose required keys this addon would be guessing at.
    """
    new_field = getattr(col.models, "new_field", None)

    def make_field(name: str, template: Dict[str, Any]) -> Dict[str, Any]:
        if new_field is not None:
            try:
                built = new_field(name)
            except TypeError:
                built = None  # different calling convention on this build; use the fallback
            if isinstance(built, dict):
                built["name"] = name
                return built
        return _field_from_template(name, template)

    return make_field


def shape_notetype(source: Dict[str, Any], *, name: str, front: str, back: str,
                    css: str, template_name: str,
                    extra_fields: Sequence[str] = (),
                    make_field: Optional[Callable[[str, Dict[str, Any]], Dict[str, Any]]] = None,
                    ) -> Dict[str, Any]:
    """Build the notetype dict a clone should be saved as.

    ``extra_fields`` names fields to add that the source doesn't have -- the generated audio
    fields, per ``core.audio_fields``. They are **appended after** the source's own fields
    and never inserted among them, which keeps every original field at its original ord.
    That is what lets a Flip-in-place change-notetype keep mapping old field *n* to new field
    *n*, with the appended ones having no counterpart to come from (see ``_flip_in_place``).

    Adding a field that is already there is a no-op, so converting an already-converted
    notetype a second time doesn't accumulate duplicates.
    """
    shaped = copy.deepcopy(source)
    shaped["id"] = 0
    shaped["name"] = name

    if not shaped.get("tmpls"):
        raise ApiMismatch("notetype %r has no templates" % source.get("name"))

    if extra_fields:
        flds = shaped.get("flds")
        if not flds:
            raise ApiMismatch("notetype %r has no fields to extend" % source.get("name"))
        flds.sort(key=lambda f: int(f.get("ord", 0)))
        build = make_field or _field_from_template
        present = {str(f["name"]).strip().lower() for f in flds}
        for field_name in extra_fields:
            if str(field_name).strip().lower() in present:
                continue
            flds.append(build(field_name, flds[-1]))
            present.add(str(field_name).strip().lower())
        for index, fld in enumerate(flds):
            fld["ord"] = index
        shaped["flds"] = flds

    # M1 emits a single template. Keep the first, drop the rest: extra templates would
    # generate extra cards in a direction we did not design.
    template = shaped["tmpls"][0]
    template["name"] = template_name
    template["qfmt"] = front
    template["afmt"] = back
    shaped["tmpls"] = [template]
    if css:
        shaped["css"] = css
    return shaped


def build_clone(col: Any, plan: ConversionPlan, front: str, back: str, css: str,
                template_name: str) -> Dict[str, Any]:
    """Create the cloned notetype carrying the generated templates.

    Deliberately *not* ``col.models.copy()``: we control the name and the add step, and
    deepcopy + ``id = 0`` is stable across Anki versions.
    """
    source = col.models.by_name(plan.source_notetype)
    if source is None:
        raise ApiMismatch("notetype %r not found" % plan.source_notetype)

    name = _unique_notetype_name(col, plan.clone_notetype)
    clone = shape_notetype(source, name=name, front=front, back=back, css=css,
                            template_name=template_name,
                            extra_fields=plan.new_fields,
                            make_field=make_field_factory(col))

    added = _call_checked(col.models.add_dict, "col.models.add_dict", notetype=clone)
    clone_id = _extract_id(added, "col.models.add_dict")

    if clone_id == source["id"]:
        raise ApiMismatch(
            "the clone was given the source notetype's id (%s) -- refusing to continue, "
            "this would modify the original" % clone_id
        )

    saved = col.models.by_name(clone["name"])
    if saved is None:
        raise ApiMismatch("clone %r was not saved" % clone["name"])

    # The generated templates already reference these fields; a build that silently dropped
    # one would leave every card rendering a dangling {{Field}}. Checked against what was
    # actually saved, not against what we asked for.
    saved_names = {str(f["name"]).strip().lower() for f in saved.get("flds", [])}
    missing = [n for n in plan.new_fields if str(n).strip().lower() not in saved_names]
    if missing:
        raise ApiMismatch(
            "the clone was saved without the generated audio field(s) %s. Verify "
            "col.models.new_field/add_dict against your Anki build (docs/api-notes.md)."
            % ", ".join(repr(n) for n in missing)
        )
    return saved


# ---------------------------------------------------------------------------
# modes
# ---------------------------------------------------------------------------


def _flip_in_place(col: Any, plan: ConversionPlan, clone: Dict[str, Any]) -> int:
    """Repoint the existing notes onto the clone.

    The clone keeps every source field at its original name and ord, and only *appends* the
    generated audio field(s) after them (see ``shape_notetype``). So Anki's prefilled
    field map is an identity map for all the source's own fields, with the appended ones
    having no source field to come from -- which is exactly right: they are meant to start
    empty, and get filled by the TTS run. Role mapping drives template HTML only, never
    field migration.

    The appended-at-the-end ordering is deliberate rather than incidental. It makes the
    prefilled map correct whether Anki builds it by matching field *names* (the appended
    names exist only on the clone, so they match nothing) or by position (their ords are
    past the end of the source's field list, so there is nothing at that position either).
    Inserting them next to their sibling fields instead would shift every later field by one
    and make the positional reading silently wrong. See docs/api-notes.md.
    """
    source = col.models.by_name(plan.source_notetype)
    info = _call_checked(
        col.models.change_notetype_info,
        "col.models.change_notetype_info",
        old_notetype_id=source["id"],
        new_notetype_id=clone["id"],
    )
    request = getattr(info, "input", None)
    if request is None:
        raise ApiMismatch(
            "change_notetype_info() returned %r with no .input request. Verify against "
            "your Anki build (see docs/api-notes.md)." % type(info).__name__
        )

    del request.note_ids[:]
    request.note_ids.extend(plan.note_ids)
    col.models.change_notetype_of_notes(request)
    return len(plan.note_ids)


def _tag_converted_notes(col: Any, note_ids: Sequence[int], tag: str) -> None:
    """Add ``tag`` to every note in ``note_ids``, for the record ``core.deck_state`` reads
    back on reopen.

    Only needed by Flip-in-place: the notes already existed before this conversion, and
    ``change_notetype_of_notes`` (a bulk schema-level repoint) does not touch tags. New-deck
    mode instead adds the tag inline while building each duplicated note in
    :func:`_new_deck`, since that note is already being written anyway.
    """
    for nid in note_ids:
        note = col.get_note(nid)
        if tag not in note.tags:
            note.tags.append(tag)
            col.update_note(note)


def _copy_fields_by_name(source_note: Any, target_note: Any, field_names: Sequence[str]) -> None:
    """Fill ``target_note``'s fields from ``source_note``, matching by name.

    Fields the clone has that the source note doesn't are skipped rather than raised. That
    is the normal case now, not an edge one: the clone carries the generated audio field(s)
    the conversion just added, and those are *meant* to arrive empty -- there is nothing on
    the source note to copy into them, and the TTS run fills them later.
    """
    for name in field_names:
        try:
            value = source_note[name]
        except (KeyError, IndexError, ValueError):
            continue
        target_note[name] = value


def _new_deck(col: Any, plan: ConversionPlan, clone: Dict[str, Any]) -> int:
    """Duplicate the notes onto the clone, into a fresh deck.

    ``[sound:...]`` references point at files already in the media folder, so media is
    shared rather than duplicated. Anki's duplicate check is per-notetype, so these copies
    raise no duplicate warnings against the originals.
    """
    deck_id = _resolve_deck(col, plan.target_deck)
    clone_fields = [f["name"] for f in clone["flds"]]
    strip = {t.lower() for t in plan.strip_tags}
    # Always stripped, regardless of what the user configured: the tag means "this note's
    # generated audio is current", and a brand-new duplicate's audio never is. Carrying it
    # over would make the TTS batch skip the note as already done, leaving it permanently
    # silent -- its generated audio field is empty, since only the *source* fields are
    # copied across.
    strip.add(AUDIO_DONE_TAG.lower())

    tag = conversion_tag(plan.target_language, plan.native_language)
    created = 0
    for nid in plan.note_ids:
        source_note = col.get_note(nid)
        new_note = col.new_note(clone)
        _copy_fields_by_name(source_note, new_note, clone_fields)
        new_note.tags = [t for t in source_note.tags if t.lower() not in strip]
        new_note.tags.append(tag)
        col.add_note(new_note, deck_id)
        created += 1
    return created


def _resolve_deck(col: Any, name: str) -> int:
    existing = col.decks.by_name(name)
    if existing:
        return int(existing["id"])
    added = _call_checked(
        col.decks.add_normal_deck_with_name, "col.decks.add_normal_deck_with_name", name=name
    )
    return _extract_id(added, "col.decks.add_normal_deck_with_name")


def _reset_scheduling(col: Any, card_ids: Sequence[int]) -> int:
    """Reset the converted cards to new.

    NOTE: ``col.sched.forget_cards`` does **not** exist in Anki 26.08.1 -- the backend
    method is ``schedule_cards_as_new``. ``forget_cards`` only exists as the GUI wrapper
    ``aqt.operations.scheduling.forget_cards``.
    """
    if not card_ids:
        return 0
    fn = getattr(col.sched, "schedule_cards_as_new", None)
    if fn is None:
        raise ApiMismatch(
            "col.sched.schedule_cards_as_new is missing. This addon targets Anki 26.08.1; "
            "re-run the probe in docs/api-notes.md."
        )
    _call_checked(
        fn,
        "col.sched.schedule_cards_as_new",
        card_ids=list(card_ids),
        restore_position=False,
        reset_counts=True,
    )
    return len(card_ids)


# ---------------------------------------------------------------------------
# entry point
# ---------------------------------------------------------------------------


def apply_plan(
    col: Any,
    plan: ConversionPlan,
    *,
    front: str,
    back: str,
    css: str,
    template_name: str = "Production",
    expected_note_count: Optional[int] = None,
) -> ConversionResult:
    """Run ``plan``. Assumes the user has already approved the preflight summary.

    Caller is responsible for wrapping this in a ``CollectionOp`` so it lands as one
    undoable step; see ``addon/ops/convert_op.py``.
    """
    validation = plan.validate()
    validation.raise_if_failed()

    result = ConversionResult(dry_run=plan.dry_run)

    # Re-derive the scope and check it still matches what the user was shown. A mismatch
    # means the collection changed underneath us; better to stop than to write.
    live_ids = list(col.find_notes(plan.scope_query))
    if expected_note_count is not None and len(live_ids) != expected_note_count:
        raise ApiMismatch(
            "the note count changed between preflight and apply (%d -> %d). "
            "Nothing was written; re-run the conversion."
            % (expected_note_count, len(live_ids))
        )
    if set(live_ids) != set(plan.note_ids):
        plan.note_ids = live_ids

    if plan.dry_run:
        # Nothing is cloned or written in a dry run, so there is no notetype id or deck id
        # to report -- only what *would* have happened, from the plan itself.
        result.messages.append(
            "Dry run: nothing was written. %d notes from %r would move onto a new "
            "notetype %r%s."
            % (
                len(plan.note_ids),
                plan.source_notetype,
                plan.clone_notetype,
                (" in a new deck %r" % plan.target_deck)
                if plan.mode is ConversionMode.NEW_DECK
                else " (replacing their current cards)",
            )
        )
        result.notes_converted = len(plan.note_ids)
        return result

    # ``build_clone`` and (for Flip-in-place) ``change_notetype_of_notes`` are both
    # notetype-*schema* changes -- they bump Anki's schema modification time, which
    # invalidates any custom undo marker set before them. A custom-undo-entry span may
    # therefore only ever cover what comes *after* the last such call, never wrap around
    # one; doing so produces Anki's own "target undo op not found" (confirmed against a
    # real collection -- see docs/api-notes.md). Deck creation is not schema-level in this
    # sense (it has never required a full resync), so it's fine inside the wrapped span.
    clone = build_clone(col, plan, front=front, back=back, css=css, template_name=template_name)
    result.clone_notetype_id = int(clone["id"])
    result.clone_notetype_name = clone["name"]

    if plan.mode is ConversionMode.FLIP_IN_PLACE:
        result.notes_converted = _flip_in_place(col, plan, clone)
        undo_entry = col.add_custom_undo_entry("Convert deck direction")
        # After the marker, never before: change_notetype_of_notes just above is a schema
        # change, and Anki invalidates any custom undo marker set before one (see this
        # function's own docstring). Tagging is plain per-note writes, so it groups cleanly
        # into the same undo step as long as it happens after the marker.
        _tag_converted_notes(
            col, plan.note_ids, conversion_tag(plan.target_language, plan.native_language)
        )
    else:
        undo_entry = col.add_custom_undo_entry("Convert deck direction")
        result.notes_converted = _new_deck(col, plan, clone)

    card_ids = list(col.find_cards('mid:%d' % clone["id"]))
    if plan.reset_scheduling:
        result.cards_reset = _reset_scheduling(col, card_ids)

    result.target_deck_id = _resolve_deck(col, plan.target_deck) if plan.mode is ConversionMode.NEW_DECK else 0

    # Every real write above has already committed by this point -- what follows only
    # groups them into one convenient undo step. If merging still raises "target undo op
    # not found" (seen in real use despite the ordering above matching the documented fix
    # in docs/api-notes.md -- some other schema-level touch evidently still lands between
    # the marker and here in at least one real run), that's an undo-*grouping* failure, not
    # a conversion failure: letting it propagate would report a successful conversion as a
    # crash to the user (and, worse, would stop CollectionOp from ever calling our
    # on_success -- so the screen would never notice the new deck/notetype either). Retry
    # with a fresh marker set right now instead: nothing schema-changing can happen between
    # this line and the merge two lines below, so the retry is guaranteed to succeed.
    try:
        result.op_changes = col.merge_undo_entries(undo_entry)
    except Exception:  # noqa: BLE001 -- see comment above; deliberately broad
        # Deliberately not surfaced in result.messages: to the user this undo-grouping
        # hiccup is invisible and irrelevant -- the conversion itself already fully
        # succeeded either way. It's still worth knowing about if it ever needs a real
        # fix, so it stays noted in docs/api-notes.md instead.
        retry_entry = col.add_custom_undo_entry("Convert deck direction")
        result.op_changes = col.merge_undo_entries(retry_entry)

    if plan.mode is ConversionMode.NEW_DECK:
        destination = "into new deck %r" % plan.target_deck
    else:
        destination = "in place, in %r" % plan.source_deck
    result.messages.append(
        "Done: %d notes converted to %r %s; %d cards reset to new."
        % (result.notes_converted, result.clone_notetype_name, destination, result.cards_reset)
    )
    return result
