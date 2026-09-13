"""``PiperProvider``: sanitize -> ensure binary/voice -> subprocess synthesis.

The binary is pre-populated on disk in these tests so ``ensure_piper_binary`` takes its
"already cached" path and never needs a real archive to extract -- only the (injected)
``--version`` verification call happens for real. Voice files are stubbed the same way
``ensure_voice`` already covers its own download path in ``test_piper_voice_manager.py``.
"""

from __future__ import annotations

import platform
import tempfile
import unittest
from pathlib import Path

from addon.tts.piper_binary_manager import SubprocessResult
from addon.tts.piper_provider import EmptyTextError, PiperProvider, SynthesisError


def _prepopulate_binary(cache_dir) -> Path:
    root = Path(cache_dir) / "piper"
    root.mkdir(parents=True, exist_ok=True)
    exe_name = "piper.exe" if platform.system().lower() == "windows" else "piper"
    exe_path = root / exe_name
    exe_path.write_bytes(b"stub")
    return exe_path


def _stub_voice_download(url, dest):
    Path(dest).parent.mkdir(parents=True, exist_ok=True)
    Path(dest).write_bytes(b"stub")


class TestPiperProviderSynthesize(unittest.TestCase):
    def test_synthesizes_sanitized_text_with_the_expected_argv(self):
        captured = {}

        def fake_run(argv, input_text=None):
            if "--version" in argv:
                return SubprocessResult(0, "piper 1.2.0", "")
            captured["argv"] = argv
            captured["input_text"] = input_text
            out_path = Path(argv[argv.index("--output_file") + 1])
            out_path.write_bytes(b"RIFF....WAVEfake")
            return SubprocessResult(0, "", "")

        with tempfile.TemporaryDirectory() as cache_dir:
            _prepopulate_binary(cache_dir)
            provider = PiperProvider(
                cache_dir, download_to=_stub_voice_download, run=fake_run
            )
            out = provider.synthesize(
                "processing,&nbsp;management<div>(unlike 加工, a new thing "
                "is not created)</div>",
                voice_id="en_US-lessac-medium",
            )

            self.assertTrue(out.exists())
            self.assertGreater(out.stat().st_size, 0)
            self.assertIn("--model", captured["argv"])
            model_arg = captured["argv"][captured["argv"].index("--model") + 1]
            self.assertIn("en_US-lessac-medium.onnx", model_arg)
            self.assertNotIn("加工", captured["input_text"])
            self.assertNotIn("<div>", captured["input_text"])
            self.assertIn("processing", captured["input_text"])

    def test_empty_after_sanitizing_raises_before_touching_piper_at_all(self):
        def must_not_run(argv, input_text=None):
            raise AssertionError("piper must not be invoked for text that sanitizes to empty")

        with tempfile.TemporaryDirectory() as cache_dir:
            provider = PiperProvider(
                cache_dir, download_to=_stub_voice_download, run=must_not_run
            )
            with self.assertRaises(EmptyTextError):
                provider.synthesize(
                    "<!-- only a comment -->", voice_id="en_US-lessac-medium"
                )

    def test_nonzero_exit_from_piper_raises_synthesis_error(self):
        def fake_run(argv, input_text=None):
            if "--version" in argv:
                return SubprocessResult(0, "piper 1.2.0", "")
            return SubprocessResult(1, "", "model load failed")

        with tempfile.TemporaryDirectory() as cache_dir:
            _prepopulate_binary(cache_dir)
            provider = PiperProvider(
                cache_dir, download_to=_stub_voice_download, run=fake_run
            )
            with self.assertRaises(SynthesisError):
                provider.synthesize("hello there", voice_id="en_US-lessac-medium")

    def test_ensure_ready_downloads_binary_and_voice_without_synthesizing(self):
        calls = []

        def fake_run(argv, input_text=None):
            calls.append(argv)
            assert "--version" in argv, "must not invoke Piper for anything but the check"
            return SubprocessResult(0, "piper 1.2.0", "")

        with tempfile.TemporaryDirectory() as cache_dir:
            _prepopulate_binary(cache_dir)
            provider = PiperProvider(
                cache_dir, download_to=_stub_voice_download, run=fake_run
            )

            provider.ensure_ready("en_US-lessac-medium")

            onnx = Path(cache_dir) / "voices" / "en_US-lessac-medium.onnx"
            config = Path(cache_dir) / "voices" / "en_US-lessac-medium.onnx.json"
            self.assertTrue(onnx.exists())
            self.assertTrue(config.exists())
            self.assertTrue(all("--version" in argv for argv in calls))

    def test_ensure_ready_is_idempotent(self):
        """Safe to call again (e.g. once per voice change) -- must not re-download."""
        with tempfile.TemporaryDirectory() as cache_dir:
            _prepopulate_binary(cache_dir)
            downloads = []

            def counting_download(url, dest):
                downloads.append(url)
                _stub_voice_download(url, dest)

            provider = PiperProvider(
                cache_dir,
                download_to=counting_download,
                run=lambda argv, input_text=None: SubprocessResult(0, "piper 1.2.0", ""),
            )

            provider.ensure_ready("en_US-lessac-medium")
            provider.ensure_ready("en_US-lessac-medium")

            self.assertEqual(len(downloads), 2, "onnx + json, only on the first call")

    def test_missing_output_file_raises_synthesis_error(self):
        """Piper exits 0 but never actually writes the file -- must not be reported as success."""
        def fake_run(argv, input_text=None):
            if "--version" in argv:
                return SubprocessResult(0, "piper 1.2.0", "")
            return SubprocessResult(0, "", "")  # no file written

        with tempfile.TemporaryDirectory() as cache_dir:
            _prepopulate_binary(cache_dir)
            provider = PiperProvider(
                cache_dir, download_to=_stub_voice_download, run=fake_run
            )
            with self.assertRaises(SynthesisError):
                provider.synthesize("hello there", voice_id="en_US-lessac-medium")


if __name__ == "__main__":
    unittest.main()
