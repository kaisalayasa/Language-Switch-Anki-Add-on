"""``llm.analyze``: the orchestration loop (direction -> prompt -> model -> parse -> validate ->
retry -> append generated-audio references) and the trust-star rating computed from how much
trouble that loop had.

``call_model_fn`` is always a canned fake here -- no subprocess, no real model. The direction
resolution itself is real (not mocked), same as ``direction.py``'s own tests, using sample text
unambiguous enough to detect consistently: Spanish sentences for "Word", English for
"Translation", which resolve_direction places as new_front=(Word,), new_back=(Translation,),
audio_targets=(AudioTarget("Word", "ddc-audio-Word"),).
"""

from __future__ import annotations

import unittest

from addon.core.deck_state import ConversionState
from addon.llm.analyze import DeckAnalysis, MAX_ATTEMPTS, analyze_deck, strip_pending_audio_html
from addon.llm.direction import AudioTarget
from addon.llm.prompt import ANALYSIS_MARKER, BACK_MARKER, CSS_MARKER, FRONT_MARKER, FieldSample

FIELDS = [
    FieldSample(name="Word", samples=("Buenos dias amigo", "Hola como estas", "Adios y buena suerte")),
    FieldSample(name="Translation", samples=("Good morning friend", "Hello how are you", "Goodbye and good luck")),
]
QFMT = "{{Translation}}"
AFMT = "{{FrontSide}}<hr id=answer>{{Word}}"


def _response(front, back, css=".card {}", description="d"):
    return (
        '%s\n{"description": "%s"}\n%s\n%s\n%s\n%s\n%s\n%s'
        % (ANALYSIS_MARKER, description, FRONT_MARKER, front, BACK_MARKER, back, CSS_MARKER, css)
    )


# No audio reference here -- the model never writes one any more (see analyze._append_audio_html,
# which adds it after the fact). TestAudioTargetsAreComputedAndAppended checks it actually lands.
_GOOD_FRONT = "<div>{{Word}}</div>"
_GOOD_BACK = "{{FrontSide}}<hr id=answer><div>{{Translation}}</div>"
_GOOD_RESPONSE = _response(_GOOD_FRONT, _GOOD_BACK)

# References a field ("Bogus") that doesn't exist on the notetype -- validate_response rejects
# this with an "unknown_field" problem, matching the real hallucination bug this loop exists to
# catch.
_BAD_RESPONSE = _response("<div>{{Bogus}}</div>", _GOOD_BACK)


class FakeCaller:
    """Returns each of ``responses`` in order, one per call; records every (system, user)
    prompt pair it was actually called with."""

    def __init__(self, responses):
        self._responses = list(responses)
        self.calls = []

    def __call__(self, system_prompt, user_prompt):
        self.calls.append((system_prompt, user_prompt))
        return self._responses[len(self.calls) - 1]


def _analyze(responses, **overrides):
    kwargs = dict(
        deck_name="Test Deck", notetype_name="Basic", fields=FIELDS, qfmt=QFMT, afmt=AFMT,
        css=".card { font-size: 20px; }",
    )
    kwargs.update(overrides)
    caller = FakeCaller(responses)
    result = analyze_deck(call_model_fn=caller, **kwargs)
    return result, caller


class TestDirectionIsWiredIn(unittest.TestCase):
    def test_placement_and_languages_come_from_real_direction_resolution(self):
        result, _ = _analyze([_GOOD_RESPONSE])
        self.assertEqual(result.target_language, "es")
        self.assertEqual(result.native_language, "en")

    def test_the_given_field_placement_is_exposed_not_just_used_internally(self):
        """A UI showing "what was decided" needs the given placement itself, not just
        whatever fields happen to appear in the model's returned HTML -- those can
        legitimately differ (e.g. an omitted bookkeeping field)."""
        result, _ = _analyze([_GOOD_RESPONSE])
        self.assertEqual(result.new_front_fields, ("Word",))
        self.assertEqual(result.new_back_fields, ("Translation",))


