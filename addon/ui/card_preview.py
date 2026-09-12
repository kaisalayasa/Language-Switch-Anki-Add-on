"""Tools-menu entry point: "Preview converted card…".

Picks a (deck, notetype) pair with a shipped-profile mapping and opens Anki's real Card
Types editor directly on one real note from it -- no intermediate dialog to click through,
no collection write, nothing to undo (see ``addon/ui/preview.py`` for why that matters: an
earlier scratch-notetype-based approach here caused a real, repeatable Anki hang).

Deliberately scoped to shipped profiles only. An in-progress, not-yet-saved mapping built in
``RoleMapperDialog`` only exists in that dialog's own memory -- there is nowhere else for a
standalone menu action to read it from. For Core 2000 (the one shipped profile so far) this
needs zero input at all beyond picking the item from the menu.
"""

from __future__ import annotations

from aqt import mw
from aqt.qt import QInputDialog
from aqt.utils import showWarning

from ..core.conversion import scope_query
from ..core.profiles import match_profile
from ..core.role_schema import fields_from_notetype
from ..core.template_generator import generate_templates
from .convert_dialog import _decks_with_notetypes, addon_config, template_options_from_config
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
    match = match_profile(notetype_name, live_fields)
    if not match.usable:
        showWarning(
            "No shipped field mapping is known for %r yet.\n\n"
            "Use Tools → Convert deck language direction… → \"Map fields…\" "
            "to build one -- this standalone preview only works from a shipped profile."
            % notetype_name
        )
        return

    mapping = match.profile.to_mapping(live_fields=live_fields)
    templates = generate_templates(
        mapping,
        source_css=notetype.get("css", ""),
        options=template_options_from_config(addon_config()),
    )

    note_ids = mw.col.find_notes(scope_query(notetype_name, deck))
    if not note_ids:
        showWarning("No notes found for %r in %r." % (notetype_name, deck))
        return
    note = mw.col.get_note(note_ids[0])

    try:
        open_live_preview(
            mw, note,
            front=templates.front_html, back=templates.back_html, css=templates.css,
        )
    except CardLayoutUnavailable as exc:
        showWarning(str(exc))
