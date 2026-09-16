"""Run a conversion as a CollectionOp.

Ideally every write in a conversion would group under one custom undo entry, so a single
Ctrl+Z reverts the whole thing. That's not fully achievable: creating the clone notetype
(and, for Flip-in-place, moving notes onto it) is a notetype *schema* change, and Anki
invalidates any custom undo marker spanning across one. So in practice a conversion is
**two or three separate undo steps**, not one -- see the ordering comment in
``apply_plan`` (`notetype_manager.py`) for exactly where the boundaries fall. Undoing all
of them fully reverses a conversion; it just isn't a single keystroke.
"""

from __future__ import annotations

from typing import Any, Callable, Optional, Tuple

from ..core.conversion import ConversionPlan
from ..core.deck_state import append_css_marker
from .notetype_manager import ConversionResult, apply_plan

__all__ = ["run_conversion", "convert_op"]


def run_conversion(
    col: Any,
    plan: ConversionPlan,
    *,
    expected_note_count: Optional[int] = None,
) -> Tuple[ConversionResult, Any]:
    """Apply ``plan``.

    Returns ``(result, op_changes)``. ``op_changes`` is whatever ``col.merge_undo_entries``
    returned inside :func:`apply_plan` -- a ``CollectionOp`` needs it to refresh the UI --
    or ``None`` for a dry run.

    The undo entry is created and merged **inside** ``apply_plan``, not wrapped around the
    whole call here: cloning the notetype is a schema change, and Anki invalidates any
    custom undo marker set before a schema change. Only ``apply_plan`` knows exactly where
    its own schema-changing calls sit, so only it can place the marker correctly -- an
    earlier version of this function set the marker out here, which failed every time with
    Anki's own ``"target undo op not found"`` (confirmed against a real collection; see
    docs/api-notes.md).

    Safe to call off the Qt main thread: it touches no widgets.
    """
    css = append_css_marker(plan.css, plan.target_language, plan.native_language)
    result = apply_plan(
        col,
        plan,
        front=plan.front,
        back=plan.back,
        css=css,
        template_name=plan.template_name,
        expected_note_count=expected_note_count,
    )
    return result, result.op_changes


def convert_op(
    parent: Any,
    plan: ConversionPlan,
    *,
    expected_note_count: Optional[int] = None,
    on_success: Optional[Callable[[ConversionResult], None]] = None,
):
    """Wrap :func:`run_conversion` in an ``aqt`` CollectionOp.

    ``aqt`` is imported lazily so this module stays importable -- and testable -- outside
    Anki.
    """
    from aqt.operations import CollectionOp  # noqa: WPS433 - intentional lazy import

    holder: dict = {}

    def _op(col):
        result, changes = run_conversion(col, plan, expected_note_count=expected_note_count)
        holder["result"] = result
        return changes

    op = CollectionOp(parent=parent, op=_op)
    if on_success is not None:
        op = op.success(lambda _changes: on_success(holder.get("result")))
    return op
