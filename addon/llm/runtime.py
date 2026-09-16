"""Download, cache, and verify the llama.cpp CPU-only runtime for the current OS/arch.

Facts here were verified against the real GitHub API and the real downloaded binary this
session (not guessed -- see ``docs/llm-notes.md``): llama.cpp has no stable release, every tag
is a rolling nightly build number, so ``RELEASE_TAG`` is a pin that will need periodic
re-verification, not a "latest" to defer to. The asset table is deliberately CPU-only --
CUDA/ROCm/Vulkan/SYCL/OpenVINO variants need matching GPU drivers this addon can't assume, and
are 5-30x larger. Re-confirmed live against the pinned tag's real release assets while building
this module.

The binary this addon actually invokes is ``llama-completion``, not ``llama-cli`` -- see
``docs/llm-notes.md`` for why (``llama-cli`` is an interactive REPL that pollutes stdout with a
banner even in single-turn mode). **Only the Windows build of this executable has actually been
run** (``llama-completion.exe --version`` exits 0, printing the version line to stderr, exactly
like Piper's own ``--version`` check). The Linux/macOS archives are assumed to ship an
equivalently-named ``llama-completion`` binary (no extension) by the same convention Piper's own
binary follows across platforms, but that assumption has not been exercised on real Linux/macOS
hardware -- flag this prominently if a report ever suggests otherwise.

Same dependency-injection discipline as ``addon.tts.piper_binary_manager``, which this module
otherwise mirrors closely: ``download_to``/``run`` are injected so resolution, extraction, and
verification are unit-testable without a network connection or a real binary.
"""

from __future__ import annotations

import os
import platform
import stat
import subprocess
import tarfile
import urllib.request
import zipfile
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, NamedTuple, Optional

__all__ = [
    "LlamaRuntime",
    "UnsupportedPlatform",
    "RuntimeVerificationError",
    "resolve_asset_name",
    "asset_url",
    "ensure_llama_runtime",
]

#: Pinned nightly build. Re-verify against the real release asset list before ever bumping this
#: -- there is no "latest stable" tag to fall back on (see module docstring).
RELEASE_TAG = "b10983"
RELEASE_BASE_URL = "https://github.com/ggml-org/llama.cpp/releases/download/%s/" % RELEASE_TAG

#: (system.lower(), machine.lower()) -> asset filename. Verified live against the real
#: release's asset list for RELEASE_TAG. Deliberately excludes CUDA/ROCm/Vulkan/SYCL/OpenVINO
#: variants -- see module docstring.
_ASSET_TABLE = {
    ("windows", "amd64"): "llama-%s-bin-win-cpu-x64.zip" % RELEASE_TAG,
    ("windows", "x86_64"): "llama-%s-bin-win-cpu-x64.zip" % RELEASE_TAG,
    ("windows", "arm64"): "llama-%s-bin-win-cpu-arm64.zip" % RELEASE_TAG,
    ("linux", "x86_64"): "llama-%s-bin-ubuntu-x64.tar.gz" % RELEASE_TAG,
    ("linux", "amd64"): "llama-%s-bin-ubuntu-x64.tar.gz" % RELEASE_TAG,
    ("linux", "aarch64"): "llama-%s-bin-ubuntu-arm64.tar.gz" % RELEASE_TAG,
    ("darwin", "x86_64"): "llama-%s-bin-macos-x64.tar.gz" % RELEASE_TAG,
    ("darwin", "amd64"): "llama-%s-bin-macos-x64.tar.gz" % RELEASE_TAG,
    ("darwin", "arm64"): "llama-%s-bin-macos-arm64.tar.gz" % RELEASE_TAG,
}


class UnsupportedPlatform(RuntimeError):
    """No llama.cpp CPU release asset exists for the detected (system, machine) pair."""


class RuntimeVerificationError(RuntimeError):
    """The runtime was downloaded/extracted but ``llama-completion`` did not run successfully."""


@dataclass(frozen=True)
class LlamaRuntime:
    path: Path
    version: str


class SubprocessResult(NamedTuple):
    returncode: int
    stdout: str
    stderr: str


DownloadFn = Callable[[str, Path], None]
RunFn = Callable[..., SubprocessResult]


