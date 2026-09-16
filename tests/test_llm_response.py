"""``llm.response``: splitting the model's raw reply text into its declared sections.

Canned response strings only -- no model, no subprocess. Content-level defects (a hallucinated
field, an unbalanced conditional, a placement violation) are deliberately out of scope here; this
module only answers whether the reply has the shape `prompt.py` asked for.
"""

from __future__ import annotations

import unittest

from addon.llm.prompt import ANALYSIS_MARKER, BACK_MARKER, CSS_MARKER, FRONT_MARKER
from addon.llm.response import ParsedResponse, ResponseParseError, parse_response


def _make_response(description="A test deck.", front="<div>{{Word}}</div>",
                    back="{{FrontSide}}<hr id=answer>", css=".card { font-size: 20px; }"):
    return (
        "%s\n"
        '{\n  "description": "%s"\n}\n'
        "%s\n%s\n%s\n%s\n%s\n%s"
    ) % (ANALYSIS_MARKER, description, FRONT_MARKER, front, BACK_MARKER, back, CSS_MARKER, css)


class TestParseResponseHappyPath(unittest.TestCase):
    def test_extracts_all_four_sections(self):
        text = _make_response()
        result = parse_response(text)
        self.assertEqual(result, ParsedResponse(
            description="A test deck.",
            front="<div>{{Word}}</div>",
            back="{{FrontSide}}<hr id=answer>",
            css=".card { font-size: 20px; }",
        ))

    def test_tolerates_a_preamble_before_the_first_marker(self):
        """The model was told not to write anything before ANALYSIS but sometimes does anyway
        -- harmless as long as the real markers still follow, so this is discarded, not
        rejected."""
        text = "Sure, here is the conversion:\n\n" + _make_response()
        result = parse_response(text)
        self.assertEqual(result.description, "A test deck.")

    def test_extra_keys_in_analysis_are_ignored_not_rejected(self):
        """Older prompt versions asked for speak_text_from/target_language/native_language/
        write_audio_to; if a model ever emits one of these anyway (e.g. from stale fine-tuning
        bias), that's harmless noise, not a parse failure."""
        text = (
            "%s\n"
            '{"description": "d", "speak_text_from": "Word", "target_language": "English"}\n'
            "%s\nfront\n%s\nback\n%s\ncss"
        ) % (ANALYSIS_MARKER, FRONT_MARKER, BACK_MARKER, CSS_MARKER)
        result = parse_response(text)
        self.assertEqual(result.description, "d")

    def test_multiline_front_back_css_are_preserved(self):
        front = "<div>line one</div>\n<div>line two</div>"
        back = "{{FrontSide}}<hr id=answer>\n<div>answer</div>"
        css = ".card { font-size: 20px; }\n.word { font-size: 32px; }"
        text = _make_response(front=front, back=back, css=css)
        result = parse_response(text)
        self.assertEqual(result.front, front)
        self.assertEqual(result.back, back)
        self.assertEqual(result.css, css)


class TestParseResponseFailureModes(unittest.TestCase):
    def test_missing_front_marker_raises(self):
        text = "%s\n{}\n%s\nback\n%s\ncss" % (ANALYSIS_MARKER, BACK_MARKER, CSS_MARKER)
        with self.assertRaises(ResponseParseError):
            parse_response(text)

    def test_missing_css_marker_raises(self):
        text = "%s\n{}\n%s\nfront\n%s\nback" % (ANALYSIS_MARKER, FRONT_MARKER, BACK_MARKER)
        with self.assertRaises(ResponseParseError):
            parse_response(text)

    def test_sections_out_of_order_raises(self):
        """A real observed failure mode: the model wrote FRONT before ANALYSIS."""
        text = "%s\nfront\n%s\n{}\n%s\nback\n%s\ncss" % (
            FRONT_MARKER, ANALYSIS_MARKER, BACK_MARKER, CSS_MARKER
        )
        with self.assertRaises(ResponseParseError):
            parse_response(text)

    def test_malformed_json_in_analysis_raises(self):
        text = "%s\nthis is not json\n%s\nfront\n%s\nback\n%s\ncss" % (
            ANALYSIS_MARKER, FRONT_MARKER, BACK_MARKER, CSS_MARKER
        )
        with self.assertRaises(ResponseParseError):
            parse_response(text)

    def test_analysis_missing_description_raises(self):
        text = '%s\n{}\n%s\nfront\n%s\nback\n%s\ncss' % (
            ANALYSIS_MARKER, FRONT_MARKER, BACK_MARKER, CSS_MARKER
        )
        with self.assertRaises(ResponseParseError):
            parse_response(text)

    def test_analysis_that_is_a_json_list_not_object_raises(self):
        text = '%s\n["d"]\n%s\nfront\n%s\nback\n%s\ncss' % (
            ANALYSIS_MARKER, FRONT_MARKER, BACK_MARKER, CSS_MARKER
        )
        with self.assertRaises(ResponseParseError):
            parse_response(text)

    def test_completely_unstructured_reply_raises(self):
        with self.assertRaises(ResponseParseError):
            parse_response("I'm not sure how to convert this deck.")

    def test_empty_reply_raises(self):
        with self.assertRaises(ResponseParseError):
            parse_response("")


if __name__ == "__main__":
    unittest.main()
