"""Tools-menu entry point: "Preview converted card…".

Picks a (deck, notetype) pair, seeds a mapping from the deck's own content (see
``core.role_detect.guess_role_mapping``), and opens Anki's real Card Types editor directly
on one real note from it -- no intermediate dialog to click through, no collection write,
nothing to undo (see ``addon/ui/preview.py`` for why that matters: an earlier
scratch-notetype-based approach here caused a real, repeatable Anki hang).

Deliberately zero-input: pick the item from the menu and it previews immediately, using
whatever the content-based guesser produces. A guess that doesn't validate (e.g. detection
found no usable content on one side) is reported instead of guessed around -- this
standalone preview never opens the mapper dialog itself, since previewing an in-progress,
not-yet-saved mapping isn't available yet.
"""

from __future__ import annotations

from aqt import mw
from aqt.qt import QInputDialog
from aqt.utils import showWarning

from ..core.conversion import scope_query
from ..core.role_detect import guess_role_mapping
from ..core.role_schema import ValidationError, fields_from_notetype
from ..core.template_generator import generate_templates
from .convert_dialog import (
    _collect_raw_samples,
    _decks_with_notetypes,
    addon_config,
    template_options_from_config,
)
from .preview import CardLayoutUnavailable, open_live_preview

__all__ = ["show_card_preview"]


def show_card_preview() -> None:
    if mw.col is None:
        showWarning("Open a collection first.")
        return

    pairs = _decks_with_notetypes(mw.col)
    if not pairs:
        showWarning("This collection has no notes to preview.")
        return

    if len(pairs) == 1:
        deck, notetype_name, _ = pairs[0]
    else:
        labels = ["%s  —  %s  (%d notes)" % pair for pair in pairs]
        label, ok = QInputDialog.getItem(
            mw, "Preview converted card", "What to preview:", labels, 0, False
        )
        if not ok:
            return
        deck, notetype_name, _ = pairs[labels.index(label)]

    notetype = mw.col.models.by_name(notetype_name)
    live_fields = fields_from_notetype(notetype["flds"])
    note_ids = mw.col.find_notes(scope_query(notetype_name, deck))
    if not note_ids:
        showWarning("No notes found for %r in %r." % (notetype_name, deck))
        return

    raw_samples = _collect_raw_samples(note_ids[:8])
    tmpls = notetype.get("tmpls") or [{}]
    mapping = guess_role_mapping(
        notetype_name,
        live_fields,
        raw_samples,
        front_html=tmpls[0].get("qfmt", ""),
        back_html=tmpls[0].get("afmt", ""),
        css=notetype.get("css", ""),
    )

    try:
        templates = generate_templates(
            mapping,
            source_css=notetype.get("css", ""),
            options=template_options_from_config(addon_config()),
        )
    except ValidationError as exc:
        showWarning(
            "Couldn't guess a usable field mapping for %r: %s\n\n"
            "Use Tools → Convert deck language direction… → \"Map fields…\" to build one "
            "by hand." % (notetype_name, exc)
        )
        return

    note = mw.col.get_note(note_ids[0])

    try:
        open_live_preview(
            mw, note,
            front=templates.front_html, back=templates.back_html, css=templates.css,
        )
    except CardLayoutUnavailable as exc:
        showWarning(str(exc))
