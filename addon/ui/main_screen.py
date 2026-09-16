"""The single-screen UI: pick a deck/notetype pair, click Analyze, review the AI's result
(or hand-edit the HTML), click Convert, then Generate TTS audio.

Replaces the old role-mapping flow entirely -- there is no field/role list any more, because
there are no roles: the model (``llm.analyze.analyze_deck``) reads the deck's real content and
produces the finished Front/Back/CSS templates directly, plus a 1-5 star trust rating computed
from how much retrying it took (never something the model reports about itself -- see
``llm/analyze.py``'s docstring). Direction (which language ends up where) is likewise computed
deterministically by ``llm.direction`` inside that same call, never asked of the user up front.

Layout, top to bottom: deck/mode picker; a left/right split (an "AI Result" read-only summary
of the last analysis by default, or a raw Front/Back/Styling editor when "HTML" is switched on,
on the left -- and, on the right, a live preview, ``preview_panel.PreviewPanel``, an embeddable
``AnkiWebView`` that never writes to the collection); a voice picker with a sample button; then
Analyze / Convert / Generate TTS audio, in that order -- each step's output feeds the next.

Whether the *currently selected* pair was already converted by a previous session is read back
via ``core.deck_state.state_from_notetype`` -- a recorded fact (a note tag and a css comment
written at conversion time), never re-derived by inspecting the live templates the way the old
role-mapping system did (which is exactly what broke on a deck this addon had already
converted, reading the same templates backwards a second time -- see ``llm.direction``'s
``known_state`` parameter, which this screen feeds from ``self._conversion_state`` on every
Analyze specifically so a re-Analyze can never flip an already-converted deck back).

Convert always needs a fresh Analyze -- there is no other source for the templates it writes.
Generate TTS audio does not, for a pair already known to be converted: which fields to speak and
which (empty or already-filled) fields their audio goes into is entirely deterministic
(``llm.direction.resolve_direction``'s ``audio_targets`` -- see that module), so this screen just
recomputes it directly (a fast, local, non-AI call) rather than requiring a fresh Analyze --
``self._recomputed_direction``, refreshed on every pair change. Only Convert needs the AI; once a
deck is converted, adding more TTS to it later never does.
"""

from __future__ import annotations

import functools
import threading
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
from ..core.deck_state import ConversionState, state_from_notetype
from ..llm.analyze import DeckAnalysis, analyze_deck, strip_pending_audio_html
from ..llm.client import call_model
from ..llm.direction import Direction, resolve_direction
from ..llm.model_manager import ensure_model, model_is_cached
from ..llm.runtime import ensure_llama_runtime, runtime_is_cached
from ..ops.convert_op import convert_op
from ..ops.deck_data import addon_config, collect_field_samples, decks_with_notetypes
from ..ops.tts_batch import notes_needing_audio
from ..ops.tts_runner import BatchOutcome, default_concurrency, run_tts_batch
from ..tts.piper_provider import PiperProvider
from ..tts.piper_voice_manager import CURATED_VOICES
from .preview_panel import PreviewPanel

__all__ = ["MainScreen", "show_main_screen"]

#: Fallback only -- used before a deck/notetype is picked, or before an analysis exists.
#: Once an analysis is available, the sample button speaks *that note's* own audio-target
#: field content instead (see ``MainScreen._sample_text``).
_SAMPLE_TEXT = "This is a sample sentence for testing."

_DEFAULT_VOICE_ID = "en_GB-alba-medium"

_BATCH_CHUNK = 500

#: Real sample notes handed to the model per analysis. Matches the cap prompt.py itself
#: applies (``_MAX_SAMPLES_PER_FIELD = 5``) with a little headroom -- passing more would only
#: mean reading extra notes for samples the prompt discards anyway.
_ANALYZE_SAMPLE_NOTES = 6


def _cache_dir() -> Path:
    return Path(__file__).resolve().parent.parent / "user_files"


class _RawHtmlEditor(QWidget):
    """Front/Back/Styling tabs over one text editor -- mirrors ``aqt.clayout.CardLayout``'s
    own ``tform`` pattern, rebuilt here as a plain, standalone widget since this screen
    doesn't launch CardLayout itself.

    The single source of truth for the templates a conversion will actually write: populated
    by Analyze's result, and directly editable by hand afterward. There is no separate
    "regenerate from a mapping" data path any more -- whatever this widget holds is what
    Convert uses, unchanged from whichever of those two ways it got there.
    """

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


