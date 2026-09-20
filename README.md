# Deck Direction Converter

An Anki addon that takes an existing bilingual deck and flips its direction — for example,
turning a Japanese-front/English-back deck into an English-front/Japanese-back deck — and
generates natural pronunciation audio for the newly-fronted language. Everything runs locally
and offline: a local LLM reads the deck's real fields and writes the new card layout, and a
local TTS engine generates the audio. No cloud services, no accounts, no data ever leaves your
machine.

<img width="1917" height="984" alt="main picture" src="https://github.com/user-attachments/assets/c6507db0-9993-4306-a169-ca9e4fab53a0" />


## Why I built this

There are a lot of excellent, carefully-built decks out there for people learning Japanese —
Core 2000 being a well-known example. But the reverse doesn't really exist: there isn't nearly
as much equally well-structured material for people trying to learn English. Rather than build
a new English-learning deck from scratch, it seemed simpler to just take a deck that already
works — good example sentences, good structure, years of community refinement — and reverse it:
flip which language is on the front, and generate real pronunciation audio for it. That way the
quality of the original deck carries over, but now a much wider audience can actually use it.

## Current status and limitations

- **Direction-flipping works between any two languages** the deck already contains — the addon
  reads the deck's own content to figure out field placement, it isn't hardcoded to Japanese or
  English.
- **Pronunciation audio currently only works when English ends up as the new front-facing
  language.** The addon's voice list right now only includes English voices, so converting into
  a non-English front will flip the deck correctly but won't have audio generated for it yet.
  Adding more languages is a matter of adding more voices, not rebuilding the pipeline — it's
  planned, just not done yet.
- The core flow (analyze → review → convert → generate audio) is built and tested end-to-end.
  See [`TODO.md`](TODO.md) for the small list of known open issues.

## Requirements

- Anki 2.1.50 or newer (built and verified against 26.08.1)
- Windows, macOS, or Linux — CPU only, no GPU required
- About 5 GB of free disk space the first time you use it (a local language model and a local
  text-to-speech voice are downloaded once and cached — every run after that is fast and fully
  offline)

## How to use it

1. **Install the addon** in Anki (Tools → Add-ons → Install from file…, using the
   `.ankiaddon` file — see [Installing](#installing) below if you're building it yourself).
2. Open **Tools → Deck Direction Converter…**
3. **Pick the deck and note type** you want to convert from the dropdowns.
4. Click **Analyze**. The first time you do this, it downloads the local language model — this
   can take a while depending on your connection, but only happens once. It reads your deck's
   real fields and content and works out how the converted card should look.
5. **Review the result** in the live preview on the right. You can:
   - Use **Hide fields** to show or hide individual fields on the card without touching any code.
   - Switch to the **HTML** view if you want to hand-edit the generated template directly.
6. Click **Convert**. This creates a brand-new deck with the flipped direction — your original
   deck, notes, and notetype are never modified or touched in any way.
7. Click **Generate TTS audio**. The first time, this downloads the voice model — again, only
   once. It then generates pronunciation audio for every card, with a progress bar and a Stop
   button; you can stop partway through and pick up again later without redoing finished cards.
8. Study your new deck like any other.

## Licensing

This addon's own code is **MIT-licensed** (see [`LICENSE`](LICENSE)) — free to use, modify, and
share.

Everything it downloads at runtime was individually checked for license compatibility:

| Component | License |
|---|---|
| [Piper](https://github.com/rhasspy/piper) (TTS engine) | MIT |
| [llama.cpp](https://github.com/ggml-org/llama.cpp) (LLM runtime) | MIT |
| [Qwen2.5-7B-Instruct](https://huggingface.co/Qwen/Qwen2.5-7B-Instruct-GGUF) (language model) | Apache-2.0 |
| Piper voice `en_US-ljspeech-high` | Public domain |
| Piper voice `en_GB-alba-medium` | CC BY 4.0 (attribution: "Alba" voice via [rhasspy/piper-voices](https://huggingface.co/rhasspy/piper-voices)) |
| ffmpeg (Windows/Linux build, used only to compress generated audio) | [LGPL-2.1+](https://github.com/BtbN/FFmpeg-Builds) |
| ffmpeg (macOS build, same purpose) | [GPL-3.0+](https://evermeet.cx/ffmpeg/) — invoked as a separate subprocess only, never linked into this addon's code, so its license doesn't apply to this addon's own code (the same way any app can shell out to `git` or ImageMagick without becoming GPL itself) |

None of these are bundled in this repository — the addon downloads each one from its own
official source (GitHub releases, HuggingFace, evermeet.cx for the macOS ffmpeg build) the
first time it's needed, and caches it locally.

## For developers

The full technical write-up — architecture, design decisions, verified Anki/llama.cpp API
facts, and the reasoning behind every non-obvious choice in this codebase — lives in
[`CLAUDE.md`](CLAUDE.md) and the [`docs/`](docs/) folder. [`TODO.md`](TODO.md) tracks known
open issues.
