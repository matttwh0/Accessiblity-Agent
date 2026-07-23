# Calm UI, Spoken Narration & Chat Answers — Implementation Plan

> Executed inline in-session (executing-plans style) by the controller, which has
> full codebase context from the same session. Spec:
> `docs/superpowers/specs/2026-07-22-calm-ui-voice-narration-chat-design.md`

**Goal:** Checklist + spinner UI (no narration text), spoken step narration via
chrome.tts in plain language for older users, and a terminal `answer` action for
non-navigation questions.

## Task 1: Backend — `answer` action (TDD)
- [ ] Failing tests in `backend/test_answer.py`: `answer` action → `status == "done"`
      even with unchecked checklist items; `ACTION_TOOL` enum contains `"answer"`;
      `SYSTEM_PROMPT` contains the answer rule + "not verbose" + spoken-language guidance.
- [ ] Implement: `ActionType.ANSWER` (schemas), terminal handling in
      `nodes.decide_action`, tool enum + prompt edits in `clients/claude.py`.
- [ ] Full suite green; commit.

## Task 2: Extension — spinner UI
- [ ] `content.js`: spinner start/stop helpers with rotating word list; rework
      `agent_update` handler (running → spinner+checklist only; ended → final text);
      `startTask` uses spinner; `stopAgent` stops it.
- [ ] `bubble.css`: spinner + keyframes.
- [ ] `node --check`; commit.

## Task 3: Extension — spoken narration
- [ ] `manifest.json`: add `"tts"` permission.
- [ ] `background.js`: `speak()` helper (storage-gated, rate 0.9), called in
      `onBackendMessage` (each action + terminal, answers use `value`);
      `chrome.tts.stop()` on dictation start; done-branch passes answer text.
- [ ] `content.js`: 🔊 toggle button (storage `speechEnabled`, default ON, synced).
- [ ] `node --check` + manifest JSON check; commit.

## Task 4: Verify end-to-end
- [ ] Backend suite green.
- [ ] Harness (headless Chrome + scripted fake `/agent`): spinner while running,
      no narration text, answer text rendered on `answer`.
- [ ] Commit any fixes; push to `feature/user-profile-autofill` (PR #1).