class TestAudioTargetsAreComputedAndAppended(unittest.TestCase):
    """Which fields get audio, and their names, is no longer something the model decides or
    writes -- see llm/direction.py. This is the other half of that: analyze_deck exposes the
    computed targets and appends their references to the front it returns."""

    def test_audio_targets_reflects_the_given_placement(self):
        result, _ = _analyze([_GOOD_RESPONSE])
        self.assertEqual(result.audio_targets, (AudioTarget("Word", "ddc-audio-Word"),))

    def test_the_audio_reference_is_appended_not_written_by_the_model(self):
        """_GOOD_RESPONSE's front has no audio reference in it at all -- if one appears in the
        result, analyze_deck put it there itself."""
        result, _ = _analyze([_GOOD_RESPONSE])
        self.assertIn("{{#ddc-audio-Word}}{{ddc-audio-Word}}{{/ddc-audio-Word}}", result.front)
        self.assertIn("<div>{{Word}}</div>", result.front)

    def test_the_audio_reference_is_appended_even_on_total_failure(self):
        """The last attempt's own (still-broken) output is never silently discarded -- and
        that includes still getting the deterministic audio reference appended, since that
        part was never in question regardless of what the model got wrong."""
        result, _ = _analyze([_BAD_RESPONSE, _BAD_RESPONSE, _BAD_RESPONSE])
        self.assertIn("{{#ddc-audio-Word}}{{ddc-audio-Word}}{{/ddc-audio-Word}}", result.front)


class TestStripPendingAudioHtml(unittest.TestCase):
    """The inverse of _append_audio_html -- for previewing a Front before Convert has created
    the audio field(s) it references. See ui/main_screen.py's _preview_front."""

    def test_removes_the_exact_block_append_audio_html_would_add(self):
        front = "<div>{{Word}}</div>\n{{#ddc-audio-Word}}{{ddc-audio-Word}}{{/ddc-audio-Word}}"
        self.assertEqual(
            strip_pending_audio_html(front, ["ddc-audio-Word"]), "<div>{{Word}}</div>\n"
        )

    def test_removes_only_the_named_pending_fields_not_every_audio_block(self):
        """A field already on the live notetype (e.g. re-previewing an already-converted pair)
        is not passed in -- its block must survive untouched."""
        front = (
            "<div>{{Word}}</div>"
            "{{#ddc-audio-Word}}{{ddc-audio-Word}}{{/ddc-audio-Word}}"
            "{{#ddc-audio-Sentence}}{{ddc-audio-Sentence}}{{/ddc-audio-Sentence}}"
        )
        result = strip_pending_audio_html(front, ["ddc-audio-Word"])
        self.assertNotIn("ddc-audio-Word", result)
        self.assertIn("{{#ddc-audio-Sentence}}{{ddc-audio-Sentence}}{{/ddc-audio-Sentence}}", result)

    def test_no_pending_fields_is_a_no_op(self):
        front = "<div>{{Word}}</div>"
        self.assertEqual(strip_pending_audio_html(front, []), front)

    def test_a_field_not_present_in_front_at_all_is_a_no_op(self):
        front = "<div>{{Word}}</div>"
        self.assertEqual(strip_pending_audio_html(front, ["ddc-audio-Word"]), front)


class TestKnownStateIsThreadedThrough(unittest.TestCase):
    """``known_state`` (this notetype's recorded conversion state, when re-analyzing one
    already converted) must actually reach ``resolve_direction``, not just be accepted and
    dropped -- see ``test_llm_direction.py`` for what goes wrong without it."""

    def test_known_state_overrides_the_structurally_derived_direction(self):
        """This fixture's own qfmt/afmt structurally resolve to target=es/native=en (see
        TestDirectionIsWiredIn) -- a known_state claiming the deck was already converted the
        other way around must win instead."""
        known_state = ConversionState(target_language="en", native_language="es")
        flipped_front = "<div>{{Translation}}</div>"
        flipped_back = "{{FrontSide}}<hr id=answer><div>{{Word}}</div>"
        response = _response(flipped_front, flipped_back)

        result, caller = _analyze([response], known_state=known_state)

        self.assertEqual(len(caller.calls), 1)
        self.assertEqual(result.target_language, "en")
        self.assertEqual(result.native_language, "es")
        self.assertEqual(result.new_front_fields, ("Translation",))
        self.assertEqual(result.new_back_fields, ("Word",))

    def test_omitting_known_state_keeps_the_original_structural_behavior(self):
        result, _ = _analyze([_GOOD_RESPONSE])
        self.assertEqual(result.target_language, "es")
        self.assertEqual(result.native_language, "en")


