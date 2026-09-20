"""``ffmpeg_binary_manager``: OS/arch resolution, extraction, caching, verification.

Mirrors ``tests/test_piper_binary_manager.py``'s structure -- same shape of manager, same
dependency-injected network/subprocess calls, so the same testing approach applies: nothing
here touches a real network or spawns a real process. The asset table under test was verified
against the real BtbN/FFmpeg-Builds GitHub API and evermeet.cx's own JSON API this session; see
``docs/api-notes.md``.
"""

from __future__ import annotations

import os
import stat
import tarfile
import tempfile
import unittest
import zipfile
from pathlib import Path

from addon.tts.ffmpeg_binary_manager import (
    FfmpegVerificationError,
    SubprocessResult,
    UnsupportedPlatform,
    ensure_ffmpeg_binary,
    resolve_asset,
)


def _zip_with_exe(dest: Path, exe_name: str, content: bytes = b"fake binary") -> None:
    with zipfile.ZipFile(dest, "w") as zf:
        zf.writestr("ffmpeg/%s" % exe_name, content)


def _ok_run(argv, input_text=None):
    return SubprocessResult(0, "ffmpeg version 9.0", "")


class TestResolveAsset(unittest.TestCase):
    def test_known_platforms_resolve_to_the_verified_hosts(self):
        self.assertIn("BtbN", resolve_asset("Windows", "AMD64").license)
        self.assertIn("BtbN", resolve_asset("Linux", "x86_64").license)
        self.assertIn("BtbN", resolve_asset("Linux", "aarch64").license)
        self.assertIn("evermeet.cx", resolve_asset("Darwin", "x86_64").license)
        self.assertIn("evermeet.cx", resolve_asset("Darwin", "arm64").license)

    def test_linux_32bit_arm_is_unsupported_not_silently_mapped(self):
        """BtbN publishes no 32-bit ARM linux build -- confirmed this session, unlike Piper
        itself which does support this platform. Compression is simply unavailable there."""
        with self.assertRaises(UnsupportedPlatform):
            resolve_asset("Linux", "armv7l")

    def test_unknown_platform_raises(self):
        with self.assertRaises(UnsupportedPlatform):
            resolve_asset("Plan9", "alpha")


class TestEnsureFfmpegBinary(unittest.TestCase):
    def test_downloads_extracts_verifies_and_returns_the_binary(self):
        with tempfile.TemporaryDirectory() as cache_dir:
            calls = []

            def fake_download(url, dest):
                calls.append(url)
                _zip_with_exe(dest, "ffmpeg.exe")

            binary = ensure_ffmpeg_binary(
                cache_dir, system="Windows", machine="AMD64",
                download_to=fake_download, run=_ok_run,
            )

            self.assertTrue(binary.path.exists())
            self.assertEqual(binary.path.name, "ffmpeg.exe")
            self.assertEqual(binary.version, "ffmpeg version 9.0")
            self.assertEqual(len(calls), 1)

    def test_second_call_reuses_the_cached_binary_without_redownloading(self):
        with tempfile.TemporaryDirectory() as cache_dir:
            calls = []

            def fake_download(url, dest):
                calls.append(url)
                _zip_with_exe(dest, "ffmpeg.exe")

            ensure_ffmpeg_binary(cache_dir, system="Windows", machine="AMD64",
                                  download_to=fake_download, run=_ok_run)
            ensure_ffmpeg_binary(cache_dir, system="Windows", machine="AMD64",
                                  download_to=fake_download, run=_ok_run)

            self.assertEqual(len(calls), 1)

    def test_extracts_tar_xz_and_finds_the_binary_inside(self):
        with tempfile.TemporaryDirectory() as cache_dir:
            def fake_download(url, dest):
                src = Path(cache_dir) / "_src_ffmpeg"
                src.write_bytes(b"fake binary")
                with tarfile.open(dest, "w:xz") as tf:
                    tf.add(src, arcname="ffmpeg/ffmpeg")
                src.unlink()

            binary = ensure_ffmpeg_binary(
                cache_dir, system="Linux", machine="x86_64",
                download_to=fake_download, run=_ok_run,
            )
            self.assertEqual(binary.path.name, "ffmpeg")
            if os.name == "posix":
                mode = os.stat(binary.path).st_mode
                self.assertTrue(mode & stat.S_IXUSR)

    def test_verification_failure_raises_with_no_silent_success(self):
        with tempfile.TemporaryDirectory() as cache_dir:
            def fake_download(url, dest):
                _zip_with_exe(dest, "ffmpeg.exe")

            def silent_run(argv, input_text=None):
                return SubprocessResult(1, "", "")

            with self.assertRaises(FfmpegVerificationError):
                ensure_ffmpeg_binary(
                    cache_dir, system="Windows", machine="AMD64",
                    download_to=fake_download, run=silent_run,
                )

    def test_missing_executable_after_extraction_raises(self):
        with tempfile.TemporaryDirectory() as cache_dir:
            def fake_download(url, dest):
                with zipfile.ZipFile(dest, "w") as zf:
                    zf.writestr("ffmpeg/readme.txt", b"no executable in this archive")

            with self.assertRaises(FfmpegVerificationError):
                ensure_ffmpeg_binary(
                    cache_dir, system="Windows", machine="AMD64",
                    download_to=fake_download, run=_ok_run,
                )

    def test_unsupported_platform_raises_before_any_download(self):
        def fake_download(url, dest):
            raise AssertionError("must not download for an unsupported platform")

        with tempfile.TemporaryDirectory() as cache_dir:
            with self.assertRaises(UnsupportedPlatform):
                ensure_ffmpeg_binary(
                    cache_dir, system="Linux", machine="armv7l",
                    download_to=fake_download, run=_ok_run,
                )


if __name__ == "__main__":
    unittest.main()
