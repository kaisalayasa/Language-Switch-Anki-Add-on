"""``llm.runtime``: OS/arch resolution, extraction, caching, verification of the llama.cpp
CPU-only runtime.

All network and subprocess calls are injected fakes -- these tests never touch the real GitHub
release or spawn a real process. The asset table under test was verified against the real
GitHub API this session; see ``docs/llm-notes.md``.
"""

from __future__ import annotations

import os
import stat
import tarfile
import tempfile
import unittest
import zipfile
from pathlib import Path

from addon.llm.runtime import (
    RELEASE_TAG,
    RuntimeVerificationError,
    SubprocessResult,
    UnsupportedPlatform,
    asset_url,
    ensure_llama_runtime,
    resolve_asset_name,
)


def _zip_with_exe(dest: Path, exe_name: str, content: bytes = b"fake binary") -> None:
    with zipfile.ZipFile(dest, "w") as zf:
        zf.writestr("llama/%s" % exe_name, content)


def _ok_run(argv):
    return SubprocessResult(0, "", "version: 0.4.1-dev (build 10983, commit 9e7171624)")


class TestResolveAssetName(unittest.TestCase):
    def test_known_platforms_map_to_the_verified_asset_names(self):
        self.assertEqual(resolve_asset_name("Windows", "AMD64"), "llama-%s-bin-win-cpu-x64.zip" % RELEASE_TAG)
        self.assertEqual(resolve_asset_name("Windows", "ARM64"), "llama-%s-bin-win-cpu-arm64.zip" % RELEASE_TAG)
        self.assertEqual(resolve_asset_name("Linux", "x86_64"), "llama-%s-bin-ubuntu-x64.tar.gz" % RELEASE_TAG)
        self.assertEqual(resolve_asset_name("Linux", "aarch64"), "llama-%s-bin-ubuntu-arm64.tar.gz" % RELEASE_TAG)
        self.assertEqual(resolve_asset_name("Darwin", "x86_64"), "llama-%s-bin-macos-x64.tar.gz" % RELEASE_TAG)
        self.assertEqual(resolve_asset_name("Darwin", "arm64"), "llama-%s-bin-macos-arm64.tar.gz" % RELEASE_TAG)

    def test_asset_name_never_double_prefixes_the_release_tag(self):
        """RELEASE_TAG already includes the leading 'b' (e.g. "b10983") -- a format string
        that adds another one would produce the wrong, nonexistent filename "llama-bb10983-...".
        """
        name = resolve_asset_name("Windows", "AMD64")
        self.assertNotIn("bb%s" % RELEASE_TAG.lstrip("b"), name)
        self.assertTrue(name.startswith("llama-%s-" % RELEASE_TAG))

    def test_unknown_platform_raises(self):
        with self.assertRaises(UnsupportedPlatform):
            resolve_asset_name("Plan9", "alpha")

    def test_no_32_bit_arm_build_is_unsupported_not_silently_mapped(self):
        with self.assertRaises(UnsupportedPlatform):
            resolve_asset_name("Linux", "armv7l")

    def test_asset_url_uses_the_pinned_release_tag(self):
        url = asset_url("llama-%s-bin-win-cpu-x64.zip" % RELEASE_TAG)
        self.assertEqual(
            url,
            "https://github.com/ggml-org/llama.cpp/releases/download/%s/"
            "llama-%s-bin-win-cpu-x64.zip" % (RELEASE_TAG, RELEASE_TAG),
        )


class TestEnsureLlamaRuntime(unittest.TestCase):
    def test_downloads_extracts_verifies_and_returns_the_binary(self):
        with tempfile.TemporaryDirectory() as cache_dir:
            calls = []

            def fake_download(url, dest):
                calls.append(url)
                _zip_with_exe(dest, "llama-completion.exe")

            runtime = ensure_llama_runtime(
                cache_dir, system="Windows", machine="AMD64",
                download_to=fake_download, run=_ok_run,
            )
            self.assertTrue(runtime.path.exists())
            self.assertEqual(runtime.path.name, "llama-completion.exe")
            self.assertIn("build 10983", runtime.version)
            self.assertEqual(calls, [asset_url("llama-%s-bin-win-cpu-x64.zip" % RELEASE_TAG)])

    def test_second_call_reuses_the_cached_binary_without_redownloading(self):
        with tempfile.TemporaryDirectory() as cache_dir:
            calls = []

            def fake_download(url, dest):
                calls.append(url)
                _zip_with_exe(dest, "llama-completion.exe")

            ensure_llama_runtime(cache_dir, system="Windows", machine="AMD64",
                                  download_to=fake_download, run=_ok_run)
            ensure_llama_runtime(cache_dir, system="Windows", machine="AMD64",
                                  download_to=fake_download, run=_ok_run)
            self.assertEqual(len(calls), 1)

    def test_looks_for_llama_completion_not_llama_cli(self):
        """llama-cli is the interactive REPL this addon deliberately avoids (see
        docs/llm-notes.md) -- an archive containing only llama-cli must not be mistaken for
        having the right binary."""
        with tempfile.TemporaryDirectory() as cache_dir:
            def fake_download(url, dest):
                _zip_with_exe(dest, "llama-cli.exe")

            with self.assertRaises(RuntimeVerificationError):
                ensure_llama_runtime(
                    cache_dir, system="Windows", machine="AMD64",
                    download_to=fake_download, run=_ok_run,
                )

    def test_uses_extensionless_binary_name_on_non_windows(self):
        with tempfile.TemporaryDirectory() as cache_dir:
            def fake_download(url, dest):
                src = Path(cache_dir) / "_src_bin"
                src.write_bytes(b"fake binary")
                with tarfile.open(dest, "w:gz") as tf:
                    tf.add(src, arcname="llama/llama-completion")
                src.unlink()

            runtime = ensure_llama_runtime(
                cache_dir, system="Linux", machine="x86_64",
                download_to=fake_download, run=_ok_run,
            )
            self.assertEqual(runtime.path.name, "llama-completion")
            if os.name == "posix":
                mode = os.stat(runtime.path).st_mode
                self.assertTrue(mode & stat.S_IXUSR)

    def test_verification_failure_raises_with_no_silent_success(self):
        with tempfile.TemporaryDirectory() as cache_dir:
            def fake_download(url, dest):
                _zip_with_exe(dest, "llama-completion.exe")

            def silent_run(argv):
                return SubprocessResult(1, "", "")

            with self.assertRaises(RuntimeVerificationError):
                ensure_llama_runtime(
                    cache_dir, system="Windows", machine="AMD64",
                    download_to=fake_download, run=silent_run,
                )

    def test_unsupported_platform_raises_before_any_download(self):
        def fake_download(url, dest):
            raise AssertionError("must not download for an unsupported platform")

        with tempfile.TemporaryDirectory() as cache_dir:
            with self.assertRaises(UnsupportedPlatform):
                ensure_llama_runtime(
                    cache_dir, system="Linux", machine="armv7l",
                    download_to=fake_download, run=_ok_run,
                )


if __name__ == "__main__":
    unittest.main()
