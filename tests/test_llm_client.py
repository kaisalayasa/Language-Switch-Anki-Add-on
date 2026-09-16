"""``llm.client``: the subprocess invocation and stdout parsing, in isolation from any real
llama-completion.exe binary or model file.

All subprocess calls are injected fakes -- these tests never spawn a real process. The stdout
shape asserted against here was verified against the real binary; see ``docs/llm-notes.md``.
"""

from __future__ import annotations

import unittest
from pathlib import Path

from addon.llm.client import ModelCallError, SubprocessResult, call_model

RUNTIME = Path("llama-completion.exe")
MODEL = Path("model.gguf")


def _ok_run(stdout):
    def run(argv):
        return SubprocessResult(0, stdout, "")
    return run


class TestCallModel(unittest.TestCase):
    def test_extracts_the_assistant_reply_from_the_verified_stdout_shape(self):
        stdout = (
            "system\r\nyou are a helper\r\n"
            "user\r\ndo the thing\r\n"
            "assistant\r\nhere is the answer [end of text]\r\n\r\n\r\n"
        )
        response = call_model(
            runtime_path=RUNTIME, model_path=MODEL,
            system_prompt="you are a helper", user_prompt="do the thing",
            run=_ok_run(stdout),
        )
        self.assertEqual(response, "here is the answer")

    def test_strips_end_of_text_marker_and_surrounding_whitespace(self):
        stdout = "user\r\nhi\r\nassistant\r\n  padded reply  [end of text]\r\n"
        response = call_model(
            runtime_path=RUNTIME, model_path=MODEL,
            system_prompt="", user_prompt="hi", run=_ok_run(stdout),
        )
        self.assertEqual(response, "padded reply")

    def test_a_multiline_reply_is_returned_in_full(self):
        stdout = (
            "user\r\nhi\r\nassistant\r\n"
            "--- ANALYSIS ---\r\n{}\r\n--- FRONT ---\r\n<div></div> [end of text]\r\n"
        )
        response = call_model(
            runtime_path=RUNTIME, model_path=MODEL,
            system_prompt="", user_prompt="hi", run=_ok_run(stdout),
        )
        self.assertEqual(response, "--- ANALYSIS ---\n{}\n--- FRONT ---\n<div></div>")

    def test_only_the_last_assistant_marker_counts(self):
        """A user or system prompt that happens to contain the literal word "assistant"
        must never be mistaken for the real reply boundary."""
        stdout = (
            "system\r\nact as an assistant\r\n"
            "user\r\nhi\r\n"
            "assistant\r\nthe real reply [end of text]\r\n"
        )
        response = call_model(
            runtime_path=RUNTIME, model_path=MODEL,
            system_prompt="act as an assistant", user_prompt="hi", run=_ok_run(stdout),
        )
        self.assertEqual(response, "the real reply")

    def test_nonzero_exit_raises_with_stderr_in_the_message(self):
        def failing_run(argv):
            return SubprocessResult(1, "", "model file not found")

        with self.assertRaises(ModelCallError) as ctx:
            call_model(
                runtime_path=RUNTIME, model_path=MODEL,
                system_prompt="", user_prompt="hi", run=failing_run,
            )
        self.assertIn("model file not found", str(ctx.exception))

    def test_missing_assistant_marker_raises_rather_than_returning_garbage(self):
        def weird_run(argv):
            return SubprocessResult(0, "some unexpected output shape", "")

        with self.assertRaises(ModelCallError):
            call_model(
                runtime_path=RUNTIME, model_path=MODEL,
                system_prompt="", user_prompt="hi", run=weird_run,
            )

    def test_prompts_are_written_to_files_not_passed_as_argv_text(self):
        """A real prompt embeds a whole deck's samples/templates and can run to several KB --
        must go through -sysf/-f file arguments, never inline on the command line. The files
        must also still exist and hold the right content at the moment the process would read
        them -- checked from inside the fake ``run``, since call_model's temp directory is
        cleaned up as soon as ``run`` returns."""
        captured = {}

        def capturing_run(argv):
            captured["argv"] = argv
            sys_file = Path(argv[argv.index("-sysf") + 1])
            user_file = Path(argv[argv.index("-f") + 1])
            captured["sys_contents"] = sys_file.read_text(encoding="utf-8")
            captured["user_contents"] = user_file.read_text(encoding="utf-8")
            return SubprocessResult(0, "user\r\nx\r\nassistant\r\nok [end of text]\r\n", "")

        long_system = "S" * 5000
        long_user = "U" * 5000
        call_model(
            runtime_path=RUNTIME, model_path=MODEL,
            system_prompt=long_system, user_prompt=long_user, run=capturing_run,
        )
        argv = captured["argv"]
        self.assertNotIn(long_system, argv)
        self.assertNotIn(long_user, argv)
        self.assertIn("-sysf", argv)
        self.assertIn("-f", argv)
        self.assertEqual(captured["sys_contents"], long_system)
        self.assertEqual(captured["user_contents"], long_user)

    def test_passes_runtime_and_model_paths_and_deterministic_temp(self):
        captured = {}

        def capturing_run(argv):
            captured["argv"] = argv
            return SubprocessResult(0, "user\r\nx\r\nassistant\r\nok [end of text]\r\n", "")

        runtime_path = Path("C:/tools/llama-completion.exe")
        model_path = Path("C:/models/qwen2.5-7b.gguf")
        call_model(
            runtime_path=runtime_path, model_path=model_path,
            system_prompt="s", user_prompt="u", run=capturing_run,
        )
        argv = captured["argv"]
        self.assertEqual(argv[0], str(runtime_path))
        self.assertIn("-m", argv)
        self.assertEqual(argv[argv.index("-m") + 1], str(model_path))
        self.assertIn("--single-turn", argv)
        self.assertIn("--temp", argv)
        self.assertEqual(argv[argv.index("--temp") + 1], "0.0")

    def test_n_predict_is_passed_through(self):
        captured = {}

        def capturing_run(argv):
            captured["argv"] = argv
            return SubprocessResult(0, "user\r\nx\r\nassistant\r\nok [end of text]\r\n", "")

        call_model(
            runtime_path=RUNTIME, model_path=MODEL,
            system_prompt="s", user_prompt="u", n_predict=4096, run=capturing_run,
        )
        argv = captured["argv"]
        self.assertIn("-n", argv)
        self.assertEqual(argv[argv.index("-n") + 1], "4096")


if __name__ == "__main__":
    unittest.main()
