"""M1 conversion dialog: pick a deck, pick a mode, read the preflight, confirm.

Deliberately plain. The real mapping UI arrives in M3; until then the mapping comes from a
matched profile and this dialog's job is to make absolutely clear what is about to happen
before anything is written.
"""

from __future__ import annotations

from typing import List, Optional, Tuple

from aqt import mw
from aqt.qt import (
    QCheckBox,
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QFont,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QPlainTextEdit,
    QPushButton,
    QVBoxLayout,
)
from aqt.operations import CollectionOp
from aqt.utils import showWarning, tooltip

from ..core.conversion import ConversionMode, build_plan
from ..core.profiles import match_profile
from ..core.role_schema import fields_from_notetype
from ..core.template_generator import TemplateOptions, generate_templates
from ..ops.convert_op import convert_op
from ..ops.notetype_manager import ensure_preview_scaffold, write_preview_note
from .preview import CardLayoutUnavailable, open_card_layout

__all__ = ["ConvertDialog", "show_convert_dialog"]


def _decks_with_notetypes(col) -> List[Tuple[str, str, int]]:
    """Every (deck, notetype, note count) pairing that actually has notes.

    Built from real data rather than from the deck list, because a deck can hold several
    notetypes and a notetype can span several decks -- and the conversion is scoped by the
    pair, not by either alone.
    """
    out: List[Tuple[str, str, int]] = []
    for deck in col.decks.all_names_and_ids():
        for notetype in col.models.all_names_and_ids():
            query = 'deck:"%s" note:"%s"' % (deck.name.replace('"', '\\"'),
                                             notetype.name.replace('"', '\\"'))
            count = len(col.find_notes(query))
            if count:
                out.append((deck.name, notetype.name, count))
    return sorted(out, key=lambda row: (-row[2], row[0]))


