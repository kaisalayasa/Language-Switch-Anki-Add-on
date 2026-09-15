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

import threading
from typing import Any, List, Optional, Tuple

from aqt import mw
from aqt.qt import (
    QButtonGroup,
    QCheckBox,
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QRadioButton,
    QVBoxLayout,
)
from aqt.utils import askUser, showInfo, showWarning

from ..core.conversion import scope_query
from ..core.role_detect import guess_role_mapping
from ..core.role_schema import Role, fields_from_notetype
from ..ops.tts_batch import notes_needing_audio
from ..ops.tts_runner import BatchOutcome, default_concurrency, run_tts_batch
from ..tts.piper_voice_manager import CURATED_VOICES
from .convert_dialog import (
    _collect_raw_samples,
    _decks_with_notetypes,
    addon_config,
    template_options_from_config,
)

__all__ = ["show_tts_batch_dialog"]

_DEFAULT_VOICE_ID = "en_GB-alba-medium"
_BATCH_CHUNK = 500


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
        default_voice = addon_config().get("tts_voice") or _DEFAULT_VOICE_ID
        index = self.voice_box.findData(default_voice)
        if index != -1:
            self.voice_box.setCurrentIndex(index)
        layout.addWidget(self.voice_box)

        self.force_checkbox = QCheckBox("Force regenerate (ignore already-generated audio)")
        self.force_checkbox.stateChanged.connect(lambda *_args: self._update_limit_options())
        layout.addWidget(self.force_checkbox)

        limit_row = QHBoxLayout()
        limit_row.addWidget(QLabel("Notes per run:"))
        self.limit_all_radio = QRadioButton("All")
        self.limit_chunk_radio = QRadioButton("%d at a time" % _BATCH_CHUNK)
        self._chunk_tooltip = (
            "Stop after %d notes instead of doing the whole deck in one run. The rest are "
            "left for next time -- \"Generate TTS audio\" only ever processes notes that "
            "don't already have audio, so re-running later just continues where this run "
            "left off." % _BATCH_CHUNK
        )
        self.limit_chunk_radio.setToolTip(self._chunk_tooltip)
        self.limit_all_radio.setChecked(True)
        self._limit_group = QButtonGroup(self)
        self._limit_group.addButton(self.limit_all_radio)
        self._limit_group.addButton(self.limit_chunk_radio)
        limit_row.addWidget(self.limit_all_radio)
        limit_row.addWidget(self.limit_chunk_radio)
        limit_row.addStretch(1)
        layout.addLayout(limit_row)

        self.parallel_checkbox = QCheckBox(
            "Generate multiple notes at once (faster, uses more CPU)"
        )
        self.parallel_checkbox.setToolTip(
            "Runs several Piper syntheses at the same time instead of one after another. "
            "Faster on most machines, but leave this off on a low-end or already-busy "
            "computer."
        )
        layout.addWidget(self.parallel_checkbox)

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

        self._cancel_event: Optional[threading.Event] = None
        self.stop_button = QPushButton("Stop")
        self.stop_button.setVisible(False)
        self.stop_button.clicked.connect(self._on_stop_clicked)
        layout.addWidget(self.stop_button)

        self.buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel
        )
        self.buttons.button(QDialogButtonBox.StandardButton.Ok).setText("Generate")
        self.buttons.accepted.connect(self._start)
        self.buttons.rejected.connect(self.reject)
        layout.addWidget(self.buttons)

        self.pair_box.currentIndexChanged.connect(lambda *_args: self._update_limit_options())
        self._update_limit_options()

    # -- limit options, depend on how many notes are actually pending -------

    def _update_limit_options(self) -> None:
        current = self._current()
        pending = (
            len(notes_needing_audio(mw.col, current[1], force=self.force_checkbox.isChecked()))
            if current is not None
            else 0
        )
        fits_in_one_chunk = pending <= _BATCH_CHUNK
        self.limit_chunk_radio.setEnabled(not fits_in_one_chunk)
        if fits_in_one_chunk:
            self.limit_chunk_radio.setToolTip(
                "Only %d note%s pending -- same as \"All\" right now."
                % (pending, "" if pending == 1 else "s")
            )
            self.limit_all_radio.setChecked(True)
        else:
            self.limit_chunk_radio.setToolTip(self._chunk_tooltip)

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

        sample_note_ids = mw.col.find_notes(scope_query(notetype_name, deck))
        raw_samples = _collect_raw_samples(sample_note_ids[:8])
        tmpls = notetype.get("tmpls") or [{}]
        mapping = guess_role_mapping(
            notetype_name,
            live_fields,
            raw_samples,
            front_html=tmpls[0].get("qfmt", ""),
            back_html=tmpls[0].get("afmt", ""),
            css=notetype.get("css", ""),
        )
        if not mapping.validate().ok:
            showWarning(
                "No usable field mapping for %r.\n\nMap it first via Tools → Convert "
                "deck language direction… → \"Map fields…\"." % notetype_name,
                parent=self,
            )
            return
        if mapping.first(Role.TARGET_AUDIO) is None and mapping.first(Role.TARGET_SENTENCE_AUDIO) is None:
            showWarning(
                "This notetype has no field for generated audio yet.\n\nConvert the deck "
                "first -- the conversion creates that field.",
                parent=self,
            )
            return
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

        limit = _BATCH_CHUNK if self.limit_chunk_radio.isChecked() else 0
        run_count = min(limit, len(note_ids)) if limit else len(note_ids)

        config = addon_config()
        if config.get("prompt_for_backup", True):
            proceed = askUser(
                "About to generate audio for %d notes%s. This can take a while on first "
                "run (downloading the Piper binary/voice) and writes real audio into "
                "your notes.\n\nMake sure you have a backup -- use \"Create backup now\" "
                "below, or Anki backs up automatically on close.\n\nContinue?"
                % (
                    run_count,
                    "" if run_count == len(note_ids) else " (of %d pending)" % len(note_ids),
                ),
                parent=self,
                defaultno=False,
            )
            if not proceed:
                return

        self._run_batch(notetype, mapping, note_ids, voice_id, config, limit)

    # -- the batch itself ---------------------------------------------------

    def _on_stop_clicked(self) -> None:
        if self._cancel_event is not None:
            self._cancel_event.set()
        self.stop_button.setEnabled(False)
        self.status_label.setText(
            "Stopping after the note currently in progress… audio already generated is "
            "kept, and you can continue right where this leaves off later."
        )

    def _run_batch(self, notetype, mapping, note_ids, voice_id, config, limit) -> None:
        self.buttons.setEnabled(False)
        self._cancel_event = threading.Event()
        self.stop_button.setEnabled(True)
        self.stop_button.setVisible(True)
        self.status_label.setText(
            "Generating audio… click Stop at any time -- audio already generated is kept, "
            "and you can continue right where you left off later."
        )
        concurrency = default_concurrency() if self.parallel_checkbox.isChecked() else 1

        def on_done(outcome: BatchOutcome) -> None:
            self.buttons.setEnabled(True)
            self.stop_button.setVisible(False)
            self._cancel_event = None
            if outcome.error is not None:
                showWarning("TTS batch failed: %s" % outcome.error, parent=self)
                return
            remaining_note = (
                " %d notes still remain -- run this again to continue." % outcome.remaining
                if outcome.remaining
                else ""
            )
            headline = "Stopped early." if outcome.cancelled else "Done."
            showInfo(
                "%s %d notes generated, %d failed.%s"
                % (headline, outcome.done, outcome.failed, remaining_note),
                parent=self,
            )
            self.accept()

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
            cancel_event=self._cancel_event,
            on_done=on_done,
        )


def show_tts_batch_dialog() -> None:
    if mw.col is None:
        showWarning("Open a collection first.")
        return
    TtsBatchDialog(mw).exec()
