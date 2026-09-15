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
language text; then Convert / Generate TTS audio.
"""

from __future__ import annotations

import threading
from dataclasses import replace
from pathlib import Path
from typing import Any, Dict, List, Optional, Set, Tuple

from aqt import mw
from aqt.operations import QueryOp
from aqt.qt import (
    QButtonGroup,
    QCheckBox,
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QHBoxLayout,
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

from ..core.audio_fields import (
    plan_generated_fields,
    resolve_audio_fields,
    sound_field_names,
)
from ..core.conversion import ConversionMode, build_plan, scope_query
from ..core.language_detect import detect_field_language
from ..core.role_detect import guess_role_mapping
from ..core.role_schema import (
    Role,
    RoleMapping,
    ValidationError,
    fields_from_notetype,
    mapping_from_assignments,
)
from ..core.template_generator import (
    GeneratedTemplates,
    generate_templates,
    referenced_fields,
    split_render_order,
)
from ..ops.convert_op import convert_op
from ..ops.tts_batch import notes_needing_audio
from ..ops.tts_runner import BatchOutcome, default_concurrency, run_tts_batch
from ..tts.piper_provider import PiperProvider
from ..tts.piper_voice_manager import CURATED_VOICES
from .convert_dialog import (
    _collect_raw_samples,
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
        #: Names of fields whose real content holds ``[sound:...]``, refreshed per pair.
        #: Feeds core.audio_fields so audio the deck already had is bound to a native role
        #: (kept, but never rendered) instead of falling through to the back of the card.
        self._sound_fields: Set[str] = set()
        self._note_ids: List[int] = []
        self._html_mode = False
        self._html_dirty = False
        self._target_deck_dirty = False
        self._tts_cancel_event: Optional[threading.Event] = None
        self._convert_running = False
        #: (clone_notetype_name, mapping) set right after a successful Convert, so the very
        #: next pair-change (auto-selecting that clone) can carry the exact role mapping
        #: forward rather than re-running content detection against it -- cloning preserves
        #: field names 1:1, so the same mapping is valid on the clone, and re-detecting could
        #: land on something else entirely (the clone's own generated CSS marker changes what
        #: "converted" detection sees). Without this, _direction_is_correct could (wrongly)
        #: report "not converted yet" immediately after a Convert that just succeeded.
        #: Consumed once, whether or not it ends up matching -- see _on_pair_changed.
        self._converted_mapping_override: Optional[Tuple[str, RoleMapping]] = None

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
        self.mode_box.currentIndexChanged.connect(lambda *_args: self._update_target_deck_default())

        rename_row = QHBoxLayout()
        rename_row.addWidget(QLabel("New deck name:"))
        self.target_deck_edit = QLineEdit()
        self.target_deck_edit.textEdited.connect(self._on_target_deck_edited)
        rename_row.addWidget(self.target_deck_edit, stretch=1)
        layout.addLayout(rename_row)

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
        # Two separate steps, on purpose: Convert switches the deck's direction; Generate
        # TTS audio adds audio for whichever language a card currently shows first. Audio
        # only ever makes sense to generate *after* the direction is right, so "Generate
        # TTS audio" stays disabled (see _update_action_state/_direction_is_correct) until
        # the currently selected notetype's own front template already shows the target
        # language -- checked against the notetype, not just remembered from a click, so a
        # deck that was already in the right direction to begin with is never blocked.
        self.flow_hint_label = QLabel("")
        self.flow_hint_label.setWordWrap(True)
        layout.addWidget(self.flow_hint_label)

        action_row = QHBoxLayout()
        action_row.addStretch(1)
        self.stop_button = QPushButton("Stop")
        self.stop_button.setVisible(False)
        self.stop_button.clicked.connect(self._on_stop_clicked)
        action_row.addWidget(self.stop_button)
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

    def _refresh_anki_main_window(self) -> None:
        """Refresh Anki's own deck browser so a new/changed deck shows up without the
        user having to manually refresh it themselves.

        Defensive rather than a hard dependency: ``aqt.deckbrowser.DeckBrowser.refresh``
        is a long-standing, stable widget method (it just re-reads deck stats and
        redraws), safe to call even when the deck browser isn't the screen currently
        shown -- but per ``claude.md``'s "never guess an Anki API" rule, this is wrapped
        so a future Anki build that removed or renamed it degrades to a no-op instead of
        crashing the (already-successful) conversion.
        """
        deck_browser = getattr(mw, "deckBrowser", None)
        refresh = getattr(deck_browser, "refresh", None)
        if callable(refresh):
            try:
                refresh()
            except Exception:  # noqa: BLE001 -- best-effort UI refresh, never fatal
                pass

    def closeEvent(self, event: Any) -> None:  # noqa: N802 -- Qt override signature
        # Safety net: if something changed during this session that the deck browser
        # never picked up live (see _refresh_anki_main_window), make sure it's caught up
        # by the time the user closes this screen and looks at Anki again.
        self._refresh_anki_main_window()
        super().closeEvent(event)

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

        # Which fields actually hold audio, for every branch below -- not just the detected
        # one. It's what lets audio the deck already had be bound to a native role and so
        # kept off the card, whether the mapping came from a carry-forward or a fresh guess.
        # See core.audio_fields.
        raw_samples = _collect_raw_samples(self._note_ids[:8])
        self._sound_fields = sound_field_names(raw_samples)

        if (
            self._converted_mapping_override is not None
            and self._converted_mapping_override[0] == notetype_name
        ):
            # The pair a Convert just finished on -- see the field's own docstring.
            mapping = self._converted_mapping_override[1]
            self._converted_mapping_override = None
        else:
            tmpls = self._notetype.get("tmpls") or [{}]
            mapping = guess_role_mapping(
                notetype_name,
                self._live_fields,
                raw_samples,
                front_html=tmpls[0].get("qfmt", ""),
                back_html=tmpls[0].get("afmt", ""),
                css=self._notetype.get("css", ""),
            )
        mapping = resolve_audio_fields(mapping, sound_fields=self._sound_fields)

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

        self._target_deck_dirty = False
        self._update_target_deck_default()

        self._update_language_banner()
        self._update_tts_limit_options()
        self._update_action_state()
        self._refresh_preview()

    def _on_target_deck_edited(self, _text: str) -> None:
        self._target_deck_dirty = True

    def _update_target_deck_default(self) -> None:
        """Refills "New deck name" with a sensible default -- unless the user has typed
        their own, which stays put until the deck/notetype pair itself changes. Flip in
        place never creates a separate deck (notes stay put; only the notetype changes),
        so the field is locked to the source deck's own name and disabled while that mode
        is selected."""
        if not self._deck_name:
            return
        if self.mode_box.currentData() is ConversionMode.FLIP_IN_PLACE:
            self.target_deck_edit.setText(self._deck_name)
            self.target_deck_edit.setEnabled(False)
            self.target_deck_edit.setToolTip(
                "Flip in place keeps notes in their original deck -- there's no new deck "
                "to name."
            )
            return
        self.target_deck_edit.setEnabled(True)
        self.target_deck_edit.setToolTip("")
        if not self._target_deck_dirty:
            suffix = addon_config().get("name_suffix", "Converted")
            self.target_deck_edit.setText("%s (%s)" % (self._deck_name, suffix))

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

    # -- Convert-before-Audio gate ----------------------------------------------------

    def _direction_is_correct(self) -> bool:
        """Whether the currently selected notetype's real, on-disk front template already
        shows target-language content -- i.e. whether Convert has already been run against
        it (or it was already in the right direction to begin with).

        Checked against the *live* ``qfmt`` (``self._notetype``), not against what
        ``_current_templates()`` would freshly generate -- that's always computable from
        the mapping alone, converted or not, so it can't tell the two apart. This can.
        """
        if self._notetype is None or not self._notetype.get("tmpls"):
            return False
        try:
            mapping = self._current_mapping()
        except ValidationError:
            return False
        live_front_fields = referenced_fields(self._notetype["tmpls"][0].get("qfmt", ""))
        for role in (Role.TARGET_TERM, Role.TARGET_SENTENCE):
            binding = mapping.first(role)
            if binding is not None and binding.name in live_front_fields:
                return True
        return False

    def _is_busy(self) -> bool:
        return self._convert_running or self._tts_cancel_event is not None

    def _update_action_state(self) -> None:
        busy = self._is_busy()
        correct = self._direction_is_correct()
        self.convert_button.setEnabled(not busy)
        self.generate_button.setEnabled(correct and not busy)
        if correct:
            self.generate_button.setToolTip("")
            self.flow_hint_label.setText(
                "This deck's direction is already switched -- Generate TTS audio is ready. "
                "It writes real audio in the background; you can click Stop at any time "
                "and continue right where you left off later."
            )
        else:
            self.generate_button.setToolTip(
                "Convert this deck's direction first -- Generate TTS audio only works once "
                "the target language is already on the front of the card."
            )
            self.flow_hint_label.setText(
                "Step 1: click Convert to switch this deck's direction. Step 2: once "
                "switched, come back here and click Generate TTS audio -- it writes real "
                "audio in the background, and you can click Stop at any time and continue "
                "right where you left off later."
            )

    # -- language banner ------------------------------------------------------------

    def _on_manual_toggled(self) -> None:
        manual = self.manual_check.isChecked()
        self.target_language.setVisible(manual)
        self.native_language.setVisible(manual)
        self._update_language_banner()

    def _majority_language(self, field_names: set) -> Optional[str]:
        """The most common confidently-detected language among ``field_names``, or
        ``None`` if none of them produced a confident guess."""
        counts: Dict[str, int] = {}
        for _ord, name in self._live_fields:
            if name not in field_names:
                continue
            guess = detect_field_language(self._samples.get(name, []))
            if guess.is_confident:
                counts[guess.code] = counts.get(guess.code, 0) + 1
        return max(counts.items(), key=lambda kv: kv[1])[0] if counts else None

    def _structural_direction(self) -> Optional[str]:
        """What's actually on the card *right now*, read straight off the live qfmt/afmt
        -- an objective fact about the deck as it exists today, independent of any role
        mapping (which describes what *will* happen once Convert runs, not what already
        has). ``None`` if there's no notetype yet or neither side yields a confident guess.
        """
        if self._notetype is None or not self._notetype.get("tmpls"):
            return None
        tmpl = self._notetype["tmpls"][0]
        front_fields = set(referenced_fields(tmpl.get("qfmt", "")))
        back_fields = set(referenced_fields(tmpl.get("afmt", ""))) - front_fields
        front_lang = self._majority_language(front_fields)
        back_lang = self._majority_language(back_fields)
        if not front_lang and not back_lang:
            return None
        return "%s → %s" % (front_lang or "?", back_lang or "?")

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
        current = self._structural_direction()

        if self._direction_is_correct():
            # Front already shows target -- "current" and "after converting" are the same
            # thing, so there's nothing to contrast. Prefer the structural reading; fall
            # back to the mapping's own target/native strings if detection came up empty.
            direction = "%s (already converted)" % (
                current or "%s → %s" % (target or "?", native or "?")
            )
        elif target or native:
            after = "%s → %s" % (target or "?", native or "?")
            direction = (
                "Current: %s   |   after converting: %s" % (current, after)
                if current
                else "after converting: %s" % after
            )
        elif current:
            direction = "Current: %s   -- map roles (or set manually) to see the direction after converting" % current
        else:
            direction = 'not set -- check "Set manually", or map roles yourself'

        self.detected_label.setText("Detected: %s    Direction: %s" % (detected, direction))

    # -- mapping / templates --------------------------------------------------------

    def _current_mapping(self) -> RoleMapping:
        """What the field list currently says, with the audio policy applied.

        Resolving here rather than at each use means every consumer -- preview, direction
        check, sample text, Convert -- sees the same thing: generated
        fields hold the target audio, and any audio the deck already had is bound to a
        native role so it stays on the note but never reaches a card.
        """
        assignments = self.field_list.current_assignments()
        target = self.target_language.text().strip() or None
        native = self.native_language.text().strip() or None
        mapping = mapping_from_assignments(
            self._notetype_name, assignments, target_language=target, native_language=native
        )
        mapping.render_order = self.field_list.current_render_order() or None
        return resolve_audio_fields(mapping, sound_fields=self._sound_fields)

    def _conversion_mapping(self) -> Tuple[RoleMapping, List[Tuple[Role, str]]]:
        """``(mapping, [(role, new field name), ...])`` for an actual conversion.

        Differs from :meth:`_current_mapping` in one way: it also accounts for the audio
        fields the conversion is about to create, so the templates it generates can already
        reference them. They don't exist on the notetype being looked at -- only on the
        clone, once ``apply_plan`` has built it.
        """
        mapping = self._current_mapping()
        planned = plan_generated_fields(mapping)
        if not planned:
            return mapping, []
        resolved = resolve_audio_fields(
            mapping, sound_fields=self._sound_fields, extra_fields=planned
        )
        return resolved, planned

    def _current_templates(self, mapping: Optional[RoleMapping] = None) -> GeneratedTemplates:
        if self._html_mode:
            front, back, css = self.html_editor.get_content()
            return GeneratedTemplates(
                front_html=front,
                back_html=back,
                css=css,
                template_name=addon_config().get("template_name", "Production"),
            )
        if mapping is None:
            mapping = self._current_mapping()
        opts = template_options_from_config(addon_config())
        order = self.field_list.current_render_order()
        front_order, back_order = split_render_order(order, audio_on_front=opts.audio_on_front)
        opts = replace(opts, front_order=front_order, back_order=back_order)
        return generate_templates(mapping, source_css=self._notetype.get("css", ""), options=opts)

    # -- field list / language edits -> live preview --------------------------------

    def _on_field_or_language_changed(self) -> None:
        self._update_language_banner()
        self._update_action_state()
        self._refresh_preview()

    def _refresh_preview(self) -> None:
        if not self._note_ids or self._notetype is None:
            self.preview.clear()
            return
        try:
            templates = self._current_templates()
        except ValidationError as exc:
            self.status_label.setText("Cannot preview yet: %s" % exc)
            self.preview.clear("Map the fields (or fix the errors above) to see a preview.")
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
                    except (KeyError, IndexError, ValueError):
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

    # -- convert ------------------------------------------------------------------------

    def _on_convert(self) -> None:
        if not self._note_ids:
            showWarning("No notes in the selected deck/notetype.", parent=self)
            return
        try:
            # The conversion's own mapping, which knows about the audio fields it is about
            # to create -- not self._current_mapping(), which describes the notetype as it
            # stands right now.
            mapping, planned_fields = self._conversion_mapping()
            templates = self._current_templates(mapping)
        except ValidationError as exc:
            showWarning("Cannot convert: %s" % exc, parent=self)
            return

        config = addon_config()
        mode = self.mode_box.currentData()
        source_deck_at_start = self._deck_name
        plan = build_plan(
            mode=mode,
            mapping=mapping,
            source_notetype=self._notetype_name,
            source_deck=self._deck_name,
            target_deck=self.target_deck_edit.text().strip() or None,
            suffix=config.get("name_suffix", "Converted"),
            dry_run=False,
        )
        plan.strip_tags = list(config.get("strip_tags", ["leech"]))
        plan.new_fields = [name for _role, name in planned_fields]
        plan.note_ids = list(mw.col.find_notes(plan.scope_query))

        validation = plan.validate()
        if not validation.ok:
            showWarning("Cannot convert:\n\n" + "\n".join(validation.errors), parent=self)
            return

        self._convert_running = True
        self._update_action_state()

        def done(result: Any) -> None:
            self._convert_running = False
            if result is None:
                self._update_action_state()
                return
            showInfo("\n".join(result.messages), parent=self)
            # Flip in place never creates or moves to a new deck -- apply_plan leaves
            # result.target_deck_id at 0 for that mode, and mw.col.decks.name(0) is not a
            # real deck, so it must never be consulted here. Read from a variable captured
            # *before* the (async) conversion ran, not self._deck_name, in case the pair
            # dropdown got changed while the conversion was still in flight.
            deck_name = (
                mw.col.decks.name(result.target_deck_id)
                if mode is ConversionMode.NEW_DECK
                else source_deck_at_start
            )
            # The clone keeps every source field at its own name and ord and only appends
            # the generated audio field(s) -- which this mapping already describes, since
            # it's the one the conversion ran with. Role bindings don't otherwise change
            # through a conversion (only which side the template puts them on does), so it
            # is already correct for the clone. Without this, the new pair would land on a
            # freshly re-detected mapping and Generate TTS audio could look "not converted
            # yet" even though it just was.
            self._converted_mapping_override = (result.clone_notetype_name, mapping)
            self._refresh_pairs()
            # _select_pair (via _on_pair_changed) already calls _update_action_state, and
            # the notetype it now points at is the freshly-converted clone -- Generate TTS
            # audio unlocks immediately, no separate re-check needed.
            self._select_pair(deck_name, result.clone_notetype_name)
            # CollectionOp's own mw.col.op_made_changes(changes)-driven refresh doesn't
            # reliably pick up the new deck here (see docs/api-notes.md) -- the deck
            # creation and note writes happened before the undo marker CollectionOp's
            # changes object actually describes, so Anki's own deck browser can be left
            # showing stale state even though the addon's own dropdown updates fine.
            # Refresh it explicitly rather than rely on that.
            self._refresh_anki_main_window()

        convert_op(
            self, plan, templates, expected_note_count=len(plan.note_ids), on_success=done
        ).run_in_background()

    # -- generate TTS audio -------------------------------------------------------------

    def _on_generate_tts(self) -> None:
        if self._notetype is None:
            return
        # Whatever the field list currently shows -- already seeded from a carried-forward
        # override or a fresh content-based guess in _on_pair_changed, and already passed
        # through the audio policy (resolve_audio_fields) in _current_mapping itself.
        mapping = self._current_mapping()
        if not mapping.validate().ok:
            showWarning("Map the fields (or fix the errors shown) before generating audio.", parent=self)
            return

        if mapping.first(Role.TARGET_AUDIO) is None and mapping.first(Role.TARGET_SENTENCE_AUDIO) is None:
            showWarning(
                "This notetype has no field for generated audio yet.\n\nConvert the deck "
                "first -- the conversion creates that field. Generating audio into a field "
                "the deck already had would overwrite the original recordings.",
                parent=self,
            )
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

    def _on_stop_clicked(self) -> None:
        if self._tts_cancel_event is not None:
            self._tts_cancel_event.set()
        self.stop_button.setEnabled(False)
        self.status_label.setText(
            "Stopping after the note currently in progress… audio already generated is "
            "kept, and you can continue right where this leaves off later."
        )

    def _run_tts_batch(
        self,
        notetype: dict,
        mapping: RoleMapping,
        note_ids: List[int],
        voice_id: str,
        config: dict,
        limit: int,
    ) -> None:
        self._tts_cancel_event = threading.Event()
        self._update_action_state()
        self.stop_button.setEnabled(True)
        self.stop_button.setVisible(True)
        self.status_label.setText(
            "Generating audio… click Stop at any time -- audio already generated is kept, "
            "and you can continue right where you left off later."
        )
        concurrency = default_concurrency() if self.tts_parallel_check.isChecked() else 1

        def on_done(outcome: BatchOutcome) -> None:
            self.stop_button.setVisible(False)
            self._tts_cancel_event = None
            if outcome.error is not None:
                self._update_action_state()
                showWarning("TTS batch failed: %s" % outcome.error, parent=self)
                return
            remaining_note = (
                " %d notes still remain -- run this again anytime to continue." % outcome.remaining
                if outcome.remaining
                else ""
            )
            # A run that only ever found "nothing to say" completes fast and reports no
            # failures, which reads as success. Say plainly that no audio came out of it,
            # and name the usual cause -- a voice that speaks a different language than the
            # text it was handed.
            skipped_note = ""
            if outcome.skipped:
                skipped_note = (
                    "\n\n%d note%s had nothing to synthesize -- the source field was empty, "
                    "or its text is in a script the selected voice doesn't speak. Check that "
                    "the voice matches the language now on the front of the card."
                    % (outcome.skipped, "" if outcome.skipped == 1 else "s")
                )
            headline = "Stopped early." if outcome.cancelled else "Done."
            showInfo(
                "%s %d notes generated, %d failed.%s%s"
                % (headline, outcome.done, outcome.failed, remaining_note, skipped_note),
                parent=self,
            )
            # Refreshes the whole pair's state, including _update_action_state, from the
            # live notetype -- covers the "audio done" case the same way as any error path.
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
            cancel_event=self._tts_cancel_event,
            on_done=on_done,
        )


def show_main_screen() -> None:
    if mw.col is None:
        showWarning("Open a collection first.")
        return
    MainScreen(mw).exec()