class ConvertDialog(QDialog):
    def __init__(self, parent=None):
        super().__init__(parent or mw)
        self.setWindowTitle("Convert deck language direction")
        self.resize(640, 560)
        self._pairs = _decks_with_notetypes(mw.col)

        layout = QVBoxLayout(self)

        layout.addWidget(QLabel("<b>1. What to convert</b>"))
        self.pair_box = QComboBox()
        for deck, notetype, count in self._pairs:
            self.pair_box.addItem("%s  —  %s  (%d notes)" % (deck, notetype, count))
        layout.addWidget(self.pair_box)

        layout.addWidget(QLabel("<b>2. How</b>"))
        self.mode_box = QComboBox()
        self.mode_box.addItem(
            "New deck — non-destructive (recommended)", ConversionMode.NEW_DECK
        )
        self.mode_box.addItem(
            "Flip in place — replaces the existing cards", ConversionMode.FLIP_IN_PLACE
        )
        layout.addWidget(self.mode_box)

        row = QHBoxLayout()
        self.dry_run = QCheckBox("Dry run (show what would happen, write nothing)")
        row.addWidget(self.dry_run)
        row.addStretch(1)
        self.preview_button = QPushButton("Preview card…")
        self.preview_button.setToolTip(
            "Opens Anki's own Card Types editor on one real note, rendered with the "
            "generated template -- an actual preview, not just the template markup.\n\n"
            "Creates a small scratch notetype/deck named \"<name> (Preview)\" to do this. "
            "It never touches your original notes, and repeated previews reuse the same "
            "scratch note instead of piling up."
        )
        self.preview_button.clicked.connect(self._preview)
        row.addWidget(self.preview_button)
        layout.addLayout(row)

        preflight_group = QGroupBox("3. Preflight — nothing is written until you press OK")
        preflight_layout = QVBoxLayout(preflight_group)
        self.preflight = QPlainTextEdit()
        self.preflight.setReadOnly(True)
        self.preflight.setFont(QFont("Consolas" if _on_windows() else "Monospace", 9))
        preflight_layout.addWidget(self.preflight)
        layout.addWidget(preflight_group, stretch=1)

        self.buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel
        )
        self.buttons.accepted.connect(self._apply)
        self.buttons.rejected.connect(self.reject)
        layout.addWidget(self.buttons)

        self.pair_box.currentIndexChanged.connect(self._refresh)
        self.mode_box.currentIndexChanged.connect(self._refresh)
        self.dry_run.stateChanged.connect(self._refresh)
        self._refresh()

    # -- plan assembly ------------------------------------------------------

    def _current(self):
        if not self._pairs:
            return None
        deck, notetype, _ = self._pairs[self.pair_box.currentIndex()]
        return deck, notetype

    def _build(self):
        """Returns (plan, templates, note_count) or (None, None, reason)."""
        current = self._current()
        if current is None:
            return None, None, "This collection has no notes to convert."
        deck, notetype_name = current

        notetype = mw.col.models.by_name(notetype_name)
        live_fields = fields_from_notetype(notetype["flds"])

        match = match_profile(notetype_name, live_fields)
        if not match.usable:
            # This is the branch the M3 mapping UI will plug into.
            if match.profile is None:
                return None, None, (
                    "No field mapping is known for the notetype %r yet.\n\n"
                    "M1 ships one mapping (Core 2000). The role-mapping UI that lets you "
                    "define your own arrives in M3." % notetype_name
                )
            errors = match.validation.errors if match.validation else ["field list differs"]
            return None, None, (
                "A profile named %r exists but does not fit this notetype, so it was "
                "refused rather than force-fitted:\n\n  - %s"
                % (match.profile.id, "\n  - ".join(errors))
            )

        mapping = match.profile.to_mapping(live_fields=live_fields)
        config = mw.addonManager.getConfig(__name__.split(".")[0]) or {}

        templates = generate_templates(
            mapping,
            source_css=notetype.get("css", ""),
            options=TemplateOptions(
                include_unmapped=config.get("include_unmapped", True),
                include_audio=config.get("include_audio", False),
                audio_on_front=config.get("audio_on_front", True),
                template_name=config.get("template_name", "Production"),
            ),
        )

        plan = build_plan(
            mode=self.mode_box.currentData(),
            mapping=mapping,
            source_notetype=notetype_name,
            source_deck=deck,
            suffix=config.get("name_suffix", "Converted"),
            dry_run=self.dry_run.isChecked(),
        )
        plan.strip_tags = list(config.get("strip_tags", ["leech"]))
        plan.note_ids = list(mw.col.find_notes(plan.scope_query))
        return plan, templates, len(plan.note_ids)

    def _describe(self, plan, templates) -> str:
        """Preflight summary plus the actual template HTML that will be written.

        The summary alone (mode, counts, deck names) tells you what will happen but not
        what the result will look like. Showing the generated Front/Back template lets you
        judge the card layout before anything is written -- this is the same output
        ``tools/preview_templates.py`` prints, just inline in the dialog.
        """
        text = plan.preflight().as_text()
        if templates is not None:
            sep = "\n" + "-" * 60 + "\n"
            text += sep + "GENERATED FRONT TEMPLATE" + sep + templates.front_html
            text += sep + "GENERATED BACK TEMPLATE" + sep + templates.back_html
        return text

    def _refresh(self):
        plan, templates, info = self._build()
        ok_button = self.buttons.button(QDialogButtonBox.StandardButton.Ok)
        if plan is None:
            self.preflight.setPlainText(str(info))
            ok_button.setEnabled(False)
            self.preview_button.setEnabled(False)
            return

        validation = plan.validate()
        text = self._describe(plan, templates)
        if validation.errors:
            text += "\n\nCannot proceed:\n" + "\n".join("  - %s" % e for e in validation.errors)
        self.preflight.setPlainText(text)
        ok_button.setEnabled(validation.ok)
        # Previewing only needs *a* note to render, not a fully valid plan -- e.g. it
        # still works while "new-deck mode must write to a different deck" is unresolved.
        self.preview_button.setEnabled(bool(plan.note_ids))

    # -- apply --------------------------------------------------------------

    def _apply(self):
        plan, templates, count = self._build()
        if plan is None:
            showWarning(str(count), parent=self)
            return

        if plan.dry_run:
            result, _ = _dry_run(plan, templates)
            self.preflight.setPlainText(
                self._describe(plan, templates) + "\n\n" + "\n".join(result.messages)
            )
            return

        def done(result):
            if result is None:
                return
            tooltip("\n".join(result.messages), parent=mw)

        self.accept()
        convert_op(
            mw, plan, templates, expected_note_count=count, on_success=done
        ).run_in_background()

    def _preview(self):
        """Build the scratch preview note through a proper ``CollectionOp``.

        Building it, and opening ``CardLayout``, are two different responsibilities that
        need to run in two different places: the collection write goes through Anki's
        sanctioned operations path (background thread, then its own undo/UI-refresh
        bookkeeping), while opening the dialog is a Qt widget construction that must stay
        on the main thread -- exactly where ``CollectionOp``'s ``.success()`` runs.

        Within the write itself, the undo entry is deliberately created **after**
        ``ensure_preview_scaffold`` (which may add/update the scratch notetype) rather
        than wrapped around it: a notetype schema change invalidates Anki's outstanding
        undo markers, so spanning one produces Anki's own
        ``"target undo op not found"`` error. See ``ensure_preview_scaffold``'s docstring.
        """
        plan, templates, count = self._build()
        if plan is None:
            showWarning(str(count), parent=self)
            return
        if not plan.note_ids:
            showWarning("No notes are in scope to preview.", parent=self)
            return

        sample_note_id = plan.note_ids[0]
        holder: dict = {}

        def op(col):
            notetype, deck_id = ensure_preview_scaffold(
                col,
                plan,
                front=templates.front_html,
                back=templates.back_html,
                css=templates.css,
                template_name=templates.template_name,
            )
            undo_entry = col.add_custom_undo_entry("Build preview card")
            holder["note"] = write_preview_note(
                col, notetype, deck_id, sample_note_id=sample_note_id
            )
            return col.merge_undo_entries(undo_entry)

        def on_success(_changes):
            note = holder.get("note")
            if note is None:
                return
            try:
                dialog = open_card_layout(mw, note, parent=self)
            except CardLayoutUnavailable as exc:
                showWarning(str(exc), parent=self)
                return
            dialog.exec()

        CollectionOp(parent=self, op=op).success(on_success).run_in_background()


def _dry_run(plan, templates):
    from ..ops.convert_op import run_conversion

    return run_conversion(mw.col, plan, templates)


def _on_windows() -> bool:
    import sys

    return sys.platform.startswith("win")


def show_convert_dialog() -> None:
    if mw.col is None:
        showWarning("Open a collection first.")
        return
    ConvertDialog(mw).exec()
