"""Runs a TTS batch with progress, cooperative cancellation, an optional cap on how many
notes to process in one run, and optional concurrent synthesis.

Factored out of ``ui/tts_batch_dialog.py`` and ``ui/main_screen.py``, which used to each
hand-roll an identical sequential loop -- this is now the one place that logic lives.

**Concurrency safety.** Only ``provider.synthesize()`` (a Piper subprocess call -- pure, no
``col``/``note`` access) ever runs on a worker thread. Every ``col`` call -- reading a
note's source text, writing finished audio back, tagging, saving -- stays on the single
background thread ``mw.taskman.run_in_background`` already hands this module, exactly like
the sequential path always did. Anki's collection is not documented as safe for concurrent
access from multiple Python threads at once, so this module never risks finding out the
hard way: see ``core.tts_batch.plan_note_audio``/``apply_note_audio``, which exist
specifically to keep the "read"/"write" halves on the single collection-owning thread while
only the pure synthesis call in between is parallelized.

Kept separate from ``ops/tts_batch.py``, which has no ``aqt``/``anki`` import at module
scope and is unit-tested directly against ``tests/fake_collection.py`` -- importing
``aqt.progress``/``aqt.taskman`` there would break that. ``aqt`` stays a lazy, per-call
import here too, matching ``ops/convert_op.py``'s existing convention, so this module stays
importable (though not meaningfully callable) outside a real Anki process.
"""

from __future__ import annotations

import os
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple

from ..core.role_schema import RoleMapping
from ..tts.piper_provider import EmptyTextError, PiperProvider
from .tts_batch import (
    apply_note_audio,
    finish_audio_batch,
    generate_note_audio,
    notes_needing_audio,
    plan_note_audio,
)

__all__ = ["BatchOutcome", "default_concurrency", "cache_dir", "run_tts_batch"]


def default_concurrency() -> int:
    """A conservative "parallel" worker count for the opt-in fast path -- capped at 4 even
    on big machines, since each worker spawns a real Piper subprocess, and scaled down
    automatically on small ones so turning this on doesn't stall a low-end machine."""
    return max(1, min(4, os.cpu_count() or 1))


def cache_dir() -> Path:
    return Path(__file__).resolve().parent.parent / "user_files"


@dataclass
class BatchOutcome:
    done: int = 0
    failed: int = 0
    #: Notes still lacking generated audio after this run -- either because "Limit" left
    #: them untouched, or because they failed. Recomputed via ``notes_needing_audio`` after
    #: the run rather than tracked by hand, so it's correct regardless of *why* a note is
    #: still pending.
    remaining: int = 0
    cancelled: bool = False
    #: Set only for an error that aborted the whole batch (not a single note) -- e.g. the
    #: Piper binary/voice failed to download at all.
    error: Optional[str] = None


def _synth_one(
    provider: PiperProvider, voice_id: str, pending: list
) -> Tuple[Optional[List[Tuple[str, Path]]], Optional[str]]:
    """Runs on a worker thread: pure synthesis, no ``col``/``note`` access at all."""
    synthesized: List[Tuple[str, Path]] = []
    for item in pending:
        try:
            wav_path = provider.synthesize(item.text, voice_id=voice_id)
        except EmptyTextError:
            continue
        except Exception as exc:  # noqa: BLE001 -- reported per-note, must not sink the batch
            return None, "%s: %s" % (type(exc).__name__, exc)
        synthesized.append((item.audio_field, wav_path))
    return synthesized, None


def run_tts_batch(
    parent: Any,
    notetype: Dict[str, Any],
    mapping: RoleMapping,
    note_ids: List[int],
    voice_id: str,
    config: dict,
    *,
    template_options_from_config: Callable[[dict], Any],
    limit: int = 0,
    concurrency: int = 1,
    cancel_event: Optional[threading.Event] = None,
    on_done: Optional[Callable[[BatchOutcome], None]] = None,
) -> None:
    """Processes ``note_ids`` (already the "needs audio" set -- see ``notes_needing_audio``)
    through Piper, then flips the notetype's template on. Must be called from the Qt main
    thread; ``on_done`` (if given) is called back on the main thread when finished.

    ``limit`` caps how many of ``note_ids`` this run touches -- 0 means no cap, processing
    everything passed in. The rest are simply left untagged, exactly as if the run had been
    cancelled partway through: the next "Generate TTS audio" run picks them up automatically
    via ``notes_needing_audio``, no separate bookkeeping needed.

    ``concurrency`` > 1 opts into running that many Piper subprocesses at once (see the
    module docstring for why this is safe); 1 (the default) is the original, unchanged
    sequential path.

    ``cancel_event``, if given, is polled alongside Anki's own ``mw.progress.want_cancel()``
    (which only ever becomes true when the user presses Escape or closes the progress
    window -- there is no visible Cancel button on it). A caller that wants a real, visible
    Stop button sets this event from that button's click handler instead of guessing at an
    undocumented way to trigger Anki's own cancellation from code.
    """
    from aqt import mw  # lazy: keep this module importable without a running Anki process

    todo = note_ids[:limit] if limit else list(note_ids)
    provider = PiperProvider(cache_dir())
    total = len(todo)
    outcome = BatchOutcome()

    def cancel_requested() -> bool:
        return mw.progress.want_cancel() or (cancel_event is not None and cancel_event.is_set())

    mw.progress.start(max=total, min=0, label="Generating audio…", parent=parent, immediate=True)

    def report(i: int) -> None:
        mw.taskman.run_on_main(
            lambda: mw.progress.update(
                label="Generating audio… (%d/%d)" % (i, total), value=i, max=total
            )
        )

    def run_sequential(col: Any) -> None:
        for i, nid in enumerate(todo):
            if cancel_requested():
                outcome.cancelled = True
                break
            note = col.get_note(nid)
            result = generate_note_audio(col, note, mapping, provider, voice_id)
            if result.ok:
                outcome.done += 1
            else:
                outcome.failed += 1
            report(i + 1)

    def run_concurrent(col: Any) -> None:
        provider.ensure_ready(voice_id)  # once, up front -- avoid racing first-download

        plans = {}
        for nid in todo:
            if cancel_requested():
                outcome.cancelled = True
                return
            plans[nid] = plan_note_audio(col.get_note(nid), mapping)

        done_count = 0
        with ThreadPoolExecutor(max_workers=concurrency) as pool:
            futures = {
                pool.submit(_synth_one, provider, voice_id, plans[nid]): nid for nid in todo
            }
            for future in as_completed(futures):
                nid = futures[future]
                synthesized, error = future.result()
                note = col.get_note(nid)
                result = apply_note_audio(col, note, synthesized or [], error=error)
                if result.ok:
                    outcome.done += 1
                else:
                    outcome.failed += 1
                done_count += 1
                report(done_count)
                if cancel_requested():
                    outcome.cancelled = True
                    for f in futures:
                        f.cancel()
                    break

    def task() -> None:
        col = mw.col
        if todo:
            if concurrency > 1:
                run_concurrent(col)
            else:
                run_sequential(col)

        options = replace(template_options_from_config(config), include_audio=True)
        finish_audio_batch(
            col, notetype, mapping, source_css=notetype.get("css", ""), options=options
        )
        outcome.remaining = len(notes_needing_audio(col, notetype["name"], force=False))

    def on_future_done(future: Any) -> None:
        mw.progress.finish()
        exc = future.exception()
        if exc is not None:
            outcome.error = repr(exc)
        if on_done is not None:
            on_done(outcome)

    mw.taskman.run_in_background(task, on_future_done)
