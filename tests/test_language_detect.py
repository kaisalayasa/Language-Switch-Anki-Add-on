"""Language detection: Unicode-script fast pass, langdetect fallback, and the aggregation
that keeps a single short/noisy sample from dominating a field's guess.

Real vendored ``langdetect`` is exercised here (not mocked) -- it's pure Python, offline,
and deterministic once ``DetectorFactory.seed`` is set (done at import time in
``language_detect.py``), so there's no reason to fake it out."""

from __future__ import annotations

import unittest

from addon.core.language_detect import LanguageGuess, detect_field_language, detect_language

# Real sentences long enough for langdetect's n-gram model to have something to work with.
ENGLISH = "The quick brown fox jumps over the lazy dog."
FRENCH = "Le renard brun rapide saute par-dessus le chien paresseux."

JAPANESE_HIRAGANA = "ひらがな"
JAPANESE_KATAKANA = "カタカナ"
JAPANESE_KANJI = "日本語"
KOREAN_HANGUL = "안녕하세요"
RUSSIAN_CYRILLIC = "Привет, как дела?"


class TestScriptFastPass(unittest.TestCase):
    def test_hiragana_is_japanese(self):
        self.assertEqual(detect_language(JAPANESE_HIRAGANA), LanguageGuess("ja", "script"))

    def test_katakana_is_japanese(self):
        self.assertEqual(detect_language(JAPANESE_KATAKANA), LanguageGuess("ja", "script"))

    def test_kanji_is_japanese(self):
        self.assertEqual(detect_language(JAPANESE_KANJI), LanguageGuess("ja", "script"))

    def test_hangul_is_korean(self):
        self.assertEqual(detect_language(KOREAN_HANGUL), LanguageGuess("ko", "script"))

    def test_cyrillic_is_russian(self):
        self.assertEqual(detect_language(RUSSIAN_CYRILLIC), LanguageGuess("ru", "script"))

    def test_script_pass_wins_even_with_latin_mixed_in(self):
        # Real Core 2000 data embeds Japanese mid-English-gloss (see claude.md); the script
        # pass should still catch the Japanese characters present in the text.
        mixed = "processing (unlike 加工, a new thing is not created)"
        self.assertEqual(detect_language(mixed).code, "ja")
        self.assertEqual(detect_language(mixed).confidence, "script")


class TestLangdetectFallback(unittest.TestCase):
    def test_english_sentence(self):
        guess = detect_language(ENGLISH)
        self.assertEqual(guess.code, "en")
        self.assertEqual(guess.confidence, "langdetect")
        self.assertIsNotNone(guess.probability)
        self.assertGreater(guess.probability, 0.9)

    def test_french_sentence(self):
        guess = detect_language(FRENCH)
        self.assertEqual(guess.code, "fr")
        self.assertEqual(guess.confidence, "langdetect")


class TestNoUsableSignal(unittest.TestCase):
    def test_empty_string(self):
        self.assertEqual(detect_language(""), LanguageGuess(None, "unknown"))

    def test_whitespace_only(self):
        self.assertEqual(detect_language("   \n\t "), LanguageGuess(None, "unknown"))

    def test_digits_only(self):
        # Confirmed empirically: langdetect raises LangDetectException("No features in
        # text.") for this -- must be caught, not left to propagate.
        self.assertEqual(detect_language("1983"), LanguageGuess(None, "unknown"))

    def test_punctuation_only(self):
        self.assertEqual(detect_language("!!!"), LanguageGuess(None, "unknown"))

    def test_single_character_is_too_short_to_trust(self):
        # Confirmed empirically: a bare "a" makes langdetect confidently guess "tl"
        # (Tagalog) -- wrong and misleadingly confident. The length guard must catch this
        # before it ever reaches langdetect.
        self.assertEqual(detect_language("a"), LanguageGuess(None, "unknown"))


class TestFieldAggregation(unittest.TestCase):
    def test_majority_code_wins(self):
        samples = [ENGLISH, "Another perfectly ordinary English sentence to read aloud.", "a"]
        guess = detect_field_language(samples)
        self.assertEqual(guess.code, "en")

    def test_single_noisy_sample_does_not_dominate(self):
        # Two confident English samples plus one unrelated single-letter noise sample --
        # the noise sample doesn't even count as a vote, since it resolves to "unknown".
        samples = ["a", ENGLISH, "Another perfectly ordinary English sentence to read aloud."]
        guess = detect_field_language(samples)
        self.assertEqual(guess.code, "en")

    def test_all_unknown_samples_yield_unknown(self):
        guess = detect_field_language(["", "123", "!!!"])
        self.assertEqual(guess, LanguageGuess(None, "unknown"))

    def test_empty_sample_list_yields_unknown(self):
        self.assertEqual(detect_field_language([]), LanguageGuess(None, "unknown"))

    def test_script_detected_fields_are_still_aggregated(self):
        guess = detect_field_language([JAPANESE_KANJI, JAPANESE_HIRAGANA, "123"])
        self.assertEqual(guess.code, "ja")
        self.assertEqual(guess.confidence, "script")


if __name__ == "__main__":
    unittest.main()
