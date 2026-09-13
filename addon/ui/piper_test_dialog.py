"""M2's "single-sentence sample playback working in isolation" dialog.

Deliberately separate from ``ConvertDialog``: M2 only has to prove Piper synthesis works
standalone, not write audio into real notes (that's M5's batch-apply job, once the deck
iteration/progress/caching machinery exists). Mirrors the pattern already used for the
CardLayout preview in M1 -- a small, honest, real-in-Anki way to hear the result rather than
trusting the pipeline blind.

Synthesis touches no collection data, so this uses ``QueryOp`` (background thread only) rather
than ``CollectionOp`` (background thread + collection write + undo bookkeeping) -- there is
nothing here for Anki's undo system to know about.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from aqt import mw
from aqt.operations import QueryOp
from aqt.qt import (
    QComboBox,
    QDialog,
    QHBoxLayout,
    QLabel,
    QPlainTextEdit,
    QPushButton,
    QVBoxLayout,
)
from aqt.utils import showWarning

from ..tts.piper_provider import PiperProvider
from ..tts.piper_voice_manager import CURATED_VOICES

__all__ = ["PiperTestDialog", "show_piper_test_dialog"]

_SAMPLE_TEXT = "This is a sample sentence for testing."


def _cache_dir() -> Path:
    """The addon's own ``user_files/`` folder -- the standard Anki convention for data an
    addon downloads itself, since AnkiWeb/anki-addon-builder never wipes this directory on
    an addon update (unlike the rest of the addon's folder)."""
    return Path(__file__).resolve().parent.parent / "user_files"


class PiperTestDialog(QDialog):
    def __init__(self, parent=None):
        super().__init__(parent or mw)
        self.setWindowTitle("Test Piper voice")
        self.resize(480, 260)

        layout = QVBoxLayout(self)

        layout.addWidget(QLabel("Text to speak:"))
        self.text_edit = QPlainTextEdit(self)
        self.text_edit.setPlainText(_SAMPLE_TEXT)
        layout.addWidget(self.text_edit)

        layout.addWidget(QLabel("Voice:"))
        self.voice_combo = QComboBox(self)
        for spec in CURATED_VOICES:
            self.voice_combo.addItem(spec.display_name, spec.voice_id)
        default_voice = (mw.addonManager.getConfig(__name__.split(".")[0]) or {}).get(
            "tts_voice"
        ) or "en_GB-alba-medium"
        index = self.voice_combo.findData(default_voice)
        if index != -1:
            self.voice_combo.setCurrentIndex(index)
        layout.addWidget(self.voice_combo)

        self.status_label = QLabel("")
        self.status_label.setWordWrap(True)
        layout.addWidget(self.status_label)

        row = QHBoxLayout()
        row.addStretch(1)
        self.speak_button = QPushButton("Speak")
        self.speak_button.clicked.connect(self._on_speak)
        row.addWidget(self.speak_button)
        layout.addLayout(row)

    def _on_speak(self) -> None:
        text = self.text_edit.toPlainText().strip()
        if not text:
            return
        voice_id = self.voice_combo.currentData()

        self.speak_button.setEnabled(False)
        self.status_label.setText(
            "Synthesizing… (first use downloads the Piper binary and this voice; later "
            "uses are cached and near-instant)"
        )

        def op(_col: Any) -> Path:
            provider = PiperProvider(_cache_dir())
            return provider.synthesize(text, voice_id=voice_id)

        def on_success(wav_path: Path) -> None:
            self.speak_button.setEnabled(True)
            self.status_label.setText("Playing…")
            _play(wav_path, parent=self)

        def on_failure(exc: Exception) -> None:
            self.speak_button.setEnabled(True)
            self.status_label.setText("Failed.")
            showWarning(str(exc), parent=self)

        QueryOp(parent=self, op=op, success=on_success).failure(on_failure).run_in_background()


def _play(wav_path: Path, *, parent: Any) -> None:
    """Play a WAV file through Anki's own audio player.

    ``aqt.sound.av_player.play_file`` is a long-standing, widely-used part of Anki's public
    addon-facing sound API, but -- per ``claude.md``'s "never guess" rule -- it has not been
    independently confirmed against this specific 26.08.1 build the way the rest of this
    addon's Anki calls have (see docs/api-notes.md). Degrade to an actionable message rather
    than crash if it doesn't exist or its shape has changed.
    """
    try:
        from aqt.sound import av_player
    except ImportError as exc:
        showWarning(
            "Synthesized %s but could not import aqt.sound.av_player to play it: %r"
            % (wav_path, exc),
            parent=parent,
        )
        return
    try:
        av_player.play_file(str(wav_path))
    except (AttributeError, TypeError) as exc:
        showWarning(
            "Synthesized %s but aqt.sound.av_player.play_file(...) failed: %r\n\n"
            "Please paste this message so docs/api-notes.md can be updated with the "
            "correct call shape for your Anki build." % (wav_path, exc),
            parent=parent,
        )


def show_piper_test_dialog() -> None:
    if mw.col is None:
        showWarning("Open a collection first.")
        return
    PiperTestDialog(mw).exec()