class TestTrustStars(unittest.TestCase):
    def test_clean_pass_on_first_attempt_is_five_stars_with_no_review_message(self):
        result, caller = _analyze([_GOOD_RESPONSE])
        self.assertEqual(result.trust_stars, 5)
        self.assertEqual(result.attempts_used, 1)
        self.assertIsNone(result.review_message)
        self.assertEqual(result.unresolved_problems, ())
        self.assertEqual(len(caller.calls), 1)

    def test_pass_after_one_retry_is_four_stars_with_a_soft_review_message(self):
        result, caller = _analyze([_BAD_RESPONSE, _GOOD_RESPONSE])
        self.assertEqual(result.trust_stars, 4)
        self.assertEqual(result.attempts_used, 2)
        self.assertIsNotNone(result.review_message)
        self.assertIn("1 retry", result.review_message)
        self.assertEqual(result.unresolved_problems, ())
        self.assertEqual(len(caller.calls), 2)

    def test_pass_only_on_the_final_allowed_attempt_is_three_stars(self):
        result, caller = _analyze([_BAD_RESPONSE, _BAD_RESPONSE, _GOOD_RESPONSE])
        self.assertEqual(result.trust_stars, 3)
        self.assertEqual(result.attempts_used, MAX_ATTEMPTS)
        self.assertEqual(len(caller.calls), 3)

    def test_never_passing_is_one_star_but_still_returns_the_last_attempt(self):
        result, caller = _analyze([_BAD_RESPONSE, _BAD_RESPONSE, _BAD_RESPONSE])
        self.assertEqual(result.trust_stars, 1)
        self.assertEqual(result.attempts_used, MAX_ATTEMPTS)
        self.assertEqual(len(caller.calls), MAX_ATTEMPTS)
        self.assertNotEqual(result.unresolved_problems, ())
        # The model's last (still-broken) output is preserved, never silently discarded.
        self.assertIn("{{Bogus}}", result.front)
        self.assertIn("could not produce a fully valid result", result.review_message)

    def test_a_success_after_max_attempts_uses_a_less_alarming_message_than_total_failure(self):
        passed, _ = _analyze([_BAD_RESPONSE, _BAD_RESPONSE, _GOOD_RESPONSE])
        failed, _ = _analyze([_BAD_RESPONSE, _BAD_RESPONSE, _BAD_RESPONSE])
        self.assertNotEqual(passed.review_message, failed.review_message)
        self.assertIn("passed every check", passed.review_message)


class TestParseFailureHandling(unittest.TestCase):
    def test_unparseable_response_is_retried_with_a_format_reminder(self):
        result, caller = _analyze(["not the right format at all", _GOOD_RESPONSE])
        self.assertEqual(result.trust_stars, 4)
        self.assertEqual(len(caller.calls), 2)
        # The retry prompt must tell the model what went wrong.
        self.assertIn("did not follow the required output format", caller.calls[1][1])

    def test_total_parse_failure_returns_empty_templates_but_keeps_the_raw_response(self):
        result, _ = _analyze(["garbage"] * MAX_ATTEMPTS)
        self.assertEqual(result.trust_stars, 1)
        self.assertEqual(result.front, "")
        self.assertEqual(result.back, "")
        self.assertEqual(result.last_raw_response, "garbage")
        self.assertTrue(any(p.code == "parse_error" for p in result.unresolved_problems))


class TestRetryPromptContent(unittest.TestCase):
    def test_retry_prompt_names_the_specific_validation_problem(self):
        _, caller = _analyze([_BAD_RESPONSE, _GOOD_RESPONSE])
        retry_user_prompt = caller.calls[1][1]
        self.assertIn("Bogus", retry_user_prompt)
        self.assertIn("do not exist on this notetype", retry_user_prompt)

    def test_first_call_uses_the_plain_prompt_with_no_retry_text(self):
        _, caller = _analyze([_GOOD_RESPONSE])
        first_user_prompt = caller.calls[0][1]
        self.assertNotIn("previous attempt", first_user_prompt)


class TestAudioSafetyIsAppliedBeforeValidation(unittest.TestCase):
    def test_a_bare_reference_to_a_field_with_pre_existing_audio_is_auto_fixed_not_retried(self):
        fields_with_audio = [
            FieldSample(name="Word", samples=("Hola [sound:x.mp3]", "Adios [sound:y.mp3]")),
            FieldSample(name="Translation", samples=("Hello there friend", "Goodbye my friend")),
        ]
        # References the audio-bearing field as a bare {{Word}} -- the model's real observed
        # failure mode. Should be silently corrected to {{text:Word}} by the backstop, not
        # trigger a retry, since enforce_audio_safety guarantees the invariant before
        # validate_response ever runs.
        response = _response("<div>{{Word}}</div>", _GOOD_BACK)
        result, caller = _analyze([response], fields=fields_with_audio)
        self.assertEqual(len(caller.calls), 1)
        self.assertEqual(result.trust_stars, 5)
        self.assertIn("{{text:Word}}", result.front)
        self.assertNotIn("{{Word}}}", result.front)


if __name__ == "__main__":
    unittest.main()