def resolve_asset_name(system: str, machine: str) -> str:
    key = (system.lower(), machine.lower())
    asset = _ASSET_TABLE.get(key)
    if asset is None:
        raise UnsupportedPlatform(
            "No published llama.cpp CPU build for %s/%s (release %s). Known combinations: %s"
            % (system, machine, RELEASE_TAG, sorted(_ASSET_TABLE))
        )
    return asset


def asset_url(asset_name: str) -> str:
    return RELEASE_BASE_URL + asset_name


def ensure_llama_runtime(
    cache_dir: Path,
    *,
    system: Optional[str] = None,
    machine: Optional[str] = None,
    download_to: Optional[DownloadFn] = None,
    run: Optional[RunFn] = None,
) -> LlamaRuntime:
    """Return a verified, ready-to-run ``llama-completion`` binary under ``cache_dir``.

    Reuses an already-extracted, already-verified binary if one is found; otherwise downloads
    the correct release asset, extracts it, sets the executable bit on non-Windows, and runs it
    once (``--version``) to confirm it actually works before trusting it -- same pattern as
    ``ensure_piper_binary``, re-run every call since this binary is small (tens of MB) and the
    check is cheap, unlike the multi-gigabyte model file ``model_manager.py`` manages, which
    gets a size check instead of a subprocess launch.
    """
    system = system or platform.system()
    machine = machine or platform.machine()
    asset = resolve_asset_name(system, machine)
    download_to = download_to or _http_download_to
    run = run or _run_subprocess

    root = Path(cache_dir) / "llama-runtime"
    exe_name = "llama-completion.exe" if system.lower() == "windows" else "llama-completion"

    existing = _find_executable(root, exe_name)
    if existing is None:
        root.mkdir(parents=True, exist_ok=True)
        archive_path = root / asset
        download_to(asset_url(asset), archive_path)
        _extract(archive_path, root)
        archive_path.unlink(missing_ok=True)
        existing = _find_executable(root, exe_name)
        if existing is None:
            raise RuntimeVerificationError(
                "Extracted %s but found no %r anywhere under %s" % (asset, exe_name, root)
            )
        if system.lower() != "windows":
            mode = os.stat(existing).st_mode
            os.chmod(existing, mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)

    version = _verify(existing, run)
    return LlamaRuntime(path=existing, version=version)


def _find_executable(root: Path, exe_name: str) -> Optional[Path]:
    if not root.exists():
        return None
    target = exe_name.lower()
    for path in root.rglob("*"):
        if path.is_file() and path.name.lower() == target:
            return path
    return None


def _extract(archive_path: Path, dest: Path) -> None:
    name = archive_path.name.lower()
    if name.endswith(".zip"):
        with zipfile.ZipFile(archive_path) as zf:
            zf.extractall(dest)
    elif name.endswith(".tar.gz") or name.endswith(".tgz"):
        with tarfile.open(archive_path) as tf:
            tf.extractall(dest)
    else:
        raise RuntimeVerificationError("Don't know how to extract %s" % archive_path)


def _verify(binary_path: Path, run: RunFn) -> str:
    result = run([str(binary_path), "--version"])
    output = (result.stdout or "").strip() or (result.stderr or "").strip()
    if not output:
        raise RuntimeVerificationError(
            "%s ran but produced no output on --version (exit %d); it may not be the real "
            "llama-completion binary. Confirm the actual invocation and update "
            "docs/llm-notes.md." % (binary_path, result.returncode)
        )
    return output


def _http_download_to(url: str, dest: Path) -> None:
    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp = dest.with_suffix(dest.suffix + ".part")
    with urllib.request.urlopen(url) as response, open(tmp, "wb") as fh:
        while True:
            chunk = response.read(1024 * 256)
            if not chunk:
                break
            fh.write(chunk)
    tmp.replace(dest)


def _run_subprocess(argv) -> SubprocessResult:
    extra = {"creationflags": subprocess.CREATE_NO_WINDOW} if hasattr(subprocess, "CREATE_NO_WINDOW") else {}
    completed = subprocess.run(
        argv, capture_output=True, text=True, encoding="utf-8", errors="replace", **extra
    )
    return SubprocessResult(completed.returncode, completed.stdout, completed.stderr)
