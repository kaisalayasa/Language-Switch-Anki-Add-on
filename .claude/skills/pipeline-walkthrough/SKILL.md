---
name: pipeline-walkthrough
description: Produce a high-level, numbered walkthrough of what happens to a deck as it goes through this addon's converter (detection, mapping, template generation, cloning, conversion, TTS), each step paired with a brief "why". Use after any feature is added/removed/edited in the conversion pipeline, or whenever the user asks for this walkthrough again.
---

# Deck conversion pipeline walkthrough

This addon converts a bilingual Anki deck's direction. The user asks for this walkthrough
repeatedly, after each feature change, to sanity-check what the pipeline currently does and
why. **Never reuse a walkthrough from earlier in the conversation or from memory** — the
whole point of asking again is that the pipeline just changed. Re-derive it fresh from the
actual current code every time this skill runs.

## What to do

1. **Re-read the pipeline end to end from source**, not from what you remember it to be.
   Check these in order — each is one pipeline stage. If a file/function listed here no
   longer exists, or a new one has appeared, that itself is a change worth reflecting. As of
   the LLM overhaul, there is no role-mapping system any more (`role_schema.py`,
   `role_detect.py`, `template_generator.py`, `field_list.py`, `role_mapper.py` are all
   deleted) — the model produces finished templates directly:
   - **Deck/notetype selection & scoping**: `addon/ui/main_screen.py`'s pair picker,
     `addon/core/conversion.py`'s `scope_query`.
   - **Field sampling**: `addon/ops/deck_data.py`'s `collect_field_samples`/
     `collect_raw_samples`.
   - **Direction & field-placement resolution**: `addon/llm/direction.py`'s
     `resolve_direction` — deterministic (language detection + template-structure parsing),
     computed in Python and handed to the model as a given fact, never asked of it. Check
     this module's own docstring for why (a documented history of the model failing to
     execute an abstract "swap front and back" instruction).
   - **The analysis call**: `addon/llm/analyze.py`'s `analyze_deck` — builds the prompt
     (`llm/prompt.py`), calls the model (`llm/client.py`), parses the reply
     (`llm/response.py`), validates it and retries on failure (`llm/validate.py`), and
     computes the 1-5 star trust rating from how much retrying it took. This is the step
     most likely to have changed; do not assume it still matches a prior walkthrough.
   - **Audio safety backstop**: `addon/llm/audio_safety.py`'s `enforce_audio_safety`,
     applied inside `analyze_deck` to whatever the model wrote, regardless of whether it
     complied with the prompt's own audio rules.
   - **User review / hand-edit**: `addon/ui/main_screen.py`'s AI-result panel (read-only:
     description, trust stars, field placement) and its raw HTML editor (editable,
     initialized from the analysis) — both feed `PreviewPanel` live.
   - **Notetype cloning & the two conversion modes**: `addon/ops/notetype_manager.py`,
     `addon/core/conversion.py`'s `build_plan`.
   - **Recorded conversion state**: `addon/core/deck_state.py` — a note tag and a css
     comment written at conversion time (`ops/convert_op.py`, `ops/notetype_manager.py`),
     read back on reopen instead of re-derived from template structure.
   - **Scheduling reset**: `apply_plan` in `notetype_manager.py`.
   - **TTS generation**: `addon/ops/tts_runner.py`, `addon/ops/tts_batch.py`,
     `addon/tts/sanitize.py`, `addon/tts/piper_provider.py` — driven by a plain
     `audio_field`/`source_field` string pair now (from `DeckAnalysis`), not a role mapping.
   Skip a stage only if it's been removed entirely (confirm via grep before assuming).

2. **For each stage, state two things, tersely**: what happens, and why (the real reason —
   a bug it fixes, a constraint it exists under, a failure mode it prevents). Pull the *why*
   from the code's own comments/docstrings where they exist rather than inventing one — this
   codebase documents its reasoning inline specifically so it doesn't get re-derived wrong.

3. **Match this format** (numbered, one short paragraph per step, bold lead-in, no filler):

   ```
   **1. <Stage name>.** <What happens, one or two sentences>. <Why — one sentence>.

   **2. <Next stage>.** ...
   ```

   Close with one sentence tying it together (e.g. what invariant every stage upholds) —
   not a restatement of all the steps.

4. **If something was recently removed** (the user just asked you to cut a feature), name
   what filled the gap left behind, not just that the step is gone — e.g. "step 3 is gone;
   step 4 now runs unconditionally in its place." A missing step with no explanation reads
   as an oversight, not a deliberate change.

5. Keep it scannable — this is a sanity check the user runs repeatedly, not documentation.
   Don't pad it with implementation detail beyond what explains the *why*. If unsure how
   much depth to include, match the depth of the walkthrough already given earlier in this
   conversation (if one exists) rather than expanding scope.
