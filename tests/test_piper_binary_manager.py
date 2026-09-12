"""``piper_binary_manager``: OS/arch resolution, extraction, caching, verification.

All network and subprocess calls are injected fakes -- these tests never touch the real
GitHub release or spawn a real process. The asset table under test was verified against the
real GitHub API this session; see ``docs/api-notes.md``.
"""

from __future__ import annotations

import os
import stat
import tarfile
import tempfile
import unittest
import zipfile
from pathlib import Path

from addon.tts.piper_binary_manager import (
    PiperVerificationError,
    SubprocessResult,
    UnsupportedPlatform,
    asset_url,
    ensure_piper_binary,
    resolve_asset_name,
)


def _zip_with_exe(dest: Path, exe_name: str, content: bytes = b"fake binary") -> None:
    with zipfile.ZipFile(dest, "w") as zf:
        zf.writestr("piper/%s" % exe_name, content)


def _ok_run(argv, input_text=None):
    return SubprocessResult(0, "piper 1.2.0", "")


class TestResolveAssetName(unittest.TestCase):
    def test_known_platforms_map_to_the_verified_asset_names(self):
        self.assertEqual(resolve_asset_name("Windows", "AMD64"), "piper_windows_amd64.zip")
        self.assertEqual(resolve_asset_name("Linux", "x86_64"), "piper_linux_x86_64.tar.gz")
        self.assertEqual(resolve_asset_name("Linux", "aarch64"), "piper_linux_aarch64.tar.gz")
        self.assertEqual(resolve_asset_name("Linux", "armv7l"), "piper_linux_armv7l.tar.gz")
        self.assertEqual(resolve_asset_name("Darwin", "x86_64"), "piper_macos_x64.tar.gz")
        self.assertEqual(resolve_asset_name("Darwin", "arm64"), "piper_macos_aarch64.tar.gz")

    def test_windows_arm64_is_unsupported_not_silently_mapped(self):
        """Piper's real release list has no Windows ARM64 asset -- confirmed this session."""
        with self.assertRaises(UnsupportedPlatform):
            resolve_asset_name("Windows", "ARM64")

    def test_unknown_platform_raises(self):
        with self.assertRaises(UnsupportedPlatform):
            resolve_asset_name("Plan9", "alpha")

    def test_asset_url_uses_the_verified_release_tag(self):
        url = asset_url("piper_windows_amd64.zip")
        self.assertEqual(
            url,
            "https://github.com/rhasspy/piper/releases/download/"
            "2023.11.14-2/piper_windows_amd64.zip",
        )


class TestEnsurePiperBinary(unittest.TestCase):
    def test_downloads_extracts_verifies_and_returns_the_binary(self):
        with tempfile.TemporaryDirectory() as cache_dir:
            calls = []

            def fake_download(url, dest):
                calls.append(url)
                _zip_with_exe(dest, "piper.exe")

            binary = ensure_piper_binary(
                cache_dir, system="Windows", machine="AMD64",
                download_to=fake_download, run=_ok_run,
            )

            self.assertTrue(binary.path.exists())
            self.assertEqual(binary.path.name, "piper.exe")
            self.assertEqual(binary.version, "piper 1.2.0")
            self.assertEqual(calls, [asset_url("piper_windows_amd64.zip")])

    def test_second_call_reuses_the_cached_binary_without_redownloading(self):
        with tempfile.TemporaryDirectory() as cache_dir:
            calls = []

            def fake_download(url, dest):
                calls.append(url)
                _zip_with_exe(dest, "piper.exe")

            ensure_piper_binary(cache_dir, system="Windows", machine="AMD64",
                                 download_to=fake_download, run=_ok_run)
            ensure_piper_binary(cache_dir, system="Windows", machine="AMD64",
                                 download_to=fake_download, run=_ok_run)

            self.assertEqual(len(calls), 1)

    def test_extracts_tar_gz_and_finds_the_binary_inside(self):
        with tempfile.TemporaryDirectory() as cache_dir:
            def fake_download(url, dest):
                src = Path(cache_dir) / "_src_piper"
                src.write_bytes(b"fake binary")
                with tarfile.open(dest, "w:gz") as tf:
                    tf.add(src, arcname="piper/piper")
                src.unlink()

            binary = ensure_piper_binary(
                cache_dir, system="Linux", machine="x86_64",
                download_to=fake_download, run=_ok_run,
            )
            self.assertEqual(binary.path.name, "piper")
            if os.name == "posix":
                # chmod's executable-bit semantics only mean something on POSIX; on
                # Windows os.chmod is a no-op for this purpose, so only assert it there.
                mode = os.stat(binary.path).st_mode
                self.assertTrue(mode & stat.S_IXUSR)

    def test_verification_failure_raises_with_no_silent_success(self):
        with tempfile.TemporaryDirectory() as cache_dir:
            def fake_download(url, dest):
                _zip_with_exe(dest, "piper.exe")

            def silent_run(argv, input_text=None):
                return SubprocessResult(1, "", "")

            with self.assertRaises(PiperVerificationError):
                ensure_piper_binary(
                    cache_dir, system="Windows", machine="AMD64",
                    download_to=fake_download, run=silent_run,
                )

    def test_missing_executable_after_extraction_raises(self):
        with tempfile.TemporaryDirectory() as cache_dir:
            def fake_download(url, dest):
                with zipfile.ZipFile(dest, "w") as zf:
                    zf.writestr("piper/readme.txt", b"no executable in this archive")

            with self.assertRaises(PiperVerificationError):
                ensure_piper_binary(
                    cache_dir, system="Windows", machine="AMD64",
                    download_to=fake_download, run=_ok_run,
                )

    def test_unsupported_platform_raises_before_any_download(self):
        def fake_download(url, dest):
            raise AssertionError("must not download for an unsupported platform")

        with tempfile.TemporaryDirectory() as cache_dir:
            with self.assertRaises(UnsupportedPlatform):
                ensure_piper_binary(
                    cache_dir, system="Windows", machine="ARM64",
                    download_to=fake_download, run=_ok_run,
                )


if __name__ == "__main__":
    unittest.main()
