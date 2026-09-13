"""Tools-menu entry point: "Generate TTS audio…" -- M5's batch apply.

Runs Piper synthesis over every note on a converted notetype that doesn't already have
generated audio (see ``addon/ops/tts_batch.py``), with progress and cooperative
cancellation, then flips the notetype's template to actually reference the now-populated
audio fields.

Progress/cancellation wiring is modeled on real ``aqt`` source (the matching wheel pulled
from PyPI this session, the same way ``CardLayout``'s behaviour was verified -- see
``docs/api-notes.md``), not guessed:

- ``mw.progress.start()``/``.finish()`` bracket the run from the main thread, exactly like
  ``CollectionOp``/``QueryOp`` do internally (``aqt/taskman.py``'s ``with_progress``).
- ``mw.progress.want_cancel()`` is safe to poll directly from the background thread -- it's
  a plain attribute read with no main-thread guard (unlike ``.update()``, which has one and
  must be marshaled via ``mw.taskman.run_on_main``).
- The batch itself runs via ``mw.taskman.run_in_background`` -- the same primitive
  ``CollectionOp``/``QueryOp`` are built on, dropped down to directly because this needs
  incremental per-note progress that the higher-level wrapper doesn't expose.
"""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path
from typing import Any, List, Tuple

from aqt import mw
from aqt.qt import (
    QCheckBox,
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QVBoxLayout,
)
from aqt.utils import askUser, showInfo, showWarning

from ..core.profiles import match_profile
from ..core.role_schema import fields_from_notetype
from ..ops.tts_batch import finish_audio_batch, generate_note_audio, notes_needing_audio
from ..tts.piper_provider import PiperProvider
from ..tts.piper_voice_manager import CURATED_VOICES
from .convert_dialog import _decks_with_notetypes, addon_config, template_options_from_config

__all__ = ["show_tts_batch_dialog"]


def _cache_dir() -> Path:
    return Path(__file__).resolve().parent.parent / "user_files"


