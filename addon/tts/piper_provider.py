"""``TTSProvider`` implementation backed by the Piper CLI, invoked as a subprocess.

Per ``claude.md``: subprocess, not the ``piper-tts`` Python package (that would drag in
onnxruntime's platform-specific wheels into addon packaging). Text is always sanitized before
synthesis -- real field data routinely has HTML noise and embedded other-language
characters, and feeding that raw to Piper produces garbage output.

The exact CLI invocation shape (stdin text + ``--model``/``--output_file`` flags) matches
Piper's documented usage but has not been run against the real downloaded binary yet --
see the "still to confirm" entry in ``docs/api-notes.md``. If it mismatches, ``run`` (the
injected subprocess wrapper) will surface a non-zero exit / stderr rather than fail silently.

**Compression.** Piper only ever writes uncompressed WAV -- no flag of its own produces
anything smaller. After a successful synthesis, ``synthesize`` makes one best-effort attempt
to shrink that WAV down to a mono MP3 via ``ffmpeg_binary_manager``'s ffmpeg binary (~44KB/sec
of WAV audio becomes ~6KB/sec of MP3 -- roughly a 7x reduction, with no audible quality loss
for spoken word at this bitrate). This is a pure optimization layered on top of an already-
working pipeline: **any** failure to get a working ffmpeg (unsupported platform, offline, a
dead pinned download URL, a bad conversion) is caught in ``_compress_to_mp3`` and silently
falls back to returning the original WAV untouched -- compression must never be the reason
generating audio stops working. ``col.media.add_file``/the ``[sound:...]`` tag written by
``ops/tts_batch.py`` are both format-agnostic, so nothing downstream needed to change for
this. See ``ffmpeg_binary_manager.py`` for where the binary comes from and why its license
doesn't apply to this addon's own code.
"""

from __future__ import annotations

import threading
from pathlib import Path
from tempfile import NamedTemporaryFile
from typing import Optional, Sequence, Tuple

from .ffmpeg_binary_manager import ensure_ffmpeg_binary
from .piper_binary_manager import RunFn, _run_subprocess, ensure_piper_binary
from .piper_voice_manager import DownloadFn, ensure_voice
from .provider_base import TTSProvider
from .sanitize import sanitize_text
from .script_ranges import ranges_for_voice

__all__ = ["PiperProvider", "EmptyTextError", "SynthesisError"]

#: Mono MP3 bitrate used for compressed TTS output -- generous for intelligible spoken word,
#: nowhere near what's needed for music, chosen to bias toward the smallest safe file size.
_MP3_BITRATE = "48k"


class EmptyTextError(ValueError):
    """Nothing was left to speak after sanitizing the input text."""


class SynthesisError(RuntimeError):
    """Piper ran but did not produce usable audio."""


