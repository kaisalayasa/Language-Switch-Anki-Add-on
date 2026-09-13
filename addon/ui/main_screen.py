"""The single-screen redesign: one dialog replacing the four-entry Tools submenu.

Rolled out *alongside* the existing submenu at first, not in place of it -- see the plan
this was built from. Both entry points call into exactly the same, unmodified core/ops
logic (``core.conversion``, ``core.template_generator``, ``ops.convert_op``,
``ops.tts_batch``); nothing about *what* a conversion or a TTS batch does changes here,
only how you get to it. Once this screen is verified working end to end in a real profile,
the old submenu and its dialogs (``convert_dialog.py``, ``role_mapper.py``, ``preview.py``,
``card_preview.py``, ``tts_batch_dialog.py``, ``piper_test_dialog.py``) are removed and
``entrypoint.py`` drops to this one action.

Layout, top to bottom: deck/mode picker; a language-detection banner with a manual
override; a left/right split (a field/role list by default, or a raw Front/Back/Styling
editor when "HTML" is switched on, on the left -- and, on the right, a live preview,
``preview_panel.PreviewPanel``, an embeddable ``AnkiWebView`` that never writes to the
collection); a voice picker with a sample button that speaks the current note's own target-
language text; then Save as profile / Convert / Generate TTS audio.
"""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from aqt import mw
from aqt.operations import QueryOp
from aqt.qt import (
    QButtonGroup,
    QCheckBox,
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QHBoxLayout,
    QInputDialog,
    QLabel,
    QLineEdit,
    QPlainTextEdit,
    QPushButton,
    QRadioButton,
    QSplitter,
    Qt,
    QStackedWidget,
    QVBoxLayout,
    QWidget,
    pyqtSignal,
)
from aqt.sound import av_player
from aqt.utils import askUser, showInfo, showWarning

from ..core.conversion import ConversionMode, build_plan, scope_query
from ..core.language_detect import detect_field_language
from ..core.profiles import USER_PROFILE_DIR, load_profiles, match_profile, save_profile, slugify
from ..core.role_schema import (
    FieldBinding,
    Role,
    RoleMapping,
    ValidationError,
    fields_from_notetype,
    mapping_from_assignments,
)
from ..core.template_generator import GeneratedTemplates, generate_templates, split_render_order
from ..ops.convert_op import convert_op
from ..ops.tts_batch import notes_needing_audio
from ..ops.tts_runner import BatchOutcome, default_concurrency, run_tts_batch
from ..tts.piper_provider import PiperProvider
from ..tts.piper_voice_manager import CURATED_VOICES
from .convert_dialog import (
    _collect_samples,
    _decks_with_notetypes,
    addon_config,
    template_options_from_config,
)
from .field_list import FieldListWidget
from .preview_panel import PreviewPanel

__all__ = ["MainScreen", "show_main_screen"]

#: Fallback only -- used before a deck/notetype is picked, or if the current note has no
#: usable target-language text yet. Once a note is available, the sample button speaks
#: *that note's* own text instead (see ``MainScreen._sample_text``).
_SAMPLE_TEXT = "This is a sample sentence for testing."

_DEFAULT_VOICE_ID = "en_GB-alba-medium"

_BATCH_CHUNK = 500


def _cache_dir() -> Path:
    return Path(__file__).resolve().parent.parent / "user_files"


