# Local LLM notes

Companion to `docs/api-notes.md`, same discipline: nothing below is guessed. Every fact was
checked by actually running the real binary/model or hitting the real GitHub/HuggingFace API.

## The model

**Bumped from the originally-verified 1.5B model to `Qwen/Qwen2.5-7B-Instruct-GGUF`** once
real prompt testing (see the "Why the model doesn't decide direction" account in `CLAUDE.md`)
showed the smaller model wasn't reliable enough at even the narrowed job it's asked to do now
(writing template HTML for a given placement). Facts below for the 7B model are verified the
same way the 1.5B facts originally were (`?blobs=true` against the real HuggingFace API); the
1.5B facts are kept in the collapsed section below only as a record of what was checked first,
not as a fallback this addon can currently load.

Q4_K_M quantization, split into two files -- HuggingFace's standard convention once a GGUF
exceeds ~4GB. Confirmed exact byte sizes (`addon/llm/model_manager.py`'s `MODEL_FILES`):

| file | bytes |
|---|---|
| `qwen2.5-7b-instruct-q4_k_m-00001-of-00002.gguf` | 3,993,201,344 |
| `qwen2.5-7b-instruct-q4_k_m-00002-of-00002.gguf` | 689,872,288 |

Repo tagged `license:apache-2.0`. Download base:
`https://huggingface.co/Qwen/Qwen2.5-7B-Instruct-GGUF/resolve/main/`. llama.cpp loads a split
GGUF automatically from just the first part's path (`-m` needs only file 1 of 2) -- no merge
step, confirmed this session (see `model_manager.py`'s module docstring).

<details>
<summary>Superseded: original 1.5B facts, kept for the record</summary>

`Qwen/Qwen2.5-1.5B-Instruct-GGUF`, file `qwen2.5-1.5b-instruct-q4_k_m.gguf`, confirmed via the
HuggingFace API (`?blobs=true`) at **exactly 1,117,320,736 bytes** (≈1.04 GB). Repo tagged
`license:apache-2.0`.

Download URL: `https://huggingface.co/Qwen/Qwen2.5-1.5B-Instruct-GGUF/resolve/main/qwen2.5-1.5b-instruct-q4_k_m.gguf`

</details>

## The runtime: llama.cpp

**No stable release exists.** Every tag on `ggml-org/llama.cpp` is a rolling nightly build number
(`b10982`, `b10983`, …), all marked `prerelease: true`. Five were published in a single day during
this investigation. `ensure_llama_runtime` (the future `runtime.py`) must pin one exact build
number as a constant, the same way `piper_binary_manager.RELEASE_TAG` pins Piper's, and that pin
will need periodic re-verification — there is no "latest stable" to defer to.

**Pinned for this project: `b10983`.**

### CPU-only asset names (per OS/arch)

Verified against the real release's asset list (`b10983`). Deliberately excludes CUDA/ROCm/Vulkan/
SYCL/OpenVINO variants — those need matching GPU drivers we can't assume, and are 5-30x larger.

| system.lower() | machine.lower() | asset |
|---|---|---|
| windows | amd64 / x86_64 | `llama-b{TAG}-bin-win-cpu-x64.zip` |
| windows | arm64 | `llama-b{TAG}-bin-win-cpu-arm64.zip` |
| linux | x86_64 / amd64 | `llama-b{TAG}-bin-ubuntu-x64.tar.gz` |
| linux | aarch64 | `llama-b{TAG}-bin-ubuntu-arm64.tar.gz` |
| darwin | x86_64 / amd64 | `llama-b{TAG}-bin-macos-x64.tar.gz` |
| darwin | arm64 | `llama-b{TAG}-bin-macos-arm64.tar.gz` |

**Gap versus Piper's platform coverage: no 32-bit ARM build.** Piper ships
`piper_linux_armv7l.tar.gz`; llama.cpp's release matrix has nothing for `armv7l`/`armv6l`. Should
raise `UnsupportedPlatform` for that combination, same pattern as Piper's missing Windows-ARM64
case.

Downloaded and verified `windows/amd64` for real: extracted zip is 18.4MB, `llama-cli.exe --version`
reports `version: 0.4.1-dev (build 10983, commit 9e7171624)` on stdout. The zip contains many CPU
microarchitecture variants of `ggml-cpu-*.dll` (sandybridge through zen4) — the loader auto-selects
the right one at runtime; no per-CPU download logic needed on our end.

Asset URL pattern: `https://github.com/ggml-org/llama.cpp/releases/download/{TAG}/{asset}`

## The binary to invoke: `llama-completion.exe`, not `llama-cli.exe`

The zip ships several executables. `llama-cli.exe` is built as an **interactive chat REPL** — even
with `--single-turn`, stdout is polluted with an ASCII-art banner, a "Loading model..." line, and a
`/exit /regen /clear /read /glob` commands list, all on **stdout**, mixed with the actual response.
There is no flag that suppresses it (`--simple-io`, `--no-display-prompt`, `--log-disable` were all
tried; none remove the banner).

**`llama-completion.exe` is the right tool.** Same flag set (shares `common params`), but all
banner/loading/perf-timing output goes to **stderr**. stdout contains only the chat exchange in a
fixed, parseable shape.

## Verified invocation contract

```
llama-completion.exe -m <model.gguf> -sysf <system_prompt.txt> -f <user_prompt.txt> --single-turn --temp 0
```

- `-f FNAME` / `-sysf FNAME`: read the user/system prompt from a file rather than a command-line
  argument. **Use files, not `-p`/`-sys` with inline text** — our real prompt embeds a whole deck's
  field names/samples/templates, which can run to several KB; a file has no OS command-line length
  ceiling to worry about (Piper's `--output_file` + stdin pattern has the same motivation).
- `--single-turn` (`-st`): exits after one response instead of waiting for more stdin. **The flag
  named in earlier planning, `-no-cnv`, does not exist in this build** — confirmed by running it
  and getting `error: invalid argument: -no-cnv`. This is exactly the kind of guess `CLAUDE.md`
  forbids; glad it was checked live rather than trusted from memory.
- `--temp 0`: deterministic sampling, for reproducible output during prompt iteration and testing.
- Chat template is read from the GGUF's own metadata automatically (no `--chat-template` flag
  needed for Qwen2.5-Instruct).

**stdout shape** (confirmed byte-for-byte, line endings are `\r\n`):

```
system\r\n
<system prompt text>\r\n
user\r\n
<user prompt text>\r\n
assistant\r\n
<generated response text> [end of text]\r\n
\r\n
\r\n
```

`system\r\n...\r\n` is only present when `-sysf`/`-sys` was given. Parsing rule for `client.py`:
split on the last `\nassistant\r\n` (after normalizing `\r\n`→`\n`), take everything after it,
strip a trailing `[end of text]` marker if present, strip trailing whitespace. This is a verified
contract, not a guess — tested with and without a system prompt, both matched exactly.

**Exit code 0** on success in every test run so far. Not yet observed: what a real failure (missing
model file, corrupt GGUF, OOM) looks like on exit code / stderr — confirm this when building
`client.py`'s error handling, rather than assuming a non-zero exit code is the only failure signal.

## Performance (this dev machine, CPU only, Q4_K_M)

**These numbers are from the original 1.5B model and have not been re-measured against the 7B
model this addon actually ships now** -- flag this if performance ever needs re-justifying; per
`CLAUDE.md`'s "never guess" rule, don't assume the 7B numbers scale down proportionally from
these without actually timing a real run.

- Prompt processing: ~200-230 tokens/sec
- Generation: ~34-38 tokens/sec
- Model load time: <1 second (subsequent runs; mmap)

At 1.5B, fast enough that a single per-notetype analysis call (expected: a few hundred tokens of
prompt, a few hundred tokens of generated templates) completed in well under a minute even on
modest hardware. Real use with the 7B model (see the LLM overhaul commit history and
`CLAUDE.md`) confirms analysis calls still complete in reasonable time on the dev machine, but no
exact tokens/sec figure has been captured for it the way it was for 1.5B -- worth doing once
performance on lower-end hardware becomes a real question rather than a theoretical one.

## Grammar-constrained decoding: available, not used

`--grammar`/`--grammar-file` (GBNF) and `-j`/`--json-schema`/`--json-schema-file` all exist and are
presumably functional (not exercised — the approved design emits delimited free text, not pure
JSON, and relies on post-hoc validation + a re-prompt-on-failure loop instead of constrained
decoding). Worth knowing they exist if validation-with-retry proves too unreliable in practice.

## Still to confirm

- Real failure-mode output (bad model path, OOM, corrupt file) — needed before writing
  `runtime.py`/`client.py`'s error handling.
- Whether `-st`/`--single-turn` existed in earlier llama.cpp builds, in case the pinned build
  number ever needs bumping — re-check `--help` output after any re-pin, don't assume the flag
  survives.
- Actual template-writing quality against a real prompt + real deck fixture (this is the next
  step: iterating `prompt.py` against `tests/fixtures/*.json`).