class TtsBatchDialog(QDialog):
    def __init__(self, parent: Any = None):
        super().__init__(parent or mw)
        self.setWindowTitle("Generate TTS audio")
        self.resize(480, 300)
        self._pairs: List[Tuple[str, str, int]] = _decks_with_notetypes(mw.col)

        layout = QVBoxLayout(self)

        layout.addWidget(QLabel("Converted deck to generate audio for:"))
        self.pair_box = QComboBox()
        for deck, notetype, count in self._pairs:
            self.pair_box.addItem("%s  —  %s  (%d notes)" % (deck, notetype, count))
        layout.addWidget(self.pair_box)

        layout.addWidget(QLabel("Voice:"))
        self.voice_box = QComboBox()
        for spec in CURATED_VOICES:
            self.voice_box.addItem(spec.display_name, spec.voice_id)
        default_voice = addon_config().get("tts_voice")
        if default_voice:
            index = self.voice_box.findData(default_voice)
            if index != -1:
                self.voice_box.setCurrentIndex(index)
        layout.addWidget(self.voice_box)

        self.force_checkbox = QCheckBox("Force regenerate (ignore already-generated audio)")
        layout.addWidget(self.force_checkbox)

        backup_row = QHBoxLayout()
        backup_row.addStretch(1)
        self.backup_button = QPushButton("Create backup now")
        self.backup_button.setToolTip(
            "Immediately creates a collection backup via col.create_backup(), regardless "
            "of Anki's normal backup interval -- a manual safety net before a batch write."
        )
        self.backup_button.clicked.connect(self._create_backup)
        backup_row.addWidget(self.backup_button)
        layout.addLayout(backup_row)

        self.status_label = QLabel("")
        self.status_label.setWordWrap(True)
        layout.addWidget(self.status_label)

        self.buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel
        )
        self.buttons.button(QDialogButtonBox.StandardButton.Ok).setText("Generate")
        self.buttons.accepted.connect(self._start)
        self.buttons.rejected.connect(self.reject)
        layout.addWidget(self.buttons)

    # -- backup -----------------------------------------------------------

    def _create_backup(self) -> None:
        try:
            created = mw.col.create_backup(
                backup_folder=mw.pm.backupFolder(), force=True, wait_for_completion=True
            )
        except Exception as exc:  # noqa: BLE001 -- surfaced to the user, not swallowed
            showWarning("Backup failed: %r" % (exc,), parent=self)
            return
        showInfo(
            "Backup created." if created else "Nothing to back up (no changes since last backup).",
            parent=self,
        )

    # -- setup / validation -------------------------------------------------

    def _current(self):
        if not self._pairs:
            return None
        return self._pairs[self.pair_box.currentIndex()]

    def _start(self) -> None:
        current = self._current()
        if current is None:
            showWarning("This collection has no notes.", parent=self)
            return
        deck, notetype_name, _ = current
        notetype = mw.col.models.by_name(notetype_name)
        live_fields = fields_from_notetype(notetype["flds"])
        match = match_profile(notetype_name, live_fields)
        if not match.usable:
            showWarning(
                "No field mapping is known for %r.\n\nMap it first via Tools → Convert "
                "deck language direction… → \"Map fields…\"." % notetype_name,
                parent=self,
            )
            return
        mapping = match.profile.to_mapping(live_fields=live_fields)
        voice_id = self.voice_box.currentData()
        force = self.force_checkbox.isChecked()

        note_ids = notes_needing_audio(mw.col, notetype_name, force=force)
        if not note_ids:
            showInfo(
                "Every note already has generated audio. Use \"Force regenerate\" to "
                "redo them anyway.",
                parent=self,
            )
            return

        config = addon_config()
        if config.get("prompt_for_backup", True):
            proceed = askUser(
                "About to generate audio for %d notes. This can take a while on first "
                "run (downloading the Piper binary/voice) and writes real audio into "
                "your notes.\n\nMake sure you have a backup -- use \"Create backup now\" "
                "below, or Anki backs up automatically on close.\n\nContinue?"
                % len(note_ids),
                parent=self,
                defaultno=False,
            )
            if not proceed:
                return

        self._run_batch(notetype, mapping, note_ids, voice_id, config)

    # -- the batch itself ---------------------------------------------------

    def _run_batch(self, notetype, mapping, note_ids, voice_id, config) -> None:
        self.buttons.setEnabled(False)
        self.status_label.setText("Starting…")
        provider = PiperProvider(_cache_dir())
        total = len(note_ids)
        counters = {"done": 0, "failed": 0}

        mw.progress.start(
            max=total, min=0, label="Generating audio…", parent=self, immediate=True
        )

        def task() -> None:
            col = mw.col
            for i, nid in enumerate(note_ids):
                if mw.progress.want_cancel():
                    break
                note = col.get_note(nid)
                result = generate_note_audio(col, note, mapping, provider, voice_id)
                if result.ok:
                    counters["done"] += 1
                else:
                    counters["failed"] += 1
                mw.taskman.run_on_main(
                    lambda i=i: mw.progress.update(
                        label="Generating audio… (%d/%d)" % (i + 1, total),
                        value=i + 1,
                        max=total,
                    )
                )
            options = replace(template_options_from_config(config), include_audio=True)
            finish_audio_batch(
                col, notetype, mapping, source_css=notetype.get("css", ""), options=options
            )

        def on_done(future) -> None:
            mw.progress.finish()
            self.buttons.setEnabled(True)
            exc = future.exception()
            if exc is not None:
                showWarning("TTS batch failed: %r" % (exc,), parent=self)
                return
            showInfo(
                "Done. %d notes generated, %d failed (left for the next run)."
                % (counters["done"], counters["failed"]),
                parent=self,
            )
            self.accept()

        mw.taskman.run_in_background(task, on_done)


def show_tts_batch_dialog() -> None:
    if mw.col is None:
        showWarning("Open a collection first.")
        return
    TtsBatchDialog(mw).exec()