class _RawHtmlEditor(QWidget):
    """Front/Back/Styling tabs over one text editor -- mirrors ``aqt.clayout.CardLayout``'s
    own ``tform`` pattern (see ``addon/ui/preview.py``'s now-superseded
    ``_inject_generated_templates``), rebuilt here as a plain, standalone widget since this
    screen doesn't launch CardLayout itself."""

    changed = pyqtSignal()

    def __init__(self, parent: Any = None):
        super().__init__(parent)
        self._front = ""
        self._back = ""
        self._css = ""
        self._active = 0
        self._loading = False

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)

        tab_row = QHBoxLayout()
        self.front_button = QPushButton("Front Template")
        self.front_button.setCheckable(True)
        self.front_button.setChecked(True)
        self.back_button = QPushButton("Back Template")
        self.back_button.setCheckable(True)
        self.style_button = QPushButton("Styling")
        self.style_button.setCheckable(True)
        group = QButtonGroup(self)
        for b in (self.front_button, self.back_button, self.style_button):
            group.addButton(b)
            tab_row.addWidget(b)
        tab_row.addStretch(1)
        layout.addLayout(tab_row)

        self.editor = QPlainTextEdit()
        self.editor.textChanged.connect(self._on_text_changed)
        layout.addWidget(self.editor, stretch=1)

        self.front_button.clicked.connect(lambda: self._switch_to(0))
        self.back_button.clicked.connect(lambda: self._switch_to(1))
        self.style_button.clicked.connect(lambda: self._switch_to(2))

    def set_content(self, front: str, back: str, css: str) -> None:
        self._front, self._back, self._css = front, back, css
        self._show_active()

    def get_content(self) -> Tuple[str, str, str]:
        self._store_active()
        return self._front, self._back, self._css

    def _store_active(self) -> None:
        text = self.editor.toPlainText()
        if self._active == 0:
            self._front = text
        elif self._active == 1:
            self._back = text
        else:
            self._css = text

    def _show_active(self) -> None:
        self._loading = True
        text = (self._front, self._back, self._css)[self._active]
        self.editor.setPlainText(text)
        self._loading = False

    def _switch_to(self, index: int) -> None:
        self._store_active()
        self._active = index
        self._show_active()

    def _on_text_changed(self) -> None:
        if self._loading:
            return
        self._store_active()
        self.changed.emit()


