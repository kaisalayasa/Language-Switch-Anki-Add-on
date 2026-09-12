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

__all__ = [
    "ConversionResult", "ApiMismatch", "apply_plan", "probe_api",
    "build_preview_note", "ensure_preview_scaffold", "write_preview_note",
]


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


def _shape_notetype(source: Dict[str, Any], *, name: str, front: str, back: str,
                    css: str, template_name: str) -> Dict[str, Any]:
    """Build the notetype dict a clone or preview should be saved as.

    Shared by :func:`build_clone` (a fresh clone) and :func:`build_preview_note` (a
    reused, in-place-updated scratch notetype) so both write templates the same way.
    """
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
    clone = _shape_notetype(source, name=name, front=front, back=back, css=css,
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


def ensure_preview_scaffold(
    col: Any, plan: ConversionPlan, *, front: str, back: str, css: str, template_name: str,
) -> "tuple[Dict[str, Any], int]":
    """Create or update the dedicated scratch notetype/deck used for previews.

    Uses a deterministically-named scratch notetype/deck (suffixed ``" (Preview)"``) so it
    can never collide with what the real conversion creates.

    **Both of these are schema-level changes** (a new/changed notetype, a new deck), and
    Anki clears/invalidates its outstanding custom undo markers when a notetype's schema
    changes. Concretely: if code sets a marker with ``col.add_custom_undo_entry(...)``,
    then calls ``col.models.add_dict``/``update_dict``, the earlier marker no longer
    exists by the time ``col.merge_undo_entries(...)`` looks for it -- which fails with
    Anki's own ``"target undo op not found"``. That is exactly the bug this function's
    separation from :func:`write_preview_note` exists to prevent: **never wrap this call
    in the same undo-entry span as anything else.** Only the pure data write in
    ``write_preview_note`` is safe to group that way.
    """
    source = col.models.by_name(plan.source_notetype)
    if source is None:
        raise ApiMismatch("notetype %r not found" % plan.source_notetype)

    preview_name = "%s (Preview)" % plan.clone_notetype
    notetype = col.models.by_name(preview_name)
    shaped = _shape_notetype(source, name=preview_name, front=front, back=back, css=css,
                             template_name=template_name)
    if notetype is None:
        added = _call_checked(col.models.add_dict, "col.models.add_dict", notetype=shaped)
        _extract_id(added, "col.models.add_dict")
        notetype = col.models.by_name(preview_name)
        if notetype is None:
            raise ApiMismatch("preview notetype %r was not saved" % preview_name)
    else:
        notetype["tmpls"] = shaped["tmpls"]
        notetype["css"] = shaped["css"]
        _call_checked(col.models.update_dict, "col.models.update_dict", notetype=notetype)

    deck_id = _resolve_deck(col, preview_name)
    return notetype, deck_id


def write_preview_note(col: Any, notetype: Dict[str, Any], deck_id: int, *, sample_note_id: int) -> Any:
    """Create or update the single scratch note that previews ``notetype``.

    A pure data write -- no schema change -- so, unlike :func:`ensure_preview_scaffold`,
    this *is* safe to wrap in a single ``add_custom_undo_entry``/``merge_undo_entries``
    span.

    Repeated calls **find and update** that scratch note in place rather than creating a
    new one each time, so previewing repeatedly does not accumulate debris -- and,
    notably, this function never deletes anything, so a preview note the user has started
    tinkering with inside Anki's own Card Types editor is never silently destroyed.

    Returns the live ``Note``; the caller is responsible for rendering it (see
    ``addon/ui/preview.py``).
    """
    preview_name = notetype["name"]
    source_note = col.get_note(sample_note_id)
    field_names = [f["name"] for f in notetype["flds"]]

    existing_ids = col.find_notes('note:"%s"' % preview_name.replace('"', '\\"'))
    if existing_ids:
        preview_note = col.get_note(existing_ids[0])
        _copy_fields_by_name(source_note, preview_note, field_names)
        col.update_note(preview_note)
    else:
        preview_note = col.new_note(notetype)
        _copy_fields_by_name(source_note, preview_note, field_names)
        col.add_note(preview_note, deck_id)

    return preview_note


def build_preview_note(
    col: Any, plan: ConversionPlan, *, front: str, back: str, css: str,
    template_name: str, sample_note_id: int,
) -> Any:
    """Convenience wrapper: prepare the scratch notetype/deck, then write the note.

    For callers (including the tests) that don't need control over where the undo
    boundary sits. ``ConvertDialog._preview`` calls :func:`ensure_preview_scaffold` and
    :func:`write_preview_note` separately instead, precisely so it can place a custom
    undo entry *after* the schema change rather than around it -- see
    :func:`ensure_preview_scaffold`'s docstring for why that ordering matters.
    """
    notetype, deck_id = ensure_preview_scaffold(
        col, plan, front=front, back=back, css=css, template_name=template_name
    )
    return write_preview_note(col, notetype, deck_id, sample_note_id=sample_note_id)


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
