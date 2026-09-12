"""Download, cache, and verify the Piper CLI binary for the current OS/arch.

Facts baked in here were verified against the real GitHub API this session (not guessed --
see ``docs/api-notes.md``): release tag ``2023.11.14-2`` is genuinely the latest Piper
release, and the asset list below is its actual, complete set of published archives. There
is no native Windows ARM64 build; that combination raises :class:`UnsupportedPlatform`
rather than silently grabbing the amd64 asset.

All I/O is dependency-injected (``download_to``, ``run``) so the resolution/extraction/
verification *logic* is unit-testable without a network connection or a real Piper binary --
same discipline ``tests/fake_collection.py`` applies to the Anki side of this addon. The
default implementations (``_http_download_to``, ``_run_subprocess``) are the only code paths
in this module that touch the network or spawn a process, and they are never exercised by
the pure test suite.
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
    "PiperBinary",
    "UnsupportedPlatform",
    "PiperVerificationError",
    "resolve_asset_name",
    "asset_url",
    "ensure_piper_binary",
]

RELEASE_TAG = "2023.11.14-2"
RELEASE_BASE_URL = "https://github.com/rhasspy/piper/releases/download/%s/" % RELEASE_TAG

# (system.lower(), machine.lower()) -> asset filename. Verified against the real release's
# asset list; do not add an entry without checking the release actually ships it.
_ASSET_TABLE = {
    ("windows", "amd64"): "piper_windows_amd64.zip",
    ("windows", "x86_64"): "piper_windows_amd64.zip",
    ("linux", "x86_64"): "piper_linux_x86_64.tar.gz",
    ("linux", "amd64"): "piper_linux_x86_64.tar.gz",
    ("linux", "aarch64"): "piper_linux_aarch64.tar.gz",
    ("linux", "armv7l"): "piper_linux_armv7l.tar.gz",
    ("darwin", "x86_64"): "piper_macos_x64.tar.gz",
    ("darwin", "amd64"): "piper_macos_x64.tar.gz",
    ("darwin", "arm64"): "piper_macos_aarch64.tar.gz",
    ("darwin", "aarch64"): "piper_macos_aarch64.tar.gz",
}


class UnsupportedPlatform(RuntimeError):
    """No Piper release asset exists for the detected (system, machine) pair."""


class PiperVerificationError(RuntimeError):
    """The binary was downloaded/extracted but did not run successfully."""


@dataclass(frozen=True)
class PiperBinary:
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
            "No published Piper build for %s/%s (release %s). Known combinations: %s"
            % (system, machine, RELEASE_TAG, sorted(_ASSET_TABLE))
        )
    return asset


def asset_url(asset_name: str) -> str:
    return RELEASE_BASE_URL + asset_name


def ensure_piper_binary(
    cache_dir: Path,
    *,
    system: Optional[str] = None,
    machine: Optional[str] = None,
    download_to: Optional[DownloadFn] = None,
    run: Optional[RunFn] = None,
) -> PiperBinary:
    """Return a verified, ready-to-run Piper binary under ``cache_dir``.

    Reuses an already-extracted, already-verified binary if one is found; otherwise
    downloads the correct release asset, extracts it, sets the executable bit on
    non-Windows, and runs it once to confirm it actually works before trusting it.
    """
    system = system or platform.system()
    machine = machine or platform.machine()
    asset = resolve_asset_name(system, machine)
    download_to = download_to or _http_download_to
    run = run or _run_subprocess

    root = Path(cache_dir) / "piper"
    exe_name = "piper.exe" if system.lower() == "windows" else "piper"

    existing = _find_executable(root, exe_name)
    if existing is None:
        root.mkdir(parents=True, exist_ok=True)
        archive_path = root / asset
        download_to(asset_url(asset), archive_path)
        _extract(archive_path, root)
        archive_path.unlink(missing_ok=True)
        existing = _find_executable(root, exe_name)
        if existing is None:
            raise PiperVerificationError(
                "Extracted %s but found no %r anywhere under %s" % (asset, exe_name, root)
            )
        if system.lower() != "windows":
            mode = os.stat(existing).st_mode
            os.chmod(existing, mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)

    version = _verify(existing, run)
    return PiperBinary(path=existing, version=version)


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
        raise PiperVerificationError("Don't know how to extract %s" % archive_path)


def _verify(binary_path: Path, run: RunFn) -> str:
    result = run([str(binary_path), "--version"])
    output = (result.stdout or "").strip() or (result.stderr or "").strip()
    if not output:
        raise PiperVerificationError(
            "%s ran but produced no output on --version (exit %d); it may not be the real "
            "Piper binary. Confirm the actual invocation with `piper --help` and update "
            "docs/api-notes.md." % (binary_path, result.returncode)
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
    # On Windows, spawning a console app (piper.exe) from a GUI process (Anki/Qt) briefly
    # flashes a console window unless told not to -- CREATE_NO_WINDOW suppresses that.
    # Harmless either way, but not needed on other platforms, where the flag doesn't exist.
    extra = {"creationflags": subprocess.CREATE_NO_WINDOW} if hasattr(subprocess, "CREATE_NO_WINDOW") else {}
    completed = subprocess.run(
        argv,
        input=input_text,
        capture_output=True,
        text=True,
        **extra,
    )
    return SubprocessResult(completed.returncode, completed.stdout, completed.stderr)
