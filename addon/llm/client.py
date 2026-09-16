"""Runs one ``llama-completion.exe`` call and returns the model's raw response text.

Per ``docs/llm-notes.md``'s verified invocation contract: the system/user prompts go into temp
files (``-sysf``/``-f``, not ``-sys``/``-p`` with inline text) because a real prompt embeds a
whole deck's field names/samples/templates and can run to several KB, past what's safe on a
command line -- the same reasoning behind Piper's ``--output_file``-not-argv pattern.
``--single-turn`` exits after one response instead of waiting for more stdin (``-no-cnv``, named
in earlier planning, does not exist in this build -- confirmed by actually running it).
``--temp 0`` is not perfect determinism in practice (llama.cpp's multi-threaded CPU inference
was observed producing different completions for the identical prompt across separate runs --
see the LLM overhaul commit history), but it is still the right default: it removes sampling
*randomness* as a variable, leaving only genuine floating-point non-associativity, which is a
much smaller source of variation.

Parsing follows the confirmed stdout shape byte-for-byte: after normalizing ``\\r\\n`` to
``\\n``, split on the last ``\\nassistant\\n`` line and take everything after it, then strip a
trailing ``[end of text]`` marker. This module does not interpret the response any further --
splitting it into the ANALYSIS/FRONT/BACK/CSS sections is ``response.py``'s job.

All I/O is dependency-injected (``run``), same discipline as
``addon.tts.piper_binary_manager`` -- the default implementation is the only code path here that
spawns a real process, and it is never exercised by the pure test suite.
"""

from __future__ import annotations

import subprocess
import tempfile
from pathlib import Path
from typing import Callable, List, NamedTuple, Optional

__all__ = ["ModelCallError", "SubprocessResult", "RunFn", "call_model"]


class SubprocessResult(NamedTuple):
    returncode: int
    stdout: str
    stderr: str


RunFn = Callable[[List[str]], SubprocessResult]

#: Comfortably covers the richest real fixture seen so far (Core 2000's 18-field notetype)
#: with margin, per the real generations recorded while building this addon. A caller may
#: override this for a notetype it knows is unusually large.
DEFAULT_N_PREDICT = 2048


class ModelCallError(RuntimeError):
    """``llama-completion.exe`` ran but did not produce a usable response."""


def call_model(
    *,
    runtime_path: Path,
    model_path: Path,
    system_prompt: str,
    user_prompt: str,
    n_predict: int = DEFAULT_N_PREDICT,
    temp: float = 0.0,
    run: Optional[RunFn] = None,
) -> str:
    """Return the model's response text for one system+user prompt pair.

    Raises :class:`ModelCallError` if the process exits non-zero, or if its stdout doesn't
    match the verified contract (no ``assistant`` marker found) -- both are real signals
    something is wrong (a bad model/runtime path, a llama.cpp build whose CLI flags or output
    shape has drifted from what's documented in ``docs/llm-notes.md``), not something to paper
    over with a fallback.
    """
    run = run or _run_subprocess
    with tempfile.TemporaryDirectory() as tmp:
        sys_path = Path(tmp) / "system.txt"
        user_path = Path(tmp) / "user.txt"
        sys_path.write_text(system_prompt, encoding="utf-8")
        user_path.write_text(user_prompt, encoding="utf-8")

        result = run([
            str(runtime_path), "-m", str(model_path),
            "-sysf", str(sys_path), "-f", str(user_path),
            "--single-turn", "--temp", str(temp), "-n", str(n_predict),
        ])

    if result.returncode != 0:
        raise ModelCallError(
            "%s exited %d: %s" % (runtime_path, result.returncode, result.stderr)
        )
    return _extract_response(result.stdout)


def _extract_response(stdout: str) -> str:
    normalized = stdout.replace("\r\n", "\n")
    marker = "\nassistant\n"
    idx = normalized.rfind(marker)
    if idx == -1:
        raise ModelCallError(
            "no 'assistant' marker in llama-completion.exe output -- either the invocation "
            "contract in docs/llm-notes.md has changed, or the model failed to respond:\n%s"
            % normalized[-2000:]
        )
    raw = normalized[idx + len(marker):]
    raw = raw.replace("[end of text]", "").strip()
    return raw


def _run_subprocess(argv: List[str]) -> SubprocessResult:
    # On Windows, spawning a console app from a GUI process (Anki/Qt) briefly flashes a
    # console window unless told not to -- same reasoning as piper_binary_manager's use of
    # this flag. Harmless elsewhere, where the flag doesn't exist.
    extra = {"creationflags": subprocess.CREATE_NO_WINDOW} if hasattr(subprocess, "CREATE_NO_WINDOW") else {}
    completed = subprocess.run(
        argv, capture_output=True, text=True, encoding="utf-8", errors="replace", **extra
    )
    return SubprocessResult(completed.returncode, completed.stdout, completed.stderr)
