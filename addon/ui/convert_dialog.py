"""Conversion dialog: pick a deck, pick a mode, map fields, read the preflight, confirm.

Field mapping comes from ``_resolve_mapping``: the user's own edit via "Map fields…"
(``RoleMapperDialog``) if one exists for the currently selected notetype, else a fresh
content-based guess (``core.role_detect.guess_role_mapping``) -- never nothing, since that
guesser always produces *something* for any two-field-or-more deck (see its own docstring).
A guess that still doesn't validate (e.g. detection found no usable native-side content) is
caught when templates are generated, and the dialog points at "Map fields…" instead of
hard-stopping. This dialog's job stays what it was in M1: make absolutely clear what is
about to happen before anything is written.
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
from aqt.utils import showWarning, tooltip

from ..core.conversion import ConversionMode, build_plan, scope_query
from ..core.role_detect import guess_role_mapping
from ..core.role_schema import RoleMapping, ValidationError, fields_from_notetype
from ..core.template_generator import TemplateOptions, generate_templates
from ..ops.convert_op import convert_op
from ..tts.sanitize import sanitize_text
from .role_mapper import RoleMapperDialog

__all__ = ["ConvertDialog", "show_convert_dialog"]


def addon_config() -> dict:
    """This addon's user config, as a plain dict. Shared with ``card_preview.py`` so both
    read the same defaults rather than duplicating the lookup."""
    return mw.addonManager.getConfig(__name__.split(".")[0]) or {}


def template_options_from_config(config: dict) -> TemplateOptions:
    """Shared with ``card_preview.py`` -- see :func:`addon_config`."""
    return TemplateOptions(
        include_unmapped=config.get("include_unmapped", True),
        include_audio=config.get("include_audio", False),
        audio_on_front=config.get("audio_on_front", True),
        template_name=config.get("template_name", "Production"),
    )


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
        #: (notetype_name, mapping) from the last "Map fields…" session. Only honoured by
        #: ``_resolve_mapping`` while the notetype name still matches the current selection
        #: -- switching the deck/notetype pair naturally drops a stale override.
        self._override: Optional[Tuple[str, RoleMapping]] = None

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
        self.map_fields_button = QPushButton("Map fields…")
        self.map_fields_button.setToolTip(
            "Review or build the field-to-role mapping for this notetype. Pre-filled from a "
            "best guess based on the deck's own content -- correct anything it got wrong."
        )
        self.map_fields_button.clicked.connect(self._open_mapper)
        row.addWidget(self.map_fields_button)
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

    def _resolve_mapping(self, notetype_name: str, live_fields, notetype, note_ids) -> RoleMapping:
        """A mapping for ``notetype_name``.

        Priority: (1) the user's own edit from "Map fields…", kept only while it was built
        for this same notetype -- switching the deck/notetype pair drops a stale override
        automatically; (2) a fresh content-based guess, seeded from a handful of real note
        samples. Never ``None`` -- ``guess_role_mapping`` always returns something, even if
        it doesn't validate; a bad guess is caught later, when templates are generated.
        """
        if self._override is not None and self._override[0] == notetype_name:
            return self._override[1]
        raw_samples = _collect_raw_samples(note_ids[:8])
        tmpls = notetype.get("tmpls") or [{}]
        return guess_role_mapping(
            notetype_name,
            live_fields,
            raw_samples,
            front_html=tmpls[0].get("qfmt", ""),
            back_html=tmpls[0].get("afmt", ""),
            css=notetype.get("css", ""),
        )

    def _build(self):
        """Returns (plan, templates, note_count) or (None, None, reason)."""
        current = self._current()
        if current is None:
            return None, None, "This collection has no notes to convert."
        deck, notetype_name = current

        notetype = mw.col.models.by_name(notetype_name)
        live_fields = fields_from_notetype(notetype["flds"])
        note_ids_for_seed = mw.col.find_notes(scope_query(notetype_name, deck))

        mapping = self._resolve_mapping(notetype_name, live_fields, notetype, note_ids_for_seed)

        config = addon_config()

        try:
            templates = generate_templates(
                mapping,
                source_css=notetype.get("css", ""),
                options=template_options_from_config(config),
            )
        except ValidationError as exc:
            return None, None, (
                "No usable field mapping yet for %r: %s\n\n"
                "Click \"Map fields…\" below to fix it." % (notetype_name, exc)
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
        judge the card layout before anything is written.
        """
        text = plan.preflight().as_text()
        if templates is not None:
            sep = "\n" + "-" * 60 + "\n"
            text += sep + "GENERATED FRONT TEMPLATE" + sep + templates.front_html
            text += sep + "GENERATED BACK TEMPLATE" + sep + templates.back_html
        return text

    def _refresh(self):
        self.map_fields_button.setEnabled(self._current() is not None)

        plan, templates, info = self._build()
        ok_button = self.buttons.button(QDialogButtonBox.StandardButton.Ok)
        if plan is None:
            self.preflight.setPlainText(str(info))
            ok_button.setEnabled(False)
            return

        validation = plan.validate()
        text = self._describe(plan, templates)
        if validation.errors:
            text += "\n\nCannot proceed:\n" + "\n".join("  - %s" % e for e in validation.errors)
        self.preflight.setPlainText(text)
        ok_button.setEnabled(validation.ok)

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

    # -- field mapping --------------------------------------------------

    def _open_mapper(self):
        current = self._current()
        if current is None:
            return
        deck, notetype_name = current
        notetype = mw.col.models.by_name(notetype_name)
        live_fields = fields_from_notetype(notetype["flds"])
        note_ids = mw.col.find_notes(scope_query(notetype_name, deck))

        seed = self._resolve_mapping(notetype_name, live_fields, notetype, note_ids)
        samples = _collect_samples(note_ids[:3])

        dialog = RoleMapperDialog(
            self,
            notetype_name=notetype_name,
            live_fields=live_fields,
            samples=samples,
            initial_mapping=seed,
        )
        if dialog.exec():
            self._override = (notetype_name, dialog.result_mapping())
            self._refresh()


