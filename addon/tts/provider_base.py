"""Abstract interface every TTS backend implements.

Only ``PiperProvider`` exists for v1 (``claude.md`` explicitly rules out cloud providers for
this phase), but keeping ``synthesize`` behind this interface means adding a second backend
later is a new class, not a refactor of every call site.
"""

from __future__ import annotations

import abc
from pathlib import Path
from typing import Optional

__all__ = ["TTSProvider"]


class TTSProvider(abc.ABC):
    @abc.abstractmethod
    def synthesize(self, text: str, *, voice_id: str, out_path: Optional[Path] = None) -> Path:
        """Return the path to a generated audio file for ``text`` spoken in ``voice_id``.

        ``out_path`` lets a caller control where the file lands (e.g. a deterministic
        per-note path during batch generation in M5); when omitted, implementations write
        to a fresh temporary file.
        """
        raise NotImplementedError
