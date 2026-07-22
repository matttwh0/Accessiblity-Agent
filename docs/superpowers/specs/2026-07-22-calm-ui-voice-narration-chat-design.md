# Calm UI, Spoken Narration & Chat Answers — Design

**Date:** 2026-07-22
**Status:** Approved (conversationally, in-session)

## Summary

Three changes aimed at an older, non-technical audience:

1. **Calm visual UI.** While a task runs, the bubble no longer shows the agent's
   per-action narration. It shows the live checklist plus a small spinner with a
   rotating word ("Thinking…", "Working on it…", …). The final message (done /
   failed / answer) is still shown as text.
2. **Spoken narration.** The agent talks the user through what it's doing:
   every step's `description` is spoken aloud via Chrome's built-in TTS
   (`chrome.tts`, slightly slowed). The prompt is rewritten so descriptions are
   written to be SPOKEN to an older listener: one short sentence, plain
   everyday words, no jargon, calm and reassuring. A 🔊 toggle in the bubble
   (default ON, persisted per browser as `speechEnabled`) disables it. Speech
   stops when dictation starts so the agent never talks over (or into) the mic.
   Note: AssemblyAI is speech-to-text only (already used for dictation); TTS
   uses Chrome's engine, behind one `speak()` helper so a neural-voice service
   could be swapped in later.
3. **Chat answers.** A new terminal `answer` action: when the user's request is
   clearly a general question with no relation to navigating, finding, or doing
   something on a website, the agent answers it directly (short, explicitly
   non-verbose) in `value`, no checklist. One-shot — no conversation memory.
   Existing behavior is preserved: "where is X / show me X / how do I do X"
   remain navigation tasks (the prompt already mandates this; the answer rule
   is scoped to questions a webpage visit wouldn't satisfy).

## Backend

- `ActionType.ANSWER = "answer"`; `ACTION_TOOL` enum gains `"answer"`;
  answer text travels in `value`.
- `nodes.decide_action`: `answer` is terminal → `status = "done"` (bypasses the
  checklist done-gate, which only gates `done`).
- `SYSTEM_PROMPT`: (a) answer rule as scoped above, with "keep answers short —
  2-3 plain sentences, do not be verbose"; (b) description guidance rewritten:
  descriptions are read aloud to an older person — one short friendly sentence,
  present tense, everyday words, no technical terms, say what you're doing and
  what happens next. `RECOVERY_PROMPT` gets the same description guidance.
- No main.py changes (terminal status ends the loop as today).

## Extension

- `background.js`: `speak(text)` helper — reads `speechEnabled` (default true)
  from `chrome.storage.local`, `chrome.tts.speak(text, { rate: 0.9 })`
  (interrupting any current utterance). Called with each arriving action's
  description and the final description/answer. `chrome.tts.stop()` when
  dictation starts. Done/failed notify passes `description = action.value` for
  `answer` actions. Manifest gains the `"tts"` permission.
- `content.js`: while running, status area shows spinner + rotating word
  (~2s interval, idempotent start); per-action descriptions are not displayed.
  Checklist rendering unchanged. On `ended`, spinner stops and the final
  description (or answer) is shown. New 🔊 "Speak to me: On/Off" toggle button
  (mirrors the wake-toggle pattern, storage-synced). Errors and voice states
  still display as today.
- `bubble.css`: spinner styles + keyframes.

## Testing

- Backend: `answer` is terminal even with unchecked checklist items; tool enum
  includes it; prompt contains the answer rule, non-verbose instruction, and
  spoken-description guidance. Suite stays green.
- Extension: `node --check`; headless-Chrome harness with a scripted fake
  `/agent`: spinner appears while a task runs (no narration text), answer text
  renders on an `answer` action.

## Out of scope

Conversation memory; neural-voice TTS; speaking the checklist itself;
per-step visual narration toggle.