def _dry_run(plan, templates):
    from ..ops.convert_op import run_conversion

    return run_conversion(mw.col, plan, templates)


def _collect_samples(note_ids, *, limit_per_field: int = 3, max_len: int = 60):
    """A few real, HTML-stripped sample values per field, for the mapper's Sample column.

    Field names lie (see CLAUDE.md's "Field names are untrusted input"), so showing what a
    field actually *contains* is the only way a human can map it sensibly. Reuses M2's sanitizer in
    strip-markup-only mode (``allowed_ranges=()``) rather than writing a second HTML
    stripper for display purposes.
    """
    samples: dict = {}
    for nid in note_ids:
        note = mw.col.get_note(nid)
        for name in note.keys():
            bucket = samples.setdefault(name, [])
            if len(bucket) >= limit_per_field:
                continue
            value = sanitize_text(note[name], allowed_ranges=())[:max_len]
            if value:
                bucket.append(value)
    return samples


def _collect_raw_samples(note_ids, *, limit_per_field: int = 8, max_len: int = 400):
    """Unsanitized per-field samples for content-based role detection
    (``core.role_detect.guess_role_mapping``).

    Unlike :func:`_collect_samples` (the HTML-stripped version used for the field list's
    display column), this keeps ``[sound:...]`` tags and ``kanji[kana]``-style ruby markup
    intact -- those are exactly the signals the guesser looks for, and
    ``tts.sanitize.sanitize_text`` strips both unconditionally regardless of
    ``allowed_ranges``. Blank values are kept (not skipped) so the bucket's length reflects
    notes actually scanned, which the guesser's emptiness-ratio signal depends on. A larger
    ``limit_per_field`` than the display sampler's default: that ratio is noisy at n=3.
    """
    samples: dict = {}
    for nid in note_ids:
        note = mw.col.get_note(nid)
        for name in note.keys():
            bucket = samples.setdefault(name, [])
            if len(bucket) >= limit_per_field:
                continue
            bucket.append(note[name][:max_len])
    return samples


def _on_windows() -> bool:
    import sys

    return sys.platform.startswith("win")


def show_convert_dialog() -> None:
    if mw.col is None:
        showWarning("Open a collection first.")
        return
    ConvertDialog(mw).exec()
