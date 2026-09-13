"""``TTSProvider`` implementation backed by the Piper CLI, invoked as a subprocess.

Per ``claude.md``: subprocess, not the ``piper-tts`` Python package (that would drag in
onnxruntime's platform-specific wheels into addon packaging). Text is always sanitized before
synthesis -- real field data has HTML noise and embedded other-language characters (see
``docs/deck-facts.md``), and feeding that raw to Piper produces garbage output.

The exact CLI invocation shape (stdin text + ``--model``/``--output_file`` flags) matches
Piper's documented usage but has not been run against the real downloaded binary yet --
see the "still to confirm" entry in ``docs/api-notes.md``. If it mismatches, ``run`` (the
injected subprocess wrapper) will surface a non-zero exit / stderr rather than fail silently.
"""

from __future__ import annotations

from pathlib import Path
from tempfile import NamedTemporaryFile
from typing import Optional, Sequence, Tuple

from .piper_binary_manager import RunFn, _run_subprocess, ensure_piper_binary
from .piper_voice_manager import DownloadFn, ensure_voice
from .provider_base import TTSProvider
from .sanitize import LATIN_RANGES, sanitize_text

__all__ = ["PiperProvider", "EmptyTextError", "SynthesisError"]


class EmptyTextError(ValueError):
    """Nothing was left to speak after sanitizing the input text."""


class SynthesisError(RuntimeError):
    """Piper ran but did not produce usable audio."""


class PiperProvider(TTSProvider):
    def __init__(
        self,
        cache_dir: Path,
        *,
        allowed_ranges: Sequence[Tuple[int, int]] = LATIN_RANGES,
        download_to: Optional[DownloadFn] = None,
        run: Optional[RunFn] = None,
    ):
        self.cache_dir = Path(cache_dir)
        self.allowed_ranges = allowed_ranges
        self._download_to = download_to
        self._run: RunFn = run or _run_subprocess

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
        clean = sanitize_text(text, allowed_ranges=self.allowed_ranges)
        if not clean:
            raise EmptyTextError(
                "Nothing left to synthesize after sanitizing %r" % (text,)
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
        return out_path
