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

from ..core.conversion import ConversionMode, ConversionPlan

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


def shape_notetype(source: Dict[str, Any], *, name: str, front: str, back: str,
                    css: str, template_name: str) -> Dict[str, Any]:
    """Build the notetype dict a clone should be saved as."""
    shaped = copy.deepcopy(source)
    shaped["id"] = 0
    shaped["name"] = name

    if not shaped.get("tmpls"):
        raise ApiMismatch("notetype %r has no templates" % source.get("name"))

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
                            template_name=template_name)

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
    return saved


# ---------------------------------------------------------------------------
# modes
# ---------------------------------------------------------------------------


def _flip_in_place(col: Any, plan: ConversionPlan, clone: Dict[str, Any]) -> int:
    """Repoint the existing notes onto the clone.

    The clone preserves field names, order and count, so the field/template map Anki
    prefills is an identity map -- which is the correct explicit map here, not a "blind
    positional" one. Role mapping drives template HTML only, never field migration.
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


def _copy_fields_by_name(source_note: Any, target_note: Any, field_names: Sequence[str]) -> None:
    """Fill ``target_note``'s fields from ``source_note``, matching by name.

    Fields the clone has that the source note doesn't (shouldn't normally happen, since
    the clone's field list comes from the source notetype) are silently skipped rather
    than raised -- this is a best-effort copy, not a validation step.
    """
    for name in field_names:
        try:
            value = source_note[name]
        except (KeyError, IndexError):
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

    created = 0
    for nid in plan.note_ids:
        source_note = col.get_note(nid)
        new_note = col.new_note(clone)
        _copy_fields_by_name(source_note, new_note, clone_fields)
        new_note.tags = [t for t in source_note.tags if t.lower() not in strip]
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
    else:
        undo_entry = col.add_custom_undo_entry("Convert deck direction")
        result.notes_converted = _new_deck(col, plan, clone)

    card_ids = list(col.find_cards('mid:%d' % clone["id"]))
    if plan.reset_scheduling:
        result.cards_reset = _reset_scheduling(col, card_ids)

    result.target_deck_id = _resolve_deck(col, plan.target_deck) if plan.mode is ConversionMode.NEW_DECK else 0
    result.op_changes = col.merge_undo_entries(undo_entry)
    result.messages.append(
        "%d notes -> %r; %d cards reset to new."
        % (result.notes_converted, result.clone_notetype_name, result.cards_reset)
    )
    return result
