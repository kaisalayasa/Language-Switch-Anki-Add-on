"""Download and cache the Qwen2.5-7B-Instruct GGUF model files this addon uses for deck
analysis.

Facts here were verified live against the real HuggingFace API this session (not guessed):
repo ``Qwen/Qwen2.5-7B-Instruct-GGUF``, ``license:apache-2.0``, the Q4_K_M quantization split
into two files -- HuggingFace's standard convention once a GGUF exceeds ~4GB -- with each
file's exact byte size confirmed via ``?blobs=true``.

**Verification here is a size check, not a subprocess launch**, unlike ``runtime.py``'s binary
verification -- deliberately, per this project's own design notes: a multi-gigabyte file
warrants a cheap comparison against a known-good size on every call (cache-hit included, so a
truncated download left over from an interrupted run is never silently trusted), not spawning
llama.cpp just to prove a file exists.

llama.cpp loads a split GGUF automatically from just the first part's path -- ``ensure_model``
therefore only needs to hand ``-m`` the first file; the remaining part(s) are found and loaded
from the same directory with no merge step (confirmed this session, see ``docs/llm-notes.md``).

Same dependency-injection discipline as ``addon.tts.piper_voice_manager``: ``download_to`` is
injected so the resolution/caching logic is unit-testable without a network connection.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Optional, Tuple

from .runtime import _http_download_to

__all__ = [
    "ModelFile",
    "ModelFiles",
    "ModelVerificationError",
    "MODEL_FILES",
    "model_urls",
    "ensure_model",
    "model_is_cached",
]

HF_RESOLVE_BASE = "https://huggingface.co/Qwen/Qwen2.5-7B-Instruct-GGUF/resolve/main/"


@dataclass(frozen=True)
class ModelFile:
    filename: str
    #: Verified live via the HuggingFace API's ``?blobs=true`` this session.
    size_bytes: int


#: Order matters: index 0 is the file llama.cpp's ``-m`` flag is given directly.
MODEL_FILES: Tuple[ModelFile, ...] = (
    ModelFile("qwen2.5-7b-instruct-q4_k_m-00001-of-00002.gguf", 3993201344),
    ModelFile("qwen2.5-7b-instruct-q4_k_m-00002-of-00002.gguf", 689872288),
)


@dataclass(frozen=True)
class ModelFiles:
    #: Pass this one to llama.cpp's ``-m`` flag -- the remaining split part(s) are found
    #: automatically from the same directory.
    primary_path: Path
    all_paths: Tuple[Path, ...]


class ModelVerificationError(RuntimeError):
    """A model file is missing, or its size doesn't match what HuggingFace reports for it --
    most likely an interrupted/truncated download."""


DownloadFn = Callable[[str, Path], None]


def model_urls() -> Tuple[str, ...]:
    return tuple(HF_RESOLVE_BASE + f.filename for f in MODEL_FILES)


def model_is_cached(cache_dir: Path) -> bool:
    """Whether every model file already sits under ``cache_dir`` at its correct size -- a
    cheap, local check. Exists purely so a caller can tell the user "downloading now" vs.
    "already have it" *before* committing to either -- ``ensure_model`` still does the real,
    authoritative download-if-missing-and-verify regardless of what this returns.
    """
    model_dir = Path(cache_dir) / "model"
    return all(
        (model_dir / spec.filename).exists()
        and (model_dir / spec.filename).stat().st_size == spec.size_bytes
        for spec in MODEL_FILES
    )


def ensure_model(cache_dir: Path, *, download_to: Optional[DownloadFn] = None) -> ModelFiles:
    """Return the cached (downloading if needed) split GGUF files for Qwen2.5-7B-Instruct.

    Every file is size-checked against :data:`MODEL_FILES`, whether it was just downloaded or
    already sat in the cache from a previous run -- catching a truncated transfer here, with a
    clear error naming the file, rather than as a confusing llama.cpp load failure much later.
    """
    download_to = download_to or _http_download_to
    model_dir = Path(cache_dir) / "model"
    model_dir.mkdir(parents=True, exist_ok=True)

    paths = []
    for spec in MODEL_FILES:
        path = model_dir / spec.filename
        if not path.exists():
            download_to(HF_RESOLVE_BASE + spec.filename, path)
        actual_size = path.stat().st_size
        if actual_size != spec.size_bytes:
            raise ModelVerificationError(
                "%s is %d bytes, expected %d -- likely an interrupted or corrupted download. "
                "Delete it and try again." % (path, actual_size, spec.size_bytes)
            )
        paths.append(path)

    return ModelFiles(primary_path=paths[0], all_paths=tuple(paths))
