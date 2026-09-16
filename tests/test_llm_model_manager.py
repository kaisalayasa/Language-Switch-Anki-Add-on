"""``llm.model_manager``: caching and size-verifying the split Qwen2.5-7B-Instruct GGUF files.

All downloads are injected fakes -- these tests never touch the real HuggingFace repo. The
byte sizes asserted against here were verified against the real HuggingFace API this session;
see ``docs/llm-notes.md``.

Tests that need a *fully successful* round-trip patch ``MODEL_FILES`` down to a couple of
tiny fake sizes rather than materializing the real multi-gigabyte files on disk -- on Windows/
NTFS, extending a file to gigabyte size (even via truncate, with no actual content written)
is not the free/instant sparse-file operation it is on Linux, and was observed taking over a
minute across this file before being fixed. Tests that only need a *wrong*-size file (the
failure-mode tests) already use small sizes and were never affected.
"""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from addon.llm.model_manager import (
    MODEL_FILES,
    ModelFile,
    ModelVerificationError,
    ensure_model,
    model_is_cached,
    model_urls,
)

_TINY_FILES = (
    ModelFile("fake-model-00001-of-00002.gguf", 37),
    ModelFile("fake-model-00002-of-00002.gguf", 11),
)


class TestModelUrls(unittest.TestCase):
    def test_urls_point_at_the_verified_repo_and_filenames(self):
        urls = model_urls()
        self.assertEqual(len(urls), 2)
        for url, spec in zip(urls, MODEL_FILES):
            self.assertEqual(
                url,
                "https://huggingface.co/Qwen/Qwen2.5-7B-Instruct-GGUF/resolve/main/%s"
                % spec.filename,
            )


class TestEnsureModel(unittest.TestCase):
    @patch("addon.llm.model_manager.MODEL_FILES", _TINY_FILES)
    def test_downloads_both_split_files_and_returns_them_in_order(self):
        with tempfile.TemporaryDirectory() as cache_dir:
            calls = []

            def fake_download(url, dest):
                calls.append(url)
                spec = next(s for s in _TINY_FILES if s.filename == dest.name)
                dest.write_bytes(b"x" * spec.size_bytes)

            result = ensure_model(cache_dir, download_to=fake_download)

            self.assertEqual(len(calls), 2)
            self.assertEqual(result.primary_path.name, _TINY_FILES[0].filename)
            self.assertEqual([p.name for p in result.all_paths],
                              [spec.filename for spec in _TINY_FILES])
            for path, spec in zip(result.all_paths, _TINY_FILES):
                self.assertEqual(path.stat().st_size, spec.size_bytes)

    @patch("addon.llm.model_manager.MODEL_FILES", _TINY_FILES)
    def test_second_call_reuses_the_cache_without_redownloading(self):
        with tempfile.TemporaryDirectory() as cache_dir:
            calls = []

            def fake_download(url, dest):
                calls.append(url)
                spec = next(s for s in _TINY_FILES if s.filename == dest.name)
                dest.write_bytes(b"x" * spec.size_bytes)

            ensure_model(cache_dir, download_to=fake_download)
            ensure_model(cache_dir, download_to=fake_download)
            self.assertEqual(len(calls), 2)  # only the first call's two downloads

    @patch("addon.llm.model_manager.MODEL_FILES", _TINY_FILES)
    def test_a_truncated_cached_file_is_caught_not_silently_trusted(self):
        """The whole reason verification runs on every call, cache-hit included: a file left
        over from an interrupted previous download must never be silently used to run the
        model."""
        with tempfile.TemporaryDirectory() as cache_dir:
            model_dir = Path(cache_dir) / "model"
            model_dir.mkdir(parents=True)
            (model_dir / _TINY_FILES[0].filename).write_bytes(b"x" * 3)  # truncated
            (model_dir / _TINY_FILES[1].filename).write_bytes(b"x" * _TINY_FILES[1].size_bytes)

            def fail_download(url, dest):
                raise AssertionError("must not re-download a file that already exists on disk")

            with self.assertRaises(ModelVerificationError):
                ensure_model(cache_dir, download_to=fail_download)

    def test_a_freshly_downloaded_file_with_wrong_size_is_caught(self):
        with tempfile.TemporaryDirectory() as cache_dir:
            def bad_download(url, dest):
                dest.write_bytes(b"\0" * 10)  # wrong size regardless of which file

            with self.assertRaises(ModelVerificationError):
                ensure_model(cache_dir, download_to=bad_download)


class TestModelIsCached(unittest.TestCase):
    """Purely for UI messaging (e.g. "downloading now" vs. "already have it") -- a cheap size
    check, same rule ensure_model itself applies, just without downloading anything missing."""

    @patch("addon.llm.model_manager.MODEL_FILES", _TINY_FILES)
    def test_false_when_nothing_downloaded_yet(self):
        with tempfile.TemporaryDirectory() as cache_dir:
            self.assertFalse(model_is_cached(cache_dir))

    @patch("addon.llm.model_manager.MODEL_FILES", _TINY_FILES)
    def test_true_once_every_file_is_present_at_the_right_size(self):
        with tempfile.TemporaryDirectory() as cache_dir:
            def fake_download(url, dest):
                spec = next(s for s in _TINY_FILES if s.filename == dest.name)
                dest.write_bytes(b"x" * spec.size_bytes)

            ensure_model(cache_dir, download_to=fake_download)

            self.assertTrue(model_is_cached(cache_dir))

    @patch("addon.llm.model_manager.MODEL_FILES", _TINY_FILES)
    def test_false_when_a_file_is_present_but_truncated(self):
        with tempfile.TemporaryDirectory() as cache_dir:
            model_dir = Path(cache_dir) / "model"
            model_dir.mkdir(parents=True)
            (model_dir / _TINY_FILES[0].filename).write_bytes(b"x" * 3)  # truncated
            (model_dir / _TINY_FILES[1].filename).write_bytes(b"x" * _TINY_FILES[1].size_bytes)

            self.assertFalse(model_is_cached(cache_dir))

    @patch("addon.llm.model_manager.MODEL_FILES", _TINY_FILES)
    def test_false_when_only_some_files_are_present(self):
        with tempfile.TemporaryDirectory() as cache_dir:
            model_dir = Path(cache_dir) / "model"
            model_dir.mkdir(parents=True)
            (model_dir / _TINY_FILES[0].filename).write_bytes(b"x" * _TINY_FILES[0].size_bytes)

            self.assertFalse(model_is_cached(cache_dir))


if __name__ == "__main__":
    unittest.main()
