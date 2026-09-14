"""Picking the character allowlist from the voice instead of assuming Latin.

The bug these cover: every call site built its provider without saying which script it was
synthesizing, and the default was a Latin range list. For any non-Latin language the
sanitizer then deleted the text character by character, leaving nothing to speak -- which is
indistinguishable from an empty field, so a whole deck could process without a single error
and produce no audio at all.
"""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from addon.tts.piper_binary_manager import SubprocessResult
from addon.tts.piper_provider import EmptyTextError, PiperProvider
from addon.tts.piper_voice_manager import UnknownVoice
from addon.tts.sanitize import LATIN_RANGES, sanitize_text
from addon.tts.script_ranges import language_of_voice, ranges_for_language, ranges_for_voice

from tests.test_piper_provider import _prepopulate_binary, _stub_voice_download


class TestVoiceIdParsing(unittest.TestCase):
    def test_the_language_comes_off_the_front_of_a_real_voice_id(self):
        self.assertEqual(language_of_voice("en_US-lessac-medium"), "en")
        self.assertEqual(language_of_voice("en_GB-alba-medium"), "en")
        self.assertEqual(language_of_voice("ko_KR-kss-medium"), "ko")
        self.assertEqual(language_of_voice("ja_JP-test-medium"), "ja")

    def test_nonsense_is_reported_as_unknown_rather_than_guessed_at(self):
        for voice in ("", "   ", "123-nope"):
            with self.subTest(voice=voice):
                self.assertEqual(language_of_voice(voice), "")


class TestRanges(unittest.TestCase):
    def test_a_latin_language_filters_to_latin(self):
        self.assertEqual(ranges_for_language("en"), LATIN_RANGES)

    def test_a_non_latin_language_gets_its_own_script(self):
        for code in ("ko", "ja", "ru", "el", "he", "th", "ar", "hi"):
            with self.subTest(code=code):
                self.assertTrue(ranges_for_language(code))
                self.assertNotEqual(ranges_for_language(code), LATIN_RANGES)

    def test_an_unknown_language_filters_nothing_rather_than_guessing(self):
        """Letting a few odd characters through is a far smaller failure than deleting
        every character of a script this table doesn't list yet."""
        for code in ("", "zz", "klingon", None):
            with self.subTest(code=code):
                self.assertEqual(ranges_for_language(code), ())

    def test_a_full_locale_falls_back_to_its_language(self):
        self.assertEqual(ranges_for_language("en_US"), LATIN_RANGES)
        self.assertEqual(ranges_for_language("ko-KR"), ranges_for_language("ko"))


class TestTextSurvivesTheRightVoice(unittest.TestCase):
    KOREAN = "저는 매일 아침에 물을 마십니다."
    ENGLISH = "I drink a glass of water every morning."

    def test_korean_survives_a_korean_voice(self):
        cleaned = sanitize_text(self.KOREAN, allowed_ranges=ranges_for_voice("ko_KR-kss-medium"))
        self.assertTrue(cleaned.strip())
        self.assertIn("매일", cleaned)

    def test_korean_is_wiped_out_by_a_latin_allowlist(self):
        """The old default, reproduced deliberately so the regression stays visible."""
        self.assertEqual(sanitize_text(self.KOREAN, allowed_ranges=LATIN_RANGES).strip(), "")

    def test_english_survives_an_english_voice(self):
        cleaned = sanitize_text(self.ENGLISH, allowed_ranges=ranges_for_voice("en_GB-alba-medium"))
        self.assertIn("water", cleaned)

    def test_an_english_voice_still_strips_embedded_other_script_text(self):
        """Why filtering exists at all: real glosses mix in the other language mid-line,
        and that makes a voice stumble over the whole sentence."""
        cleaned = sanitize_text(
            "processing (unlike 加工, a new thing is not created)",
            allowed_ranges=ranges_for_voice("en_US-lessac-medium"),
        )
        self.assertIn("processing", cleaned)
        self.assertNotIn("加工", cleaned)


class TestProviderPicksRangesFromTheVoice(unittest.TestCase):
    """The provider is where the fix actually lands: no call site has to remember to pass
    a script, so none of them can forget and silently get Latin.

    Binary and voice are stubbed exactly as in ``test_piper_provider.py`` -- nothing here
    downloads anything or runs a real subprocess.
    """

    KOREAN = "저는 매일 물을 마십니다."

    def _synthesize(self, text, voice_id, **kw):
        spoken = {}

        def fake_run(argv, input_text=None):
            if "--version" in argv:
                return SubprocessResult(0, "piper 1.2.0", "")
            spoken["text"] = input_text
            Path(argv[argv.index("--output_file") + 1]).write_bytes(b"RIFF....WAVEfake")
            return SubprocessResult(0, "", "")

        with tempfile.TemporaryDirectory() as cache_dir:
            _prepopulate_binary(cache_dir)
            provider = PiperProvider(
                cache_dir, download_to=_stub_voice_download, run=fake_run, **kw
            )
            provider.synthesize(text, voice_id=voice_id)
        return spoken.get("text")

    def test_korean_gets_past_the_sanitizer_under_a_korean_voice(self):
        """Asserted by which error comes back. The curated voice list is English-only for
        now, so a Korean voice id can't be resolved -- but reaching *that* complaint proves
        the text survived sanitizing, which under the old Latin default it did not.
        """
        with self.assertRaises(UnknownVoice):
            self._synthesize(self.KOREAN, "ko_KR-kss-medium")

    def test_text_in_the_wrong_script_for_the_voice_is_reported_not_mangled(self):
        """Not merely "produces poor audio": under a Latin allowlist the Korean letters are
        all dropped and only the full stop survives, so without this the synthesizer would
        be handed "." and would write a meaningless file that counts as this note's audio."""
        with self.assertRaises(EmptyTextError):
            self._synthesize(self.KOREAN, "en_GB-alba-medium")

    def test_the_error_names_the_voice_as_a_possible_cause(self):
        with self.assertRaises(EmptyTextError) as caught:
            self._synthesize(self.KOREAN, "en_GB-alba-medium")
        self.assertIn("en_GB-alba-medium", str(caught.exception))

    def test_an_explicit_range_list_still_wins(self):
        with self.assertRaises(EmptyTextError):
            self._synthesize(self.KOREAN, "ko_KR-kss-medium", allowed_ranges=LATIN_RANGES)

    def test_a_genuinely_empty_field_is_still_empty(self):
        with self.assertRaises(EmptyTextError):
            self._synthesize("   <br>  ", "en_GB-alba-medium")

    def test_an_english_voice_speaks_english_text_unchanged(self):
        spoken = self._synthesize("I drink water every morning.", "en_GB-alba-medium")
        self.assertIn("water", spoken)

    def test_a_voice_whose_language_is_unlisted_filters_nothing(self):
        """No table lists every script. Not filtering is the safe way to be wrong."""
        unfiltered = sanitize_text(
            "Ĳsselmeer 42 물", allowed_ranges=ranges_for_voice("zz_ZZ-unknown-medium")
        )
        self.assertIn("물", unfiltered)
        self.assertIn("42", unfiltered)


if __name__ == "__main__":
    unittest.main()
