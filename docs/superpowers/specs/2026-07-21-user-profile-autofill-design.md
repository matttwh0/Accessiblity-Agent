# User Profile for Form Autofill — Design

**Date:** 2026-07-21
**Status:** Approved (pending spec review)

## Summary

Give the extension a small, user-managed profile (contact fields + free-form
notes). The profile is sent to the backend with each task so the Claude agent
can fill matching form fields on the page using its existing `type` action.

## Decisions (from brainstorming)

| Question | Decision |
|----------|----------|
| How info is entered | Manual profile form (no auto-capture in v1) |
| Where it lives / privacy | Stored locally, **sent to the backend** so Claude reasons with real values |
| What it stores | Fixed common fields + a free-form notes area |
| How it reaches the agent | **Approach A** — always included in each task's context |

## Data model

Stored in `chrome.storage.local` under key `userProfile`. All fields optional:

```
{
  fullName, email, phone,
  street, city, state, zip, country,
  notes        // free-form text, e.g. "my dog's name is Rex"
}
```

A separate boolean `useProfile` (default `true`) is the master "Use my info"
toggle. When `false`, no profile is sent.

Empty/whitespace fields are omitted before sending, so a half-filled profile
never pushes blank values into the prompt.

## Components

### 1. Options page (new: `options.html`, `options.js`)
- A standard MV3 options page (`options_page` in the manifest) with labeled
  inputs for each field, the notes textarea, the "Use my info" toggle, and a
  Save button.
- Loads the current `userProfile` / `useProfile` on open; writes both back to
  `chrome.storage.local` on Save.
- Light, non-blocking validation (e.g. email shape, phone digits): show a hint
  but still allow saving. Never block the user from saving what they typed.
- Reachable from a new **"⚙ My info"** button added to the bubble panel in
  `content.js`, which calls `chrome.runtime.openOptionsPage()`.

### 2. Extension wiring
- `content.js` `startTask()` reads `userProfile` and `useProfile` from storage.
  If `useProfile` is true, it builds a pruned profile (non-empty fields only)
  and includes it as `profile` in the `start_task` message. If the profile is
  effectively empty, it sends nothing.
- `background.js` forwards `profile` in the WebSocket `start_task` payload,
  alongside the existing `task` / `url` / `title` / `dom_tree`.

### 3. Backend
- `schemas.py`: add a `UserProfile` pydantic model (all optional fields) and a
  `profile: Optional[UserProfile] = None` field on `AgentState`.
- `main.py`: in the `start_task` handler, read `msg.get("profile")` into
  `AgentState(profile=...)`. Everything downstream already carries `state`.
- `clients/claude.py`: in `stream_action`, when `state.profile` is present,
  append a **"User's saved info"** block to the volatile `user_message`
  (NOT the cached `SYSTEM_PROMPT` — a per-user value in the cached prefix would
  break the prompt cache). Format it as clear `label: value` lines plus notes,
  with a one-line instruction: "When a form field matches this info, fill it in
  using the `type` action; never invent values you don't have."
- Apply the same block in `stream_recovery_action` so a form encountered during
  recovery can still be filled. Factor the block into one shared helper
  (e.g. `_profile_block(state)`) used by both call sites to avoid drift.

No new action type and no change to how `content.js` executes actions — Claude
emits the existing `type` action with the real value.

## Data flow

```
Options page  ──save──▶  chrome.storage.local { userProfile, useProfile }
                                   │
                          content.js startTask() reads + prunes
                                   │  start_task { ..., profile }
                                   ▼
                          background.js  ──WS──▶  main.py
                                   │  AgentState(profile=...)
                                   ▼
                    claude.py stream_action → "User's saved info" block
                                   │
                                   ▼
                    Claude emits `type` actions with real values
                                   │
                                   ▼
                          content.js executes as today
```

## Privacy & safety

- **PII to backend + LLM (accepted tradeoff).** The profile travels to the
  backend and appears in Claude's prompt on every task where `useProfile` is on.
  This is the explicitly chosen model. Mitigations built in:
  - Only non-empty fields are sent.
  - The master "Use my info" toggle disables sending entirely.
  - **No profile in logs.** Audit backend logging so the profile is never
    emitted. Today `main.py` logs only the task string (`=== NEW TASK: %r ===`)
    and DOM, and `claude.py` does not log the full `user_message`; keep it that
    way and add no logging of the profile block.
- **Auto-submit is out of scope for v1.** The agent already clicks buttons
  autonomously, so it may submit a form after filling it. v1 keeps current
  behavior. A future "confirm before submitting personal info" gate is noted as
  a follow-up, not built now.

## Error handling

- **No profile / empty profile:** nothing is sent; the agent behaves exactly as
  it does today.
- **Partial profile:** Claude fills the fields it has and leaves the rest; the
  instruction forbids inventing missing values.
- **Toggle off:** treated identically to no profile.
- **Malformed storage:** `content.js` guards profile reads in try/catch (matching
  existing storage-access patterns) and sends nothing on failure.

## Testing

- **Backend unit test (`test_profile.py`):**
  - `AgentState` accepts and carries a `UserProfile`.
  - The pruning/formatting helper omits empty fields and includes populated
    ones + notes.
  - `stream_action`'s `user_message` contains the profile block when a profile
    is set and omits it when not (assert on the assembled message, mock the
    Claude call — consistent with existing backend tests).
- **Extension:** manual verification plus the existing headless-Chrome + CDP
  harness — set a profile via the options page, run a task against a page with a
  known form, confirm the fields fill.

## Scope

**In v1:** one profile; fixed fields + notes; options page; always-in-context
(Approach A); master toggle; non-empty-field pruning; no-logging guarantee.

**Not in v1:** multiple/named profiles; learned or auto-captured values;
submit-confirmation gating; encryption beyond `chrome.storage`; cross-device
sync; per-field enable/disable.
