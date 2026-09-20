"""Download, cache, and verify a static ffmpeg binary, used only to shrink Piper's WAV output
down to MP3 after synthesis (see ``piper_provider.py``'s ``_compress_to_mp3``).

Same subprocess-and-download-once pattern as ``piper_binary_manager.py`` and ``llm/runtime.py``
-- ffmpeg is invoked as a plain subprocess, never linked into this addon's own code, which is
also why its license doesn't need to match this addon's MIT license.

Two different, independently-verified sources are used depending on platform (verified this
session -- see ``docs/api-notes.md``):

- Windows and Linux: BtbN/FFmpeg-Builds (https://github.com/BtbN/FFmpeg-Builds), a GitHub
  Releases-hosted project that builds ffmpeg from source via CI. Its "lgpl" flavor is built
  with every GPL-only component (libx264, libx265, ...) left out -- confirmed by reading
  ``scripts.d/50-libmp3lame.sh`` and ``scripts.d/45-libvorbis.sh`` in that repo directly:
  neither is gated behind a GPL/LGPL check, so both encoders are present in the lgpl build.
  Only that build is used here, so the downloaded binary itself is LGPL-2.1+, not GPL.
- macOS: no equivalent LGPL-only static build exists for macOS from any actively-maintained
  source found. evermeet.cx (https://evermeet.cx/ffmpeg/) is the source ffmpeg.org's own
  download page recommends for macOS static builds; its build is GPL-3.0+ (confirmed via its
  own ``/ffmpeg/info/ffmpeg/release`` API, which reports ``--enable-gpl``). Used anyway,
  because a GPL binary invoked only as a subprocess -- never linked, never redistributed by
  this addon -- doesn't place this addon's own MIT code under GPL; this is the same "shell out
  to a GPL command-line tool" pattern used by countless permissively-licensed applications that
  call ``ffmpeg``/``git``/ImageMagick as external processes rather than linking them. Only
  x86_64 builds exist for macOS from this source; Apple Silicon Macs run it under Rosetta 2,
  which macOS installs automatically the first time an Intel binary is launched.

Like ``piper_binary_manager.py``, every URL below is a **pinned, verified-working snapshot**,
not a "latest" resolver -- BtbN republishes new content at the same "latest"-tagged release over
time (there is no per-version GitHub tag to pin to, the same situation already accepted for
llama.cpp's nightly builds), and evermeet.cx's URL embeds a concrete version number that will
eventually be superseded by a newer one. Both need periodic re-verification the same way
llama.cpp's pinned build does -- see ``docs/api-notes.md``.

Compression is a pure optimization on top of an already-working pipeline (Piper's WAV output
plays fine on its own): ``ensure_ffmpeg_binary`` failing for ANY reason -- unsupported platform,
offline, a dead pinned URL -- must never break TTS generation. Every call site in
``piper_provider.py`` treats a failure here as "skip compression for this run," never as a hard
error.
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
    "FfmpegBinary",
    "UnsupportedPlatform",
    "FfmpegVerificationError",
    "resolve_asset",
    "ensure_ffmpeg_binary",
]


@dataclass(frozen=True)
class _Asset:
    url: str
    license: str


# Verified against the real release/API responses this session -- see the module docstring and
# docs/api-notes.md. Do not add a platform entry without checking the source actually serves
# that combination.
_BTBN_BASE = "https://github.com/BtbN/FFmpeg-Builds/releases/download/latest/"
_EVERMEET_URL = "https://evermeet.cx/ffmpeg/ffmpeg-9.0.2.zip"
_EVERMEET_LICENSE = "GPL-3.0+ (evermeet.cx) -- subprocess-only, see module docstring"

_ASSET_TABLE = {
    ("windows", "amd64"): _Asset(
        _BTBN_BASE + "ffmpeg-n9.0-latest-win64-lgpl-9.0.zip", "LGPL-2.1+ (BtbN/FFmpeg-Builds)"
    ),
    ("windows", "x86_64"): _Asset(
        _BTBN_BASE + "ffmpeg-n9.0-latest-win64-lgpl-9.0.zip", "LGPL-2.1+ (BtbN/FFmpeg-Builds)"
    ),
    ("linux", "x86_64"): _Asset(
        _BTBN_BASE + "ffmpeg-n9.0-latest-linux64-lgpl-9.0.tar.xz",
        "LGPL-2.1+ (BtbN/FFmpeg-Builds)",
    ),
    ("linux", "amd64"): _Asset(
        _BTBN_BASE + "ffmpeg-n9.0-latest-linux64-lgpl-9.0.tar.xz",
        "LGPL-2.1+ (BtbN/FFmpeg-Builds)",
    ),
    ("linux", "aarch64"): _Asset(
        _BTBN_BASE + "ffmpeg-n9.0-latest-linuxarm64-lgpl-9.0.tar.xz",
        "LGPL-2.1+ (BtbN/FFmpeg-Builds)",
    ),
    ("darwin", "x86_64"): _Asset(_EVERMEET_URL, _EVERMEET_LICENSE),
    ("darwin", "amd64"): _Asset(_EVERMEET_URL, _EVERMEET_LICENSE),
    ("darwin", "arm64"): _Asset(_EVERMEET_URL, _EVERMEET_LICENSE + "; runs under Rosetta 2"),
    ("darwin", "aarch64"): _Asset(_EVERMEET_URL, _EVERMEET_LICENSE + "; runs under Rosetta 2"),
}
# No linux/armv7l (32-bit ARM) entry -- BtbN doesn't publish one. resolve_asset() raises
# UnsupportedPlatform for that combination; PiperProvider treats that as "skip compression"
# rather than an error (Piper itself does support that platform, just not audio compression).


class UnsupportedPlatform(RuntimeError):
    """No known ffmpeg static build for the detected (system, machine) pair."""


class FfmpegVerificationError(RuntimeError):
    """The binary was downloaded/extracted but did not run successfully."""


@dataclass(frozen=True)
class FfmpegBinary:
    path: Path
    version: str


class SubprocessResult(NamedTuple):
    returncode: int
    stdout: str
    stderr: str


DownloadFn = Callable[[str, Path], None]
RunFn = Callable[..., SubprocessResult]


def resolve_asset(system: str, machine: str) -> _Asset:
    key = (system.lower(), machine.lower())
    asset = _ASSET_TABLE.get(key)
    if asset is None:
        raise UnsupportedPlatform(
            "No known ffmpeg static build for %s/%s. Known combinations: %s"
            % (system, machine, sorted(_ASSET_TABLE))
        )
    return asset


def ensure_ffmpeg_binary(
    cache_dir: Path,
    *,
    system: Optional[str] = None,
    machine: Optional[str] = None,
    download_to: Optional[DownloadFn] = None,
    run: Optional[RunFn] = None,
) -> FfmpegBinary:
    """Return a verified, ready-to-run ffmpeg binary under ``cache_dir``.

    Mirrors ``piper_binary_manager.ensure_piper_binary`` exactly: reuse an already-extracted,
    already-verified binary if found, otherwise download the platform's pinned asset, extract
    it, set the executable bit on non-Windows, and run it once to confirm it actually works
    before trusting it.
    """
    system = system or platform.system()
    machine = machine or platform.machine()
    asset = resolve_asset(system, machine)
    download_to = download_to or _http_download_to
    run = run or _run_subprocess

    root = Path(cache_dir) / "ffmpeg"
    exe_name = "ffmpeg.exe" if system.lower() == "windows" else "ffmpeg"

    existing = _find_executable(root, exe_name)
    if existing is None:
        root.mkdir(parents=True, exist_ok=True)
        archive_path = root / asset.url.rsplit("/", 1)[-1]
        download_to(asset.url, archive_path)
        _extract(archive_path, root)
        archive_path.unlink(missing_ok=True)
        existing = _find_executable(root, exe_name)
        if existing is None:
            raise FfmpegVerificationError(
                "Extracted %s but found no %r anywhere under %s" % (asset.url, exe_name, root)
            )
        if system.lower() != "windows":
            mode = os.stat(existing).st_mode
            os.chmod(existing, mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)

    version = _verify(existing, run)
    return FfmpegBinary(path=existing, version=version)


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
    elif name.endswith(".tar.xz") or name.endswith(".txz"):
        with tarfile.open(archive_path) as tf:
            tf.extractall(dest)
    elif name.endswith(".tar.gz") or name.endswith(".tgz"):
        with tarfile.open(archive_path) as tf:
            tf.extractall(dest)
    else:
        raise FfmpegVerificationError("Don't know how to extract %s" % archive_path)


def _verify(binary_path: Path, run: RunFn) -> str:
    result = run([str(binary_path), "-version"])
    output = (result.stdout or "").strip() or (result.stderr or "").strip()
    if not output:
        raise FfmpegVerificationError(
            "%s ran but produced no output on -version (exit %d)"
            % (binary_path, result.returncode)
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


def _run_subprocess(argv, input_text: Optional[str] = None) -> SubprocessResult:
    # Same rationale as piper_binary_manager._run_subprocess: suppress the console flash on
    # Windows when spawning a console app from Anki's Qt GUI process.
    extra = (
        {"creationflags": subprocess.CREATE_NO_WINDOW}
        if hasattr(subprocess, "CREATE_NO_WINDOW")
        else {}
    )
    completed = subprocess.run(
        argv,
        input=input_text,
        capture_output=True,
        text=True,
        **extra,
    )
    return SubprocessResult(completed.returncode, completed.stdout, completed.stderr)
