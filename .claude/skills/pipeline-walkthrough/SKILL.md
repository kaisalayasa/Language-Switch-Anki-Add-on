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
   longer exists, or a new one has appeared, that itself is a change worth reflecting:
   - **Deck/notetype selection & scoping**: `addon/ui/main_screen.py`'s pair picker,
     `addon/core/conversion.py`'s `scope_query`.
   - **Field sampling**: `_collect_samples`/`_collect_raw_samples` in
     `addon/ui/convert_dialog.py`.
   - **Mapping resolution** (whatever currently decides the role mapping before the user
     sees it) — check `addon/ui/main_screen.py`'s `_on_pair_changed` for the actual current
     priority order. This is the step most likely to have changed; do not assume it still
     matches a prior walkthrough.
   - **Content-based detection**: `addon/core/role_detect.py`'s `guess_role_mapping` and
     `addon/core/language_detect.py`'s `detect_language`/`detect_field_language` — read the
     current module docstrings, they're kept up to date with the real logic and its
     rationale.
   - **Audio policy**: `addon/core/audio_fields.py` (`resolve_audio_fields`,
     `plan_generated_fields`).
   - **User review / mapper UI**: `addon/ui/field_list.py`, `addon/ui/role_mapper.py`.
   - **Template generation**: `addon/core/template_generator.py`'s `generate_templates`.
   - **Notetype cloning & the two conversion modes**: `addon/ops/notetype_manager.py`,
     `addon/core/conversion.py`'s `build_plan`.
   - **Scheduling reset**: `apply_plan` in `notetype_manager.py`.
   - **TTS generation**: `addon/ops/tts_runner.py`, `addon/ops/tts_batch.py`,
     `addon/tts/sanitize.py`, `addon/tts/piper_provider.py`.
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
