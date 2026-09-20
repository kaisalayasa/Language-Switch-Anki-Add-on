"""Curated Piper voice list, plus download/cache of a voice's ``.onnx``/``.onnx.json`` pair.

The URL pattern and the two starting voices were verified against the real HuggingFace repo
(see ``docs/api-notes.md``) -- ``en/en_US/ljspeech/high/en_US-ljspeech-high.onnx`` genuinely
exists there. ``CURATED_VOICES`` is deliberately short for M2 (the user asked for a real
multi-voice picker later, "once we get to the final stages", not now); the point of
:class:`VoiceSpec` and this module's shape is that growing the list later is just appending
entries, not a redesign.

``en_US-lessac-medium`` was the original default here and has been deliberately removed, not
just swapped for a better voice: the Lessac/Blizzard-2013 corpus this voice was trained on is
licensed "Research Purposes only" and explicitly excludes "the development, marketing,
commercialisation, sale or licencing of voice synthesis ... products" -- see
https://www.cstr.ed.ac.uk/projects/blizzard/2013/lessac_blizzard2013/license.html. That's
broad enough to cover a free open-source addon whose purpose is voice synthesis, so it was
never safe to ship as a default regardless of price. ``en_US-ljspeech-high`` replaces it:
the LJSpeech corpus is public domain per its own model card, no restriction of any kind.
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
    voice_id: str  # e.g. "en_US-ljspeech-high" -- also the .onnx file's basename
    locale: str  # e.g. "en_US"
    name: str  # e.g. "ljspeech"
    quality: str  # e.g. "high"
    display_name: str  # shown in the voice picker


@dataclass(frozen=True)
class VoiceFiles:
    onnx_path: Path
    config_path: Path


CURATED_VOICES: Tuple[VoiceSpec, ...] = (
    VoiceSpec("en_US-ljspeech-high", "en_US", "ljspeech", "high", "English (US) – LJSpeech"),
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
