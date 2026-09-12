"""Curated Piper voice list, plus download/cache of a voice's ``.onnx``/``.onnx.json`` pair.

The URL pattern and the two starting voices were verified against the real HuggingFace repo
this session (see ``docs/api-notes.md``) -- ``en/en_US/lessac/medium/en_US-lessac-medium.onnx``
genuinely exists there. ``CURATED_VOICES`` is deliberately short for M2 (the user asked for a
real multi-voice picker later, "once we get to the final stages", not now); the point of
:class:`VoiceSpec` and this module's shape is that growing the list later is just appending
entries, not a redesign.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Callable, List, Optional, Tuple

__all__ = [
    "VoiceSpec",
    "VoiceFiles",
    "UnknownVoice",
    "CURATED_VOICES",
    "find_voice",
    "voice_urls",
    "ensure_voice",
]

HF_RESOLVE_BASE = "https://huggingface.co/rhasspy/piper-voices/resolve/main/"


@dataclass(frozen=True)
class VoiceSpec:
    voice_id: str  # e.g. "en_US-lessac-medium" -- also the .onnx file's basename
    locale: str  # e.g. "en_US"
    name: str  # e.g. "lessac"
    quality: str  # e.g. "medium"
    display_name: str  # shown in the voice picker


@dataclass(frozen=True)
class VoiceFiles:
    onnx_path: Path
    config_path: Path


CURATED_VOICES: Tuple[VoiceSpec, ...] = (
    VoiceSpec("en_US-lessac-medium", "en_US", "lessac", "medium", "English (US) – Lessac"),
    VoiceSpec("en_GB-alba-medium", "en_GB", "alba", "medium", "English (UK) – Alba"),
)


class UnknownVoice(KeyError):
    pass


def find_voice(voice_id: str) -> VoiceSpec:
    for spec in CURATED_VOICES:
        if spec.voice_id == voice_id:
            return spec
    raise UnknownVoice(
        "%r is not a curated voice. Known voices: %s"
        % (voice_id, [spec.voice_id for spec in CURATED_VOICES])
    )


def voice_urls(spec: VoiceSpec) -> Tuple[str, str]:
    language = spec.locale.split("_")[0]
    base = "%s/%s/%s/%s/%s" % (language, spec.locale, spec.name, spec.quality, spec.voice_id)
    return (HF_RESOLVE_BASE + base + ".onnx", HF_RESOLVE_BASE + base + ".onnx.json")


DownloadFn = Callable[[str, Path], None]


def ensure_voice(
    cache_dir: Path,
    voice_id: str,
    *,
    download_to: Optional[DownloadFn] = None,
) -> VoiceFiles:
    """Return the cached (downloading if needed) ``.onnx``/``.onnx.json`` pair for ``voice_id``."""
    spec = find_voice(voice_id)
    voices_dir = Path(cache_dir) / "voices"
    onnx_path = voices_dir / (spec.voice_id + ".onnx")
    config_path = voices_dir / (spec.voice_id + ".onnx.json")

    if onnx_path.exists() and config_path.exists():
        return VoiceFiles(onnx_path, config_path)

    from .piper_binary_manager import _http_download_to  # same download primitive, no dup

    download_to = download_to or _http_download_to
    onnx_url, config_url = voice_urls(spec)
    voices_dir.mkdir(parents=True, exist_ok=True)
    download_to(config_url, config_path)
    download_to(onnx_url, onnx_path)
    return VoiceFiles(onnx_path, config_path)
