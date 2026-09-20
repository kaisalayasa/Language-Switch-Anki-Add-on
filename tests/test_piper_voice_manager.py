"""``piper_voice_manager``: curated voice list, HuggingFace URL construction, caching.

The URL pattern under test was verified against the real HuggingFace repo (the
``en_US-ljspeech-high.onnx`` file genuinely exists at this path) -- see ``docs/api-notes.md``.
"""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from addon.tts.piper_voice_manager import (
    CURATED_VOICES,
    UnknownVoice,
    ensure_voice,
    find_voice,
    voice_urls,
)


class TestCuratedVoices(unittest.TestCase):
    def test_m2_ships_exactly_the_two_agreed_defaults(self):
        ids = [spec.voice_id for spec in CURATED_VOICES]
        self.assertEqual(ids, ["en_US-ljspeech-high", "en_GB-alba-medium"])

    def test_find_voice_returns_the_matching_spec(self):
        spec = find_voice("en_GB-alba-medium")
        self.assertEqual(spec.locale, "en_GB")
        self.assertEqual(spec.name, "alba")
        self.assertEqual(spec.quality, "medium")

    def test_find_voice_unknown_raises_and_lists_known_voices(self):
        with self.assertRaises(UnknownVoice) as ctx:
            find_voice("fr_FR-not-curated")
        self.assertIn("en_US-ljspeech-high", str(ctx.exception))

    def test_voice_urls_match_the_verified_huggingface_layout(self):
        spec = find_voice("en_US-ljspeech-high")
        onnx_url, config_url = voice_urls(spec)
        self.assertEqual(
            onnx_url,
            "https://huggingface.co/rhasspy/piper-voices/resolve/main/"
            "en/en_US/ljspeech/high/en_US-ljspeech-high.onnx",
        )
        self.assertEqual(config_url, onnx_url + ".json")


class TestEnsureVoice(unittest.TestCase):
    def _fake_download(self, calls):
        def download(url, dest):
            calls.append(url)
            Path(dest).write_bytes(b"stub")
        return download

    def test_downloads_both_files_on_first_use(self):
        with tempfile.TemporaryDirectory() as cache_dir:
            calls = []
            files = ensure_voice(
                cache_dir, "en_US-ljspeech-high", download_to=self._fake_download(calls)
            )
            self.assertTrue(files.onnx_path.exists())
            self.assertTrue(files.config_path.exists())
            self.assertEqual(len(calls), 2)

    def test_second_call_is_a_cache_hit_no_redownload(self):
        with tempfile.TemporaryDirectory() as cache_dir:
            calls = []
            ensure_voice(cache_dir, "en_US-ljspeech-high", download_to=self._fake_download(calls))
            ensure_voice(cache_dir, "en_US-ljspeech-high", download_to=self._fake_download(calls))
            self.assertEqual(len(calls), 2)  # still 2, not 4

    def test_unknown_voice_raises_before_any_download(self):
        def must_not_be_called(url, dest):
            raise AssertionError("must not download for an unknown voice")

        with tempfile.TemporaryDirectory() as cache_dir:
            with self.assertRaises(UnknownVoice):
                ensure_voice(cache_dir, "not-a-real-voice", download_to=must_not_be_called)


if __name__ == "__main__":
    unittest.main()
