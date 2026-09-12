"""Open a real, rendered card preview using Anki's own Card Types editor -- with zero
writes to the collection.

``claude.md`` says to reuse ``aqt.clayout.CardLayout`` for preview rather than building a
raw renderer. An earlier version of this module did that by writing a scratch
notetype/deck/note via a ``CollectionOp`` and opening ``CardLayout`` on it -- which turned
out to be the actual bug: ``CollectionOp``'s completion handling fires Anki's
``state_did_reset`` hook whenever a notetype changes, and opening a modal, WebEngine-backed
``CardLayout`` right in the middle of that reset cascade produced a real, repeatable hang
(confirmed by testing -- see ``docs/api-notes.md``) severe enough that it left the whole Anki
session unresponsive, including features unrelated to this addon.

The actual fix is architectural, not timing-related: **don't write anything at all.**
Reading ``CardLayout``'s real source (pulled from the matching ``aqt`` wheel on PyPI, since
the installed build's compiled module can't be introspected -- see ``docs/api-notes.md`)
shows it already supports fully in-memory template editing. When a human types into its
Front/Back/Style boxes, it writes straight into its own private in-memory copy of the
notetype and re-renders an *ephemeral*, unsaved card -- nothing reaches the collection
unless the user explicitly clicks its own Save button. So this module opens ``CardLayout``
on a real, existing note (using its real, current notetype -- exactly like every real call
site in Anki's own codebase), then drives the same Front/Back/Style boxes a human would, to
swap in the generated templates. No clone, no scratch notetype, no ``CollectionOp``, nothing
to undo.
"""

from __future__ import annotations

import inspect
import sys
from typing import Any, List, Optional

__all__ = ["open_card_layout", "CardLayoutUnavailable", "open_live_preview"]


class CardLayoutUnavailable(RuntimeError):
    """``aqt.clayout.CardLayout`` could not be constructed with any attempted call shape."""


# The first attempt is CardLayout's real, confirmed signature for this Anki version --
# def __init__(self, mw, note, ord=0, parent=None, fill_empty=False) -- pulled from the
# actual aqt==26.8.1 source (see docs/api-notes.md). The remaining entries stay as a
# fallback for other Anki versions this addon might run against, most-featured first.
_ATTEMPTS = [
    {"ord": 0, "fill_empty": False},
    {"ord": 0},
    {},
]


def open_card_layout(mw: Any, note: Any, *, parent: Any = None) -> Any:
    """Open Anki's Card Types editor on ``note``, rendered with its current templates.

    Raises :class:`CardLayoutUnavailable` with a message pointing at the probe to run,
    rather than crashing on a version mismatch.
    """
    try:
        from aqt.clayout import CardLayout
    except ImportError as exc:
        raise CardLayoutUnavailable(
            "aqt.clayout.CardLayout could not be imported: %r. This Anki build may not "
            "ship the Card Types editor under the expected module path." % (exc,)
        ) from exc

    errors: List[str] = []
    for kwargs in _ATTEMPTS:
        call_kwargs = dict(kwargs)
        if parent is not None:
            call_kwargs["parent"] = parent
        try:
            return CardLayout(mw, note, **call_kwargs)
        except TypeError as exc:
            errors.append("CardLayout(mw, note, **%r) -> %s" % (call_kwargs, exc))
            continue

    try:
        signature = str(inspect.signature(CardLayout.__init__))
    except (TypeError, ValueError):
        signature = "<not introspectable>"

    raise CardLayoutUnavailable(
        "Could not construct aqt.clayout.CardLayout with any of the attempted call "
        "shapes.\n\nInstalled signature: CardLayout.__init__%s\n\nAttempts tried:\n  %s\n\n"
        "Please paste this message, plus the signature above, so docs/api-notes.md can be "
        "updated with the exact call shape for your Anki build."
        % (signature, "\n  ".join(errors))
    )


def _inject_generated_templates(dialog: Any, *, front: str, back: str, css: str) -> None:
    """Swap the generated Front/Back/CSS into an already-open ``CardLayout``.

    Drives the same widgets a human editing the card type by hand would use --
    ``dialog.tform``'s front/back/style buttons and its shared ``edit_area`` text box --
    rather than reaching into ``CardLayout``'s private model/redraw internals ourselves.
    Real Anki's own ``fill_fields_from_template``/``write_edits_to_template_and_redraw``
    (confirmed from source, see docs/api-notes.md) do the actual work in response; this
    function only simulates the clicks and text changes, so every bit of ``CardLayout``'s
    own change-tracking and debounced re-render keeps working exactly as designed.

    Right after construction the Front tab is already showing (``current_editor_index``
    starts at ``0``), so the first edit needs no tab switch.
    """
    tform = dialog.tform
    tform.edit_area.setPlainText(front)
    tform.back_button.click()
    tform.edit_area.setPlainText(back)
    tform.style_button.click()
    tform.edit_area.setPlainText(css)
    tform.front_button.click()  # leave the dialog showing the Front tab


def open_live_preview(parent: Any, note: Any, *, front: str, back: str, css: str) -> None:
    """Open Anki's real Card Types editor on ``note``, showing the generated templates.

    ``note`` should be a real, existing note (its own real notetype is what gets opened --
    this never creates or touches anything). Always opens on template ordinal 0 -- the
    same one a conversion actually uses (``_shape_notetype`` keeps only the first template
    of whatever notetype it clones). Raises :class:`CardLayoutUnavailable` if ``CardLayout``
    itself could not be constructed; the caller decides how to surface that.
    """
    from aqt import mw

    dialog = open_card_layout(mw, note, parent=parent)
    _inject_generated_templates(dialog, front=front, back=back, css=css)
    if sys.platform.startswith("win"):
        # Matches a real, documented quirk in Anki's own editor.py (onCardLayout): on
        # Windows, CardLayout's parent needs to be explicitly re-activated after it opens.
        parent.activateWindow()