class _AIResultPanel(QWidget):
    """Read-only display of the model's last analysis for the currently selected pair.

    Never itself editable -- the Front/Back/CSS it describes live in ``_RawHtmlEditor``,
    which the user can hand-edit after Analyze runs. This panel only ever shows *what the AI
    decided and how much to trust it*; the trust rating is computed by
    ``llm.analyze.analyze_deck`` from how much retrying validation took, never something the
    model reports about itself.
    """

    def __init__(self, parent: Any = None):
        super().__init__(parent)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(8, 8, 8, 8)

        self.stars_label = QLabel("")
        big_font = self.stars_label.font()
        big_font.setPointSize(big_font.pointSize() + 3)
        self.stars_label.setFont(big_font)
        layout.addWidget(self.stars_label)

        self.review_label = QLabel("")
        self.review_label.setWordWrap(True)
        self.review_label.setStyleSheet("color: #9a5b00;")
        self.review_label.setVisible(False)
        layout.addWidget(self.review_label)

        self.description_label = QLabel("")
        self.description_label.setWordWrap(True)
        layout.addWidget(self.description_label)

        self.details_label = QLabel("")
        self.details_label.setWordWrap(True)
        layout.addWidget(self.details_label)

        layout.addStretch(1)

    def show_not_analyzed(
        self,
        conversion_state: Optional[ConversionState],
        *,
        can_generate_tts: bool = False,
    ) -> None:
        self.stars_label.setText("")
        self.review_label.setVisible(False)
        if conversion_state is not None:
            self.description_label.setText(
                "This notetype already looks converted: %s → %s (recorded from a "
                "previous conversion)."
                % (conversion_state.target_language, conversion_state.native_language)
            )
            if can_generate_tts:
                self.details_label.setText(
                    "You can click \"Generate TTS audio\" directly -- no AI call needed for "
                    "that any more. Click Analyze only if you want to review or change the "
                    "templates themselves."
                )
            else:
                self.details_label.setText(
                    "This notetype has no real front-side field to generate audio from yet. "
                    "Click Analyze to re-check it."
                )
        else:
            self.description_label.setText("Not analyzed yet.")
            self.details_label.setText(
                "Click Analyze to have the AI look at this deck's real content and propose "
                "converted Front/Back/CSS templates."
            )

    def show_analyzing(self, *, downloading: bool) -> None:
        self.stars_label.setText("")
        self.review_label.setVisible(False)
        if downloading:
            self.description_label.setText("Downloading the local AI model…")
            self.details_label.setText(
                "One-time download, about 4.3GB -- how long this takes depends on your "
                "connection. Every analysis after this one skips straight to analyzing."
            )
        else:
            self.description_label.setText("Analyzing…")
            self.details_label.setText("The AI model is already downloaded -- usually done in under a minute.")

    def show_analysis(self, analysis: DeckAnalysis) -> None:
        stars = "★" * analysis.trust_stars + "☆" * (5 - analysis.trust_stars)
        self.stars_label.setText("%s   Trust: %d/5" % (stars, analysis.trust_stars))
        if analysis.review_message:
            self.review_label.setText(analysis.review_message)
            self.review_label.setVisible(True)
        else:
            self.review_label.setVisible(False)
        self.description_label.setText(analysis.description)
        speaks = ", ".join(t.source_field for t in analysis.audio_targets) or "(nothing to speak)"
        self.details_label.setText(
            "Speaks: %s\n"
            "Target language: %s      Native language: %s\n\n"
            "New Front: %s\n"
            "New Back: %s"
            % (
                speaks,
                analysis.target_language,
                analysis.native_language,
                ", ".join(analysis.new_front_fields) or "(none)",
                ", ".join(analysis.new_back_fields) or "(none)",
            )
        )


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
        self._note_ids: List[int] = []
        #: Recorded conversion state for the *currently selected* pair, read back via
        #: core.deck_state -- purely informational here (see module docstring for why it
        #: doesn't by itself unlock Convert/Generate TTS audio).
        self._conversion_state: Optional[ConversionState] = None
        #: This session's analysis of the currently selected pair, if any. Drives both
        #: Convert (front/back/css come from the html editor, which this populates) and,
        #: when present, Generate TTS audio (its audio_targets names which fields to speak).
        self._analysis: Optional[DeckAnalysis] = None
        #: Recomputed (not stored anywhere) whenever the selected pair is already converted --
        #: a fast, local, non-AI call to ``llm.direction.resolve_direction`` against the live
        #: notetype's own current fields, giving ``audio_targets`` without needing an analysis.
        #: ``None`` when the pair has never been converted (nothing to recompute) or has no
        #: notes to sample. See module docstring / ``_generate_tts_ready``.
        self._recomputed_direction: Optional[Direction] = None
        self._html_mode = False
        self._html_dirty = False
        self._target_deck_dirty = False
        self._tts_cancel_event: Optional[threading.Event] = None
        self._convert_running = False
        self._analyzing = False
        #: (clone_notetype_name, analysis) set right after a successful Convert, so the very
        #: next pair-change (auto-selecting that clone) can carry the just-used analysis
        #: forward instead of resetting to "not analyzed" -- lets the AI Result panel keep
        #: showing what Convert actually used, and the HTML editor keep its content, without a
        #: redundant re-Analyze (Generate TTS audio itself no longer needs this carry to work,
        #: since it can recompute audio_targets on its own -- see _recomputed_direction -- but
        #: the panel/editor still benefit from showing the real analysis rather than nothing).
        #: Consumed once, whether or not it ends up matching -- see _on_pair_changed.
        self._post_convert_carry: Optional[Tuple[str, DeckAnalysis]] = None

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

        # -- 2. left/right split ------------------------------------------------------
        splitter = QSplitter(Qt.Orientation.Horizontal)

        left_pane = QWidget()
        left_layout = QVBoxLayout(left_pane)
        left_layout.setContentsMargins(0, 0, 0, 0)

        mode_row = QHBoxLayout()
        self.ai_result_button = QPushButton("AI Result")
        self.ai_result_button.setCheckable(True)
        self.ai_result_button.setChecked(True)
        self.html_button = QPushButton("HTML")
        self.html_button.setCheckable(True)
        self._mode_group = QButtonGroup(self)
        self._mode_group.addButton(self.ai_result_button)
        self._mode_group.addButton(self.html_button)
        self.ai_result_button.clicked.connect(self._on_ai_result_mode_clicked)
        self.html_button.clicked.connect(self._on_html_mode_clicked)
        mode_row.addWidget(self.ai_result_button)
        mode_row.addWidget(self.html_button)
        mode_row.addStretch(1)
        left_layout.addLayout(mode_row)

        self.stack = QStackedWidget()
        self.ai_result_panel = _AIResultPanel()
        self.stack.addWidget(self.ai_result_panel)

        self.html_editor = _RawHtmlEditor()
        self.html_editor.changed.connect(self._on_html_edited)
        self.stack.addWidget(self.html_editor)

        left_layout.addWidget(self.stack, stretch=1)
        splitter.addWidget(left_pane)

        self.preview = PreviewPanel()
        splitter.addWidget(self.preview)

        splitter.setSizes([1, 1])
        layout.addWidget(splitter, stretch=1)

        # -- 3. voice section --------------------------------------------------------
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

        # -- 4. actions ---------------------------------------------------------------
        # Three steps, in order: Analyze asks the AI to propose a converted deck (writes
        # nothing); Convert actually clones the notetype and writes the templates Analyze
        # proposed (or that were hand-edited afterward); Generate TTS audio fills in the
        # audio field(s) Convert created. Convert stays disabled (see _update_action_state)
        # until an analysis exists for the currently selected pair -- there's no other source
        # for the front/back/css it writes. Generate TTS audio can unlock without one too, for
        # a pair already known to be converted (see _generate_tts_ready).
        self.flow_hint_label = QLabel("")
        self.flow_hint_label.setWordWrap(True)
        layout.addWidget(self.flow_hint_label)

        action_row = QHBoxLayout()
        action_row.addStretch(1)
        self.stop_button = QPushButton("Stop")
        self.stop_button.setVisible(False)
        self.stop_button.clicked.connect(self._on_stop_clicked)
        action_row.addWidget(self.stop_button)
        self.analyze_button = QPushButton("Analyze")
        self.analyze_button.clicked.connect(self._on_analyze)
        action_row.addWidget(self.analyze_button)
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
        self._pairs = decks_with_notetypes(mw.col)
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

    def _live_template_content(self) -> Tuple[str, str, str]:
        """The live notetype's own current (Front, Back, CSS) -- for a pair already known
        converted (``self._conversion_state`` is set) where no fresh or carried-forward
        analysis exists this session.

        Without this, reopening an already-converted deck left the HTML editor -- and
        therefore the preview -- blank ("Click Analyze to see a preview"), even though the
        card already exists and renders fine: the live notetype's templates ARE the converted
        card, real audio field(s) included, so there's nothing to compute or ask the AI for.
        Forcing a re-Analyze (a real AI call) just to look at or add more audio to a card that
        already exists would defeat the entire point of ``_generate_tts_ready`` recomputing
        direction locally for exactly this case -- this extends that same "read it from the
        live notetype, no AI needed" principle to what's actually shown on screen.
        """
        tmpls = self._notetype.get("tmpls") or [{}]
        return tmpls[0].get("qfmt", ""), tmpls[0].get("afmt", ""), self._notetype.get("css", "")

    def _on_pair_changed(self, index: int) -> None:
        if index < 0 or index >= len(self._pairs):
            return
        deck, notetype_name, _count = self._pairs[index]
        self._deck_name = deck
        self._notetype_name = notetype_name
        self._notetype = mw.col.models.by_name(notetype_name)

        self._note_ids = mw.col.find_notes(scope_query(notetype_name, deck))
        sample_tags = mw.col.get_note(self._note_ids[0]).tags if self._note_ids else []
        self._conversion_state = state_from_notetype(self._notetype.get("css", ""), sample_tags)
        self._recomputed_direction = self._recompute_direction()

        if (
            self._post_convert_carry is not None
            and self._post_convert_carry[0] == notetype_name
        ):
            _clone_name, analysis = self._post_convert_carry
            self._analysis = analysis
            self.html_editor.set_content(analysis.front, analysis.back, analysis.css)
            self.ai_result_panel.show_analysis(analysis)
        else:
            self._analysis = None
            if self._conversion_state is not None:
                front, back, css = self._live_template_content()
                self.html_editor.set_content(front, back, css)
            else:
                self.html_editor.set_content("", "", "")
            self.ai_result_panel.show_not_analyzed(
                self._conversion_state, can_generate_tts=self._generate_tts_ready()
            )
        self._post_convert_carry = None

        self._html_mode = False
        self._html_dirty = False
        self.ai_result_button.setChecked(True)
        self.stack.setCurrentWidget(self.ai_result_panel)

        self._target_deck_dirty = False
        self._update_target_deck_default()

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

    # -- action gating --------------------------------------------------------------

    def _is_busy(self) -> bool:
        return self._convert_running or self._analyzing or self._tts_cancel_event is not None

    def _recompute_direction(self) -> Optional[Direction]:
        """A fast, local, non-AI call to ``llm.direction.resolve_direction`` against the live
        notetype's own current fields -- gives ``audio_targets`` for an already-converted pair
        without needing an analysis. ``None`` when this pair was never converted (nothing
        recorded to resolve against) or has no notes to sample."""
        if self._conversion_state is None or not self._note_ids or self._notetype is None:
            return None
        fields = collect_field_samples(mw.col, self._note_ids[:_ANALYZE_SAMPLE_NOTES])
        tmpls = self._notetype.get("tmpls") or [{}]
        qfmt = tmpls[0].get("qfmt", "")
        afmt = tmpls[0].get("afmt", "")
        return resolve_direction(fields, qfmt, afmt, known_state=self._conversion_state)

    def _generate_tts_ready(self) -> bool:
        """Whether Generate TTS audio can run against the *live* notetype right now.

        Gated on the currently selected pair actually being converted, never on merely having
        an analysis in hand: ``self._recomputed_direction`` (see ``_recompute_direction``) is
        only ever non-``None`` when ``self._conversion_state`` is set, which ``_on_pair_changed``
        derives from the live notetype's own recorded marker -- so this is ``True`` exactly
        when the live notetype genuinely has the audio field(s) to write into. A fresh Analyze
        result alone must NOT be enough: Convert is what actually creates those fields on the
        live notetype (see ``TODO.md``'s preview-bug writeup for the same root cause), so
        running Generate TTS audio before Convert would try to write into a field that doesn't
        exist yet. Right after a real Convert this is already ``True`` again with no extra
        step needed, because ``_on_pair_changed`` re-derives it from the freshly-converted
        clone the screen just selected -- see ``_on_convert``'s ``_post_convert_carry`` comment.
        """
        return self._recomputed_direction is not None and bool(
            self._recomputed_direction.audio_targets
        )

    def _update_action_state(self) -> None:
        busy = self._is_busy()
        has_analysis = self._analysis is not None
        can_generate = self._generate_tts_ready()
        self.analyze_button.setEnabled(not busy and bool(self._note_ids))
        self.convert_button.setEnabled(has_analysis and not busy)
        self.generate_button.setEnabled(can_generate and not busy)
        if busy:
            return
        if has_analysis:
            self.generate_button.setToolTip("")
            self.flow_hint_label.setText(
                "Review the AI's result on the left (or edit the HTML directly), then "
                "click Convert. Once converted, come back and click Generate TTS audio."
            )
        elif can_generate:
            self.generate_button.setToolTip("")
            self.flow_hint_label.setText(
                "This deck is already converted (%s → %s). Click Generate TTS audio to "
                "continue adding audio -- no AI call needed. Click Analyze only if you want "
                "to review or change the templates."
                % (self._conversion_state.target_language, self._conversion_state.native_language)
            )
        else:
            self.generate_button.setToolTip(
                "Run Analyze first -- Generate TTS audio needs to know which field(s) to speak."
            )
            if self._conversion_state is not None:
                self.flow_hint_label.setText(
                    "This deck already looks converted (%s → %s), but has no real front-side "
                    "field to generate audio from. Click Analyze to re-check it."
                    % (self._conversion_state.target_language, self._conversion_state.native_language)
                )
            else:
                self.flow_hint_label.setText(
                    "Step 1: click Analyze to have the AI propose a converted deck. "
                    "Step 2: review it, then click Convert. Step 3: click Generate TTS audio."
                )

    # -- analyze --------------------------------------------------------------------

    def _on_analyze(self) -> None:
        if not self._note_ids:
            showWarning("No notes in the selected deck/notetype.", parent=self)
            return
        if self._html_dirty and not askUser(
            "Running Analyze will replace your manual HTML edits with a fresh result. "
            "Continue?",
            parent=self,
            defaultno=True,
        ):
            return

        fields = collect_field_samples(mw.col, self._note_ids[:_ANALYZE_SAMPLE_NOTES])
        tmpls = self._notetype.get("tmpls") or [{}]
        qfmt = tmpls[0].get("qfmt", "")
        afmt = tmpls[0].get("afmt", "")
        css = self._notetype.get("css", "")
        deck_name = self._deck_name
        notetype_name = self._notetype_name
        conversion_state = self._conversion_state

        # Checked up front (cheap: local file existence/size, no network, no subprocess) so the
        # very first message the user sees already says the right thing -- previously this was
        # one message covering both cases ("can take a few minutes, downloads a model") with no
        # way to tell which was actually happening. ensure_llama_runtime/ensure_model below are
        # still what actually download-if-missing and verify; this is only for wording.
        downloading = not (runtime_is_cached(_cache_dir()) and model_is_cached(_cache_dir()))

        self._analyzing = True
        self._html_dirty = False
        self._update_action_state()
        self.ai_result_panel.show_analyzing(downloading=downloading)
        self.status_label.setText("")
        if downloading:
            progress_label = (
                "Downloading the local AI model (about 4.3GB, one time only)… this can take "
                "a while depending on your connection. Every analysis after this one skips "
                "straight to analyzing."
            )
        else:
            progress_label = "Analyzing deck… the AI model is already downloaded, usually done in under a minute."
        mw.progress.start(parent=self, immediate=True, label=progress_label)

        result_holder: Dict[str, Any] = {}

        def task() -> None:
            runtime = ensure_llama_runtime(_cache_dir())
            model = ensure_model(_cache_dir())
            call_fn = functools.partial(
                call_model, runtime_path=runtime.path, model_path=model.primary_path
            )
            result_holder["analysis"] = analyze_deck(
                deck_name=deck_name,
                notetype_name=notetype_name,
                fields=fields,
                qfmt=qfmt,
                afmt=afmt,
                css=css,
                call_model_fn=call_fn,
                known_state=conversion_state,
            )

        def on_future_done(future: Any) -> None:
            mw.progress.finish()
            self._analyzing = False
            exc = future.exception()
            if exc is not None:
                # A failed Analyze doesn't touch _recomputed_direction -- if Generate TTS audio
                # was already usable without an analysis before this attempt (an
                # already-converted deck), it still is; reflect that here too, not just in the
                # button state _update_action_state sets right below.
                self.ai_result_panel.show_not_analyzed(
                    self._conversion_state, can_generate_tts=self._generate_tts_ready()
                )
                self._update_action_state()
                showWarning("Analysis failed: %r" % (exc,), parent=self)
                return
            analysis: DeckAnalysis = result_holder["analysis"]
            self._analysis = analysis
            self.html_editor.set_content(analysis.front, analysis.back, analysis.css)
            self.ai_result_panel.show_analysis(analysis)
            self._html_mode = False
            self.ai_result_button.setChecked(True)
            self.stack.setCurrentWidget(self.ai_result_panel)
            self._update_action_state()
            self._refresh_preview()

        mw.taskman.run_in_background(task, on_future_done)

    # -- HTML / AI Result mode switch ------------------------------------------------

    def _on_html_mode_clicked(self) -> None:
        if self._html_mode:
            return
        self._html_mode = True
        self.stack.setCurrentWidget(self.html_editor)

    def _on_ai_result_mode_clicked(self) -> None:
        if not self._html_mode:
            return
        self._html_mode = False
        self.stack.setCurrentWidget(self.ai_result_panel)

    def _on_html_edited(self) -> None:
        self._html_dirty = True
        self._refresh_preview()

    # -- live preview -----------------------------------------------------------------

    def _refresh_preview(self) -> None:
        if not self._note_ids or self._notetype is None:
            self.preview.clear()
            return
        front, back, css = self.html_editor.get_content()
        if not front.strip() or not back.strip():
            self.preview.clear("Click Analyze to see a preview of the converted card.")
            return
        note = mw.col.get_note(self._note_ids[0])
        preview_front = self._preview_front(front)
        self.preview.set_content(note, self._notetype, 0, front_html=preview_front, back_html=back, css=css)
        self.preview.refresh()

    def _preview_front(self, front: str) -> str:
        """``front`` with any not-yet-existing generated-audio field's block removed.

        Before Convert has actually run, these templates (fresh from Analyze, or hand-edited)
        may already reference audio field(s) Convert itself will create -- referencing them
        against the live, pre-conversion notetype (which ``note`` in ``_refresh_preview`` is
        always bound to -- it's a real note) makes Anki's own template compiler reject them as
        unknown fields ("Found '{{#ddc-audio-Word}}', but there is no field called
        'ddc-audio-Word'"), because ``ephemeral_card(custom_note_type=...)`` does not override
        which fields are considered to exist for that check (verified against real ``anki``
        source; see ``docs/api-notes.md``). An earlier fix attempt shaped a throwaway notetype
        copy with the field appended and passed that as the override instead -- plausible on
        paper, but ineffective for exactly the reason above, since the override was never what
        the check was validating against. Removing the block is a safe no-op either way: it's
        wrapped in a conditional, so it's guaranteed to render as nothing regardless of whether
        the field exists yet -- see ``llm.analyze.strip_pending_audio_html``.
        """
        if self._analysis is None:
            return front
        existing = {f["name"] for f in self._notetype.get("flds", [])}
        pending = [
            t.audio_field for t in self._analysis.audio_targets if t.audio_field not in existing
        ]
        return strip_pending_audio_html(front, pending)

    # -- voice sampling (no collection write -- QueryOp, not CollectionOp) ----------

    def _sample_text(self) -> str:
        """The text a real batch run would actually speak for the currently-previewed note, so
        "Sample" previews this deck's real content instead of a generic phrase -- the first
        audio target whose source field actually has content on this note. Falls back to
        ``_SAMPLE_TEXT`` if there's no analysis yet, no note, or every target field is empty on
        this particular note."""
        if self._note_ids and self._analysis is not None:
            note = mw.col.get_note(self._note_ids[0])
            for target in self._analysis.audio_targets:
                try:
                    text = note[target.source_field]
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
        if self._analysis is None:
            # Convert is only ever enabled once an analysis exists (see
            # _update_action_state) -- reachable only if something bypassed that gating,
            # in which case failing loudly beats writing "?" into the permanent record.
            showWarning("Run Analyze before converting.", parent=self)
            return
        front, back, css = self.html_editor.get_content()
        if not front.strip() or not back.strip():
            showWarning(
                "Analyze the deck (or write Front/Back HTML by hand) before converting.",
                parent=self,
            )
            return

        target_language = self._analysis.target_language
        native_language = self._analysis.native_language

        config = addon_config()
        mode = self.mode_box.currentData()
        source_deck_at_start = self._deck_name
        plan = build_plan(
            mode=mode,
            front=front,
            back=back,
            css=css,
            target_language=target_language,
            native_language=native_language,
            source_notetype=self._notetype_name,
            source_deck=self._deck_name,
            target_deck=self.target_deck_edit.text().strip() or None,
            suffix=config.get("name_suffix", "Converted"),
            dry_run=False,
        )
        plan.strip_tags = list(config.get("strip_tags", ["leech"]))
        plan.new_fields = [t.audio_field for t in self._analysis.audio_targets]
        plan.note_ids = list(mw.col.find_notes(plan.scope_query))

        validation = plan.validate()
        if not validation.ok:
            showWarning("Cannot convert:\n\n" + "\n".join(validation.errors), parent=self)
            return

        self._convert_running = True
        self._update_action_state()

        carried_analysis = self._analysis

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
            if carried_analysis is not None:
                self._post_convert_carry = (result.clone_notetype_name, carried_analysis)
            self._refresh_pairs()
            # _select_pair (via _on_pair_changed) already calls _update_action_state, and
            # the notetype it now points at is the freshly-converted clone -- Generate TTS
            # audio unlocks immediately via _post_convert_carry, no separate re-Analyze
            # needed.
            self._select_pair(deck_name, result.clone_notetype_name)
            # CollectionOp's own mw.col.op_made_changes(changes)-driven refresh doesn't
            # reliably pick up the new deck here (see docs/api-notes.md) -- the deck
            # creation and note writes happened before the undo marker CollectionOp's
            # changes object actually describes, so Anki's own deck browser can be left
            # showing stale state even though the addon's own dropdown updates fine.
            # Refresh it explicitly rather than rely on that.
            self._refresh_anki_main_window()

        convert_op(self, plan, expected_note_count=len(plan.note_ids), on_success=done).run_in_background()

    # -- generate TTS audio -------------------------------------------------------------

    def _on_generate_tts(self) -> None:
        if self._notetype is None:
            showWarning("No notetype selected.", parent=self)
            return
        if self._analysis is not None:
            # This session's own analysis, either just run or carried forward from a Convert
            # that just happened (_post_convert_carry) -- always the freshest answer.
            audio_targets = self._analysis.audio_targets
        elif self._recomputed_direction is not None:
            # No analysis this session, but this pair is already converted -- recomputing
            # direction against its live fields is enough, no AI call needed. See module
            # docstring / _generate_tts_ready.
            audio_targets = self._recomputed_direction.audio_targets
        else:
            audio_targets = ()

        if not audio_targets:
            showWarning(
                "Run Analyze on this notetype first -- Generate TTS audio needs to know "
                "which field(s) to speak.",
                parent=self,
            )
            return

        live_field_names = [f["name"] for f in self._notetype.get("flds", [])]
        missing = [t.audio_field for t in audio_targets if t.audio_field not in live_field_names]
        if missing:
            showWarning(
                "This notetype has no field named %s yet.\n\nConvert the deck first -- the "
                "conversion creates that field. Generating audio into a field the deck "
                "already had would overwrite the original recordings."
                % ", ".join(repr(m) for m in missing),
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

        targets = [(t.audio_field, t.source_field) for t in audio_targets]
        self._run_tts_batch(self._notetype, targets, note_ids, voice_id, limit)

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
        targets: List[Tuple[str, str]],
        note_ids: List[int],
        voice_id: str,
        limit: int,
    ) -> None:
        self._tts_cancel_event = threading.Event()
        self._update_action_state()
        self.stop_button.setEnabled(True)
        self.stop_button.setVisible(True)
        self.status_label.setText(
            "Generating audio… click Stop at any time -- audio already generated is "
            "kept, and you can continue right where you left off later."
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
            note_ids,
            voice_id,
            targets=targets,
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