class MainScreen(QDialog):
    def __init__(self, parent: Any = None):
        super().__init__(parent or mw)
        self.setWindowTitle("Deck Direction Converter")
        screen = self.screen() or mw.screen()
        available = screen.availableGeometry() if screen else None
        width = available.width() if available is not None else 1180
        self.resize(width, 760)
        if available is not None:
            self.move(available.x(), self.y())
        self.setMinimumSize(980, 620)

        self._pairs: List[Tuple[str, str, int]] = []
        self._deck_name = ""
        self._notetype_name = ""
        self._notetype: Optional[dict] = None
        self._live_fields: List[Tuple[int, str]] = []
        self._samples: Dict[str, List[str]] = {}
        self._note_ids: List[int] = []
        self._html_mode = False
        self._html_dirty = False

        layout = QVBoxLayout(self)

        # -- 1. deck/mode picker --------------------------------------------------
        picker_row = QHBoxLayout()
        picker_row.addWidget(QLabel("Deck:"))
        self.pair_box = QComboBox()
        picker_row.addWidget(self.pair_box, stretch=1)
        picker_row.addWidget(QLabel("Mode:"))
        self.mode_box = QComboBox()
        self.mode_box.addItem("New deck (recommended)", ConversionMode.NEW_DECK)
        self.mode_box.addItem("Flip in place", ConversionMode.FLIP_IN_PLACE)
        picker_row.addWidget(self.mode_box)
        layout.addLayout(picker_row)

        # -- 2. language banner -----------------------------------------------------
        lang_row = QHBoxLayout()
        self.detected_label = QLabel("")
        self.detected_label.setWordWrap(True)
        lang_row.addWidget(self.detected_label, stretch=1)
        self.manual_check = QCheckBox("Set manually")
        self.manual_check.stateChanged.connect(self._on_manual_toggled)
        lang_row.addWidget(self.manual_check)
        self.target_language = QLineEdit()
        self.target_language.setPlaceholderText("Target (front)")
        self.target_language.setVisible(False)
        self.target_language.setMaximumWidth(120)
        self.target_language.textChanged.connect(self._on_field_or_language_changed)
        lang_row.addWidget(self.target_language)
        self.native_language = QLineEdit()
        self.native_language.setPlaceholderText("Native (back)")
        self.native_language.setVisible(False)
        self.native_language.setMaximumWidth(120)
        self.native_language.textChanged.connect(self._on_field_or_language_changed)
        lang_row.addWidget(self.native_language)
        layout.addLayout(lang_row)

        # -- 3. left/right split ------------------------------------------------------
        splitter = QSplitter(Qt.Orientation.Horizontal)

        fields_pane = QWidget()
        fields_layout = QVBoxLayout(fields_pane)
        fields_layout.setContentsMargins(0, 0, 0, 0)

        mode_row = QHBoxLayout()
        self.fields_button = QPushButton("Fields")
        self.fields_button.setCheckable(True)
        self.fields_button.setChecked(True)
        self.html_button = QPushButton("HTML")
        self.html_button.setCheckable(True)
        self._mode_group = QButtonGroup(self)
        self._mode_group.addButton(self.fields_button)
        self._mode_group.addButton(self.html_button)
        self.fields_button.clicked.connect(self._on_fields_mode_clicked)
        self.html_button.clicked.connect(self._on_html_mode_clicked)
        mode_row.addWidget(self.fields_button)
        mode_row.addWidget(self.html_button)
        mode_row.addStretch(1)
        fields_layout.addLayout(mode_row)

        self.stack = QStackedWidget()
        self.field_list = FieldListWidget()
        self.field_list.changed.connect(self._on_field_or_language_changed)
        self.stack.addWidget(self.field_list)

        self.html_editor = _RawHtmlEditor()
        self.html_editor.changed.connect(self._on_html_edited)
        self.stack.addWidget(self.html_editor)

        fields_layout.addWidget(self.stack, stretch=1)
        splitter.addWidget(fields_pane)

        self.preview = PreviewPanel()
        splitter.addWidget(self.preview)

        splitter.setSizes([1, 1])
        layout.addWidget(splitter, stretch=1)

        # -- 4. voice section --------------------------------------------------------
        voice_row = QHBoxLayout()
        voice_row.addWidget(QLabel("Voice:"))
        self.voice_box = QComboBox()
        for spec in CURATED_VOICES:
            self.voice_box.addItem(spec.display_name, spec.voice_id)
        default_voice = addon_config().get("tts_voice") or _DEFAULT_VOICE_ID
        index = self.voice_box.findData(default_voice)
        if index != -1:
            self.voice_box.setCurrentIndex(index)
        voice_row.addWidget(self.voice_box, stretch=1)
        self.sample_button = QPushButton("Sample")
        self.sample_button.clicked.connect(self._on_sample_voice)
        voice_row.addWidget(self.sample_button)
        layout.addLayout(voice_row)

        # -- TTS batch options (used only by "Generate TTS audio" below) ------------
        tts_options_row = QHBoxLayout()
        tts_options_row.addWidget(QLabel("Notes per run:"))
        self.tts_limit_all_radio = QRadioButton("All")
        self.tts_limit_chunk_radio = QRadioButton("%d at a time" % _BATCH_CHUNK)
        self._tts_chunk_tooltip = (
            "Stop after %d notes instead of doing the whole deck in one run. The rest are "
            "simply left for next time -- a re-run only ever processes notes that don't "
            "already have audio." % _BATCH_CHUNK
        )
        self.tts_limit_chunk_radio.setToolTip(self._tts_chunk_tooltip)
        self.tts_limit_all_radio.setChecked(True)
        self._tts_limit_group = QButtonGroup(self)
        self._tts_limit_group.addButton(self.tts_limit_all_radio)
        self._tts_limit_group.addButton(self.tts_limit_chunk_radio)
        tts_options_row.addWidget(self.tts_limit_all_radio)
        tts_options_row.addWidget(self.tts_limit_chunk_radio)
        tts_options_row.addSpacing(16)
        self.tts_parallel_check = QCheckBox("multiple at once (faster, uses more CPU)")
        self.tts_parallel_check.setToolTip(
            "Runs several Piper syntheses at the same time instead of one after another. "
            "Faster on most machines, but leave this off on a low-end or already-busy "
            "computer."
        )
        tts_options_row.addWidget(self.tts_parallel_check)
        tts_options_row.addStretch(1)
        layout.addLayout(tts_options_row)

        self.status_label = QLabel("")
        self.status_label.setWordWrap(True)
        layout.addWidget(self.status_label)

        # -- 5. actions ---------------------------------------------------------------
        action_row = QHBoxLayout()
        self.save_profile_button = QPushButton("Save as profile…")
        self.save_profile_button.clicked.connect(self._on_save_profile)
        action_row.addWidget(self.save_profile_button)
        action_row.addStretch(1)
        self.convert_button = QPushButton("Convert")
        self.convert_button.clicked.connect(self._on_convert)
        action_row.addWidget(self.convert_button)
        self.generate_button = QPushButton("Generate TTS audio")
        self.generate_button.clicked.connect(self._on_generate_tts)
        action_row.addWidget(self.generate_button)
        layout.addLayout(action_row)

        close_box = QDialogButtonBox(QDialogButtonBox.StandardButton.Close)
        close_box.rejected.connect(self.reject)
        layout.addWidget(close_box)

        self.pair_box.currentIndexChanged.connect(self._on_pair_changed)
        self._refresh_pairs()

    # -- deck/notetype selection --------------------------------------------------

    def _refresh_pairs(self) -> None:
        self._pairs = _decks_with_notetypes(mw.col)
        self.pair_box.blockSignals(True)
        self.pair_box.clear()
        for deck, notetype, count in self._pairs:
            self.pair_box.addItem("%s  —  %s  (%d notes)" % (deck, notetype, count))
        self.pair_box.blockSignals(False)
        if self._pairs:
            self._on_pair_changed(self.pair_box.currentIndex())

    def _select_pair(self, deck_name: str, notetype_name: str) -> None:
        for i, (d, n, _count) in enumerate(self._pairs):
            if d == deck_name and n == notetype_name:
                self.pair_box.setCurrentIndex(i)
                return

    def _on_pair_changed(self, index: int) -> None:
        if index < 0 or index >= len(self._pairs):
            return
        deck, notetype_name, _count = self._pairs[index]
        self._deck_name = deck
        self._notetype_name = notetype_name
        self._notetype = mw.col.models.by_name(notetype_name)
        self._live_fields = fields_from_notetype(self._notetype["flds"])

        self._note_ids = mw.col.find_notes(scope_query(notetype_name, deck))
        self._samples = _collect_samples(self._note_ids[:3])

        match = match_profile(notetype_name, self._live_fields)
        if match.usable:
            mapping = match.profile.to_mapping(live_fields=self._live_fields)
        else:
            mapping = RoleMapping(
                notetype_name=notetype_name,
                fields=[FieldBinding(name=n, ord=o) for o, n in self._live_fields],
            )

        self.target_language.blockSignals(True)
        self.native_language.blockSignals(True)
        self.target_language.setText(mapping.target_language or "")
        self.native_language.setText(mapping.native_language or "")
        self.target_language.blockSignals(False)
        self.native_language.blockSignals(False)

        self.field_list.set_content(
            self._live_fields, self._samples, mapping, order=mapping.render_order
        )

        self._html_mode = False
        self._html_dirty = False
        self.fields_button.setChecked(True)
        self.stack.setCurrentWidget(self.field_list)

        self._update_language_banner()
        self._update_tts_limit_options()
        self._refresh_preview()

    def _update_tts_limit_options(self) -> None:
        if self._notetype is None:
            return
        pending = len(notes_needing_audio(mw.col, self._notetype_name, force=False))
        fits_in_one_chunk = pending <= _BATCH_CHUNK
        self.tts_limit_chunk_radio.setEnabled(not fits_in_one_chunk)
        if fits_in_one_chunk:
            self.tts_limit_chunk_radio.setToolTip(
                "Only %d note%s pending -- same as \"All\" right now."
                % (pending, "" if pending == 1 else "s")
            )
            self.tts_limit_all_radio.setChecked(True)
        else:
            self.tts_limit_chunk_radio.setToolTip(self._tts_chunk_tooltip)

    # -- language banner ------------------------------------------------------------

    def _on_manual_toggled(self) -> None:
        manual = self.manual_check.isChecked()
        self.target_language.setVisible(manual)
        self.native_language.setVisible(manual)
        self._update_language_banner()

    def _update_language_banner(self) -> None:
        counts: Dict[str, int] = {}
        for _ord, name in self._live_fields:
            guess = detect_field_language(self._samples.get(name, []))
            if guess.is_confident:
                counts[guess.code] = counts.get(guess.code, 0) + 1
        detected = (
            ", ".join("%s (%d)" % (c, n) for c, n in sorted(counts.items(), key=lambda kv: -kv[1]))
            or "no confident guess"
        )

        target = self.target_language.text().strip()
        native = self.native_language.text().strip()
        if target or native:
            direction = "%s → %s" % (native or "?", target or "?")
        else:
            direction = 'not set -- check "Set manually", or map roles and save a profile'

        self.detected_label.setText("Detected: %s    Direction: %s" % (detected, direction))

    # -- mapping / templates --------------------------------------------------------

    def _current_mapping(self) -> RoleMapping:
        assignments = self.field_list.current_assignments()
        target = self.target_language.text().strip() or None
        native = self.native_language.text().strip() or None
        mapping = mapping_from_assignments(
            self._notetype_name, assignments, target_language=target, native_language=native
        )
        mapping.render_order = self.field_list.current_render_order() or None
        return mapping

    def _current_templates(self) -> GeneratedTemplates:
        if self._html_mode:
            front, back, css = self.html_editor.get_content()
            return GeneratedTemplates(
                front_html=front,
                back_html=back,
                css=css,
                template_name=addon_config().get("template_name", "Production"),
            )
        mapping = self._current_mapping()
        opts = template_options_from_config(addon_config())
        order = self.field_list.current_render_order()
        front_order, back_order = split_render_order(order, audio_on_front=opts.audio_on_front)
        opts = replace(opts, front_order=front_order, back_order=back_order)
        return generate_templates(mapping, source_css=self._notetype.get("css", ""), options=opts)

    # -- field list / language edits -> live preview --------------------------------

    def _on_field_or_language_changed(self) -> None:
        self._update_language_banner()
        self._refresh_preview()

    def _refresh_preview(self) -> None:
        if not self._note_ids or self._notetype is None:
            return
        try:
            templates = self._current_templates()
        except ValidationError as exc:
            self.status_label.setText("Cannot preview yet: %s" % exc)
            return
        self.status_label.setText("")
        note = mw.col.get_note(self._note_ids[0])
        self.preview.set_content(
            note,
            self._notetype,
            0,
            front_html=templates.front_html,
            back_html=templates.back_html,
            css=templates.css,
        )
        self.preview.refresh()

    # -- HTML / Fields mode switch ----------------------------------------------------

    def _on_html_mode_clicked(self) -> None:
        if self._html_mode:
            return
        try:
            templates = self._current_templates()
        except ValidationError:
            templates = None
        if templates is not None:
            self.html_editor.set_content(templates.front_html, templates.back_html, templates.css)
        self._html_mode = True
        self._html_dirty = False
        self.stack.setCurrentWidget(self.html_editor)

    def _on_fields_mode_clicked(self) -> None:
        if not self._html_mode:
            return
        if self._html_dirty:
            if not askUser(
                "Switching back to Fields mode will discard your manual HTML edits and "
                "regenerate the card from the field list. Continue?",
                parent=self,
                defaultno=True,
            ):
                self.html_button.setChecked(True)
                return
        self._html_mode = False
        self._html_dirty = False
        self.stack.setCurrentWidget(self.field_list)
        self._refresh_preview()

    def _on_html_edited(self) -> None:
        self._html_dirty = True
        self._refresh_preview()

    # -- voice sampling (no collection write -- QueryOp, not CollectionOp) ----------

    def _sample_text(self) -> str:
        """The text a real batch run would actually speak for the currently-previewed
        note, so "Sample" previews this deck's real content instead of a generic phrase.
        Prefers TargetSentence (closer to real speech than a bare word) and only falls
        back to TargetTerm when there's no sentence -- e.g. a word-only note -- or no
        sentence text on this particular note. Falls back to ``_SAMPLE_TEXT`` only if
        neither role is mapped or has content yet, or a note isn't even selected."""
        if self._note_ids and self._notetype is not None:
            try:
                mapping = self._current_mapping()
            except ValidationError:
                mapping = None
            if mapping is not None:
                note = mw.col.get_note(self._note_ids[0])
                for role in (Role.TARGET_SENTENCE, Role.TARGET_TERM):
                    binding = mapping.first(role)
                    if binding is None:
                        continue
                    try:
                        text = note[binding.name]
                    except (KeyError, IndexError):
                        continue
                    if text.strip():
                        return text
        return _SAMPLE_TEXT

    def _on_sample_voice(self) -> None:
        voice_id = self.voice_box.currentData()
        text = self._sample_text()
        self.sample_button.setEnabled(False)
        self.status_label.setText(
            "Synthesizing… (first use downloads the Piper binary and this voice; later "
            "uses are cached and near-instant)"
        )

        def op(_col: Any) -> Path:
            provider = PiperProvider(_cache_dir())
            return provider.synthesize(text, voice_id=voice_id)

        def on_success(wav_path: Path) -> None:
            self.sample_button.setEnabled(True)
            self.status_label.setText("")
            av_player.play_file(str(wav_path))

        def on_failure(exc: Exception) -> None:
            self.sample_button.setEnabled(True)
            self.status_label.setText("")
            showWarning(str(exc), parent=self)

        QueryOp(parent=self, op=op, success=on_success).failure(on_failure).run_in_background()

    # -- save as profile --------------------------------------------------------------

    def _on_save_profile(self) -> None:
        mapping = self._current_mapping()
        if not mapping.validate().ok:
            showWarning("Fix the mapping errors before saving it as a profile.", parent=self)
            return

        name, ok = QInputDialog.getText(
            self, "Save as profile", "Profile name:", QLineEdit.EchoMode.Normal, self._notetype_name
        )
        if not ok or not name.strip():
            return
        name = name.strip()
        profile_id = slugify(name)

        existing = {p.id for p in load_profiles([USER_PROFILE_DIR])}
        if profile_id in existing:
            if not askUser(
                "A saved profile named %r already exists. Overwrite it?" % name,
                parent=self,
                defaultno=True,
            ):
                return

        path = save_profile(mapping, id=profile_id, title=name)
        showInfo("Saved profile to:\n\n%s" % path, parent=self)

    # -- convert ------------------------------------------------------------------------

    def _on_convert(self) -> None:
        if not self._note_ids:
            showWarning("No notes in the selected deck/notetype.", parent=self)
            return
        try:
            templates = self._current_templates()
        except ValidationError as exc:
            showWarning("Cannot convert: %s" % exc, parent=self)
            return

        mapping = self._current_mapping()
        config = addon_config()
        plan = build_plan(
            mode=self.mode_box.currentData(),
            mapping=mapping,
            source_notetype=self._notetype_name,
            source_deck=self._deck_name,
            suffix=config.get("name_suffix", "Converted"),
            dry_run=False,
        )
        plan.strip_tags = list(config.get("strip_tags", ["leech"]))
        plan.note_ids = list(mw.col.find_notes(plan.scope_query))

        validation = plan.validate()
        if not validation.ok:
            showWarning("Cannot convert:\n\n" + "\n".join(validation.errors), parent=self)
            return

        self.convert_button.setEnabled(False)

        def done(result: Any) -> None:
            self.convert_button.setEnabled(True)
            if result is None:
                return
            showInfo("\n".join(result.messages), parent=self)
            deck_name = mw.col.decks.name(result.target_deck_id)
            self._refresh_pairs()
            self._select_pair(deck_name, result.clone_notetype_name)

        convert_op(
            self, plan, templates, expected_note_count=len(plan.note_ids), on_success=done
        ).run_in_background()

    # -- generate TTS audio -------------------------------------------------------------

    def _on_generate_tts(self) -> None:
        if self._notetype is None:
            return
        match = match_profile(self._notetype_name, self._live_fields)
        mapping = (
            match.profile.to_mapping(live_fields=self._live_fields)
            if match.usable
            else self._current_mapping()
        )
        if not mapping.validate().ok:
            showWarning("Map the fields (or fix the errors shown) before generating audio.", parent=self)
            return

        voice_id = self.voice_box.currentData()
        note_ids = notes_needing_audio(mw.col, self._notetype_name, force=False)
        if not note_ids:
            showInfo(
                "Every note already has generated audio for this notetype.", parent=self
            )
            return

        limit = _BATCH_CHUNK if self.tts_limit_chunk_radio.isChecked() else 0
        run_count = min(limit, len(note_ids)) if limit else len(note_ids)

        config = addon_config()
        if config.get("prompt_for_backup", True):
            proceed = askUser(
                "About to generate audio for %d notes%s. This can take a while and writes "
                "real audio into your notes.\n\nMake sure you have a backup.\n\nContinue?"
                % (
                    run_count,
                    "" if run_count == len(note_ids) else " (of %d pending)" % len(note_ids),
                ),
                parent=self,
                defaultno=False,
            )
            if not proceed:
                return

        self._run_tts_batch(self._notetype, mapping, note_ids, voice_id, config, limit)

    def _run_tts_batch(
        self,
        notetype: dict,
        mapping: RoleMapping,
        note_ids: List[int],
        voice_id: str,
        config: dict,
        limit: int,
    ) -> None:
        self.generate_button.setEnabled(False)
        self.status_label.setText("Starting…")
        concurrency = default_concurrency() if self.tts_parallel_check.isChecked() else 1

        def on_done(outcome: BatchOutcome) -> None:
            self.generate_button.setEnabled(True)
            if outcome.error is not None:
                showWarning("TTS batch failed: %s" % outcome.error, parent=self)
                return
            remaining_note = (
                " %d notes still remain -- run this again to continue." % outcome.remaining
                if outcome.remaining
                else ""
            )
            showInfo(
                "Done. %d notes generated, %d failed (left for the next run).%s"
                % (outcome.done, outcome.failed, remaining_note),
                parent=self,
            )
            self._on_pair_changed(self.pair_box.currentIndex())

        run_tts_batch(
            self,
            notetype,
            mapping,
            note_ids,
            voice_id,
            config,
            template_options_from_config=template_options_from_config,
            limit=limit,
            concurrency=concurrency,
            on_done=on_done,
        )


def show_main_screen() -> None:
    if mw.col is None:
        showWarning("Open a collection first.")
        return
    MainScreen(mw).exec()