class PiperProvider(TTSProvider):
    def __init__(
        self,
        cache_dir: Path,
        *,
        allowed_ranges: Optional[Sequence[Tuple[int, int]]] = None,
        download_to: Optional[DownloadFn] = None,
        run: Optional[RunFn] = None,
    ):
        """``allowed_ranges`` defaults to ``None``, meaning "work it out from the voice"
        (see :mod:`addon.tts.script_ranges`) rather than to a fixed script.

        That default used to be a Latin range list, which quietly assumed every deck this
        addon would ever convert speaks a Latin-script language. For anything else it
        filtered the text away to nothing, which surfaces as "empty field", so a run could
        complete with no errors and no audio. Deriving from the voice makes the right thing
        happen without every call site having to remember to pass this.
        """
        self.cache_dir = Path(cache_dir)
        self.allowed_ranges = allowed_ranges
        self._download_to = download_to
        self._run: RunFn = run or _run_subprocess
        # Guards ensure_ffmpeg_binary specifically -- it's new code with a first-download race
        # the same as ensure_piper_binary/ensure_voice already have, but unlike those two
        # (accepted as-is elsewhere), nothing already calls this one from ensure_ready(), so a
        # concurrent TTS batch (ops/tts_runner.py's run_concurrent) really could have several
        # worker threads reach it at once on a cold cache.
        self._ffmpeg_lock = threading.Lock()

    def ensure_ready(self, voice_id: str) -> None:
        """Downloads the Piper binary and this voice if not already cached, without
        synthesizing anything.

        Call this once, synchronously, before fanning synthesis calls for the same voice
        out across multiple threads -- ``ensure_piper_binary``/``ensure_voice`` (called
        internally by :meth:`synthesize`) are not written to be safe against two threads
        racing to perform the *first* download of the same file at once.

        The binary and the voice live under separate cache subdirectories and share no
        state, so on a cold start (neither cached yet) this runs both downloads at once
        in a small thread pool instead of one after the other -- a real speedup on a fresh
        install, since both are genuinely large, independent transfers.
        """
        from concurrent.futures import ThreadPoolExecutor

        with ThreadPoolExecutor(max_workers=2) as pool:
            binary_future = pool.submit(
                ensure_piper_binary, self.cache_dir, download_to=self._download_to, run=self._run
            )
            voice_future = pool.submit(
                ensure_voice, self.cache_dir, voice_id, download_to=self._download_to
            )
            binary_future.result()
            voice_future.result()

    def synthesize(self, text: str, *, voice_id: str, out_path: Optional[Path] = None) -> Path:
        ranges = (
            self.allowed_ranges
            if self.allowed_ranges is not None
            else ranges_for_voice(voice_id)
        )
        clean = sanitize_text(text, allowed_ranges=ranges)
        if not clean:
            raise EmptyTextError(
                "Nothing left to synthesize after sanitizing %r (voice %r speaks a "
                "different script than this text is written in, or the field is empty)"
                % (text, voice_id)
            )

        binary = ensure_piper_binary(
            self.cache_dir, download_to=self._download_to, run=self._run
        )
        voice = ensure_voice(self.cache_dir, voice_id, download_to=self._download_to)

        if out_path is None:
            with NamedTemporaryFile(suffix=".wav", delete=False) as fh:
                out_path = Path(fh.name)
        else:
            out_path = Path(out_path)
            out_path.parent.mkdir(parents=True, exist_ok=True)

        result = self._run(
            [str(binary.path), "--model", str(voice.onnx_path), "--output_file", str(out_path)],
            input_text=clean,
        )
        if result.returncode != 0:
            raise SynthesisError(
                "piper exited %d for voice %r: %s" % (result.returncode, voice_id, result.stderr)
            )
        if not out_path.exists() or out_path.stat().st_size == 0:
            raise SynthesisError(
                "piper reported success but wrote no audio to %s" % out_path
            )
        return self._compress_to_mp3(out_path) or out_path

    def _compress_to_mp3(self, wav_path: Path) -> Optional[Path]:
        """Best-effort WAV -> mono MP3 shrink via ffmpeg. Returns ``None`` (never raises) on
        any failure at all, so a caller can just fall back to the original WAV -- see the
        module docstring for why compression must never be able to break synthesis."""
        try:
            with self._ffmpeg_lock:
                binary = ensure_ffmpeg_binary(
                    self.cache_dir, download_to=self._download_to, run=self._run
                )
            mp3_path = wav_path.with_suffix(".mp3")
            result = self._run(
                [
                    str(binary.path),
                    "-y",
                    "-i",
                    str(wav_path),
                    "-ac",
                    "1",
                    "-codec:a",
                    "libmp3lame",
                    "-b:a",
                    _MP3_BITRATE,
                    str(mp3_path),
                ]
            )
            if result.returncode != 0 or not mp3_path.exists() or mp3_path.stat().st_size == 0:
                return None
        except Exception:  # noqa: BLE001 -- compression is optional; any failure just skips it
            return None
        try:
            wav_path.unlink()
        except OSError:
            pass
        return mp3_path
