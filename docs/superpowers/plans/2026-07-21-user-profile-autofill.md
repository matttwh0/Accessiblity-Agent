# User Profile for Form Autofill — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Let users save a personal profile (contact fields + notes) that the Claude agent uses to fill matching form fields during tasks.

**Architecture:** The profile is stored in `chrome.storage.local`, edited on a dedicated extension options page. On each task, `content.js` prunes empty fields and sends the profile in the `start_task` message; `background.js` forwards it over the agent WebSocket; the backend loads it into `AgentState` and appends a "User's saved info" block to the volatile per-step prompt (not the cached system prompt). Claude fills fields with its existing `type` action — no new action type.

**Tech Stack:** Chrome MV3 (vanilla JS), FastAPI + LangGraph + Anthropic SDK (Python), pytest.

**Spec:** `docs/superpowers/specs/2026-07-21-user-profile-autofill-design.md`

---

## File Structure

**Backend:**
- Modify `backend/agent/schemas.py` — add `UserProfile` model + `profile` field on `AgentState`
- Modify `backend/clients/claude.py` — add `_profile_block()` helper; inject into `stream_action` and `stream_recovery_action`
- Modify `backend/main.py` — parse `profile` from `start_task` into `AgentState`
- Create `backend/test_profile.py` — unit tests

**Extension:**
- Modify `extension/manifest.json` — register `options_page`
- Create `extension/options.html` — the profile form
- Create `extension/options.js` — load/save profile + toggle
- Modify `extension/content.js` — add "⚙ My info" button; read + prune profile in `startTask()`
- Modify `extension/background.js` — forward `profile` in the `start_task` WebSocket payload

**Working directory for all backend commands:** `backend/` (tests import `agent.*` / `clients.*` as top-level packages). Use the project's Python environment (the one where `pytest` and `assemblyai` are installed).

---

## Task 1: UserProfile schema + AgentState.profile

**Files:**
- Modify: `backend/agent/schemas.py`
- Test: `backend/test_profile.py` (create)

- [ ] **Step 1: Write the failing test**

Create `backend/test_profile.py`:

```python
# Tests for the user-profile autofill feature: schema, prompt-block formatting,
# and that the profile reaches the agent's decide/recover prompts.
from agent.schemas import AgentState, UserProfile, PageContext, DOMNode


def make_context():
    return PageContext(
        url="https://example.com",
        title="Test",
        dom_tree=[DOMNode(tag="input", label="Email", selector="#email")],
    )


def test_agentstate_defaults_profile_to_none():
    state = AgentState(task="t", context=make_context())
    assert state.profile is None


def test_agentstate_accepts_userprofile_from_dict():
    profile = UserProfile(**{"email": "a@b.com", "fullName": "Ada"})
    state = AgentState(task="t", context=make_context(), profile=profile)
    assert state.profile.email == "a@b.com"
    assert state.profile.fullName == "Ada"
    # unset fields default to None
    assert state.profile.phone is None
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd backend && python -m pytest test_profile.py -v`
Expected: FAIL with `ImportError: cannot import name 'UserProfile'`

- [ ] **Step 3: Add the UserProfile model and the AgentState field**

In `backend/agent/schemas.py`, add this class immediately before `class PageContext(BaseModel):`:

```python
class UserProfile(BaseModel):
    """User-supplied info for filling forms. All fields optional; the extension
    sends only non-empty ones. Never logged (see main.py / claude.py)."""
    fullName: Optional[str] = None
    email: Optional[str] = None
    phone: Optional[str] = None
    street: Optional[str] = None
    city: Optional[str] = None
    state: Optional[str] = None
    zip: Optional[str] = None
    country: Optional[str] = None
    notes: Optional[str] = None
```

Then in `class AgentState(BaseModel):`, add this field right after the `context: PageContext` line:

```python
    # user-supplied info for autofill; None when the user has none or disabled it
    profile: Optional[UserProfile] = None
```

(`UserProfile` is defined earlier in this file, so a direct reference resolves without a forward-ref `model_rebuild()`.)

- [ ] **Step 4: Run test to verify it passes**

Run: `cd backend && python -m pytest test_profile.py -v`
Expected: PASS (3 passed)

- [ ] **Step 5: Commit**

```bash
git add backend/agent/schemas.py backend/test_profile.py
git commit -m "Add UserProfile schema and AgentState.profile field"
```

---

## Task 2: `_profile_block()` prompt formatter

**Files:**
- Modify: `backend/clients/claude.py`
- Test: `backend/test_profile.py`

- [ ] **Step 1: Write the failing test**

Append to `backend/test_profile.py`:

```python
from clients.claude import _profile_block
from agent.schemas import UserProfile


def test_profile_block_empty_for_none():
    assert _profile_block(None) == ""


def test_profile_block_empty_when_all_fields_blank():
    assert _profile_block(UserProfile(email="   ", notes="")) == ""


def test_profile_block_includes_populated_fields_and_notes():
    p = UserProfile(fullName="Ada Lovelace", email="ada@example.com",
                    phone="", notes="prefers window seats")
    block = _profile_block(p)
    assert "User's saved info" in block
    assert "Ada Lovelace" in block
    assert "ada@example.com" in block
    assert "prefers window seats" in block
    # empty phone is omitted, and its label must not appear
    assert "Phone" not in block


def test_profile_block_instructs_type_action_and_forbids_invention():
    block = _profile_block(UserProfile(email="ada@example.com"))
    assert "type" in block
    assert "never invent" in block.lower()
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd backend && python -m pytest test_profile.py -k profile_block -v`
Expected: FAIL with `ImportError: cannot import name '_profile_block'`

- [ ] **Step 3: Implement the helper**

In `backend/clients/claude.py`, add this function immediately above `async def stream_action(` (near line 558). It takes a `UserProfile` (or `None`) directly so it is trivially unit-testable:

```python
def _profile_block(profile) -> str:
    """Format a UserProfile as a prompt block, omitting empty fields. Returns ""
    when there is nothing to show, so callers can append unconditionally."""
    if not profile:
        return ""
    fields = [
        ("Full name", profile.fullName),
        ("Email", profile.email),
        ("Phone", profile.phone),
        ("Street address", profile.street),
        ("City", profile.city),
        ("State/region", profile.state),
        ("ZIP/postcode", profile.zip),
        ("Country", profile.country),
    ]
    lines = [f"- {label}: {val.strip()}"
             for label, val in fields if val and val.strip()]
    if profile.notes and profile.notes.strip():
        lines.append(f"- Notes: {profile.notes.strip()}")
    if not lines:
        return ""
    return (
        "\n\nUser's saved info — when a form field matches one of these, fill it "
        "in with the `type` action. Never invent values you don't have here:\n"
        + "\n".join(lines)
    )
```

- [ ] **Step 4: Run test to verify it passes**

Run: `cd backend && python -m pytest test_profile.py -k profile_block -v`
Expected: PASS (4 passed)

- [ ] **Step 5: Commit**

```bash
git add backend/clients/claude.py backend/test_profile.py
git commit -m "Add _profile_block prompt formatter"
```

---

## Task 3: Inject the profile block into decide + recover prompts

**Files:**
- Modify: `backend/clients/claude.py`
- Test: `backend/test_profile.py`

- [ ] **Step 1: Write the failing test**

Append to `backend/test_profile.py`:

```python
import asyncio
from types import SimpleNamespace

import clients.claude as claude_mod
from agent.schemas import AgentState, UserProfile, PageContext, DOMNode


def _make_state(profile=None):
    return AgentState(
        task="fill the signup form",
        context=PageContext(
            url="https://example.com",
            title="Signup",
            dom_tree=[DOMNode(tag="input", label="Email", selector="#email")],
        ),
        profile=profile,
    )


def _patch_capture(monkeypatch):
    """Replace _call_claude with a stub that captures the user message and
    returns a minimal valid tool_use response."""
    captured = {}

    async def fake_call(label, **kwargs):
        captured["user_message"] = kwargs["messages"][0]["content"]
        block = SimpleNamespace(
            type="tool_use",
            input={"type": "click", "selector": "#email", "description": "x"},
        )
        return SimpleNamespace(content=[block])

    monkeypatch.setattr(claude_mod, "_call_claude", fake_call)
    return captured


def test_stream_action_includes_profile_when_set(monkeypatch):
    captured = _patch_capture(monkeypatch)
    state = _make_state(UserProfile(email="ada@example.com"))
    asyncio.run(claude_mod.stream_action(state))
    assert "User's saved info" in captured["user_message"]
    assert "ada@example.com" in captured["user_message"]


def test_stream_action_omits_profile_when_none(monkeypatch):
    captured = _patch_capture(monkeypatch)
    asyncio.run(claude_mod.stream_action(_make_state(None)))
    assert "User's saved info" not in captured["user_message"]


def test_stream_recovery_action_includes_profile_when_set(monkeypatch):
    captured = _patch_capture(monkeypatch)
    state = _make_state(UserProfile(email="ada@example.com"))
    asyncio.run(claude_mod.stream_recovery_action(state))
    assert "User's saved info" in captured["user_message"]
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd backend && python -m pytest test_profile.py -k "stream_" -v`
Expected: FAIL — `test_stream_action_includes_profile_when_set` asserts the block is present, but it isn't appended yet.

- [ ] **Step 3: Append the block in both call sites**

In `backend/clients/claude.py`, in `stream_action`, find the line that ends the base prompt:

```python
What is the next action?"""
```

Immediately after that assignment statement, add:

```python
    user_message += _profile_block(state.profile)
```

In `stream_recovery_action`, find the line:

```python
Reconsider and choose a DIFFERENT next action."""
```

Immediately after that assignment statement, add:

```python
    user_message += _profile_block(state.profile)
```

(Both additions go before the existing `response = await _call_claude(...)` call in each function.)

- [ ] **Step 4: Run test to verify it passes**

Run: `cd backend && python -m pytest test_profile.py -v`
Expected: PASS (all tests in the file)

- [ ] **Step 5: Commit**

```bash
git add backend/clients/claude.py backend/test_profile.py
git commit -m "Inject profile block into decide and recover prompts"
```

---

## Task 4: Parse the profile in the backend WebSocket handler

**Files:**
- Modify: `backend/main.py`

- [ ] **Step 1: Add the import**

In `backend/main.py`, find the schemas import line:

```python
from agent.schemas import AgentState, PageContext, DOMNode
```

Change it to include `UserProfile`:

```python
from agent.schemas import AgentState, PageContext, DOMNode, UserProfile
```

- [ ] **Step 2: Build the profile into AgentState**

In `backend/main.py`, inside the `if msg["type"] == "start_task":` branch, replace the `state = AgentState(...)` assignment with:

```python
                profile_data = msg.get("profile")
                state = AgentState(
                    task=msg["task"],
                    # profile is never logged — do not add it to any log line
                    profile=UserProfile(**profile_data) if profile_data else None,
                    context=PageContext(
                        url=msg["url"],
                        title=msg["title"],
                        dom_tree=[DOMNode(**n) for n in msg["dom_tree"]]
                    )
                )
```

Leave the existing `logger.info("=== NEW TASK: %r ===", msg["task"])` line unchanged — it logs only the task string, never the profile.

- [ ] **Step 3: Verify the module imports cleanly**

Run: `cd backend && python -c "import main; print('import ok')"`
Expected: prints `import ok` with no traceback.

- [ ] **Step 4: Run the full backend test suite**

Run: `cd backend && python -m pytest -q`
Expected: all tests pass (existing suite + `test_profile.py`).

- [ ] **Step 5: Commit**

```bash
git add backend/main.py
git commit -m "Parse profile from start_task into AgentState"
```

---

## Task 5: Register and build the options page

**Files:**
- Modify: `extension/manifest.json`
- Create: `extension/options.html`

- [ ] **Step 1: Register the options page in the manifest**

In `extension/manifest.json`, add an `options_page` key after the `"action"` block. The file currently ends:

```json
  "action": {
    "default_icon": "icons/icon128.png"
  }
}
```

Change it to:

```json
  "action": {
    "default_icon": "icons/icon128.png"
  },
  "options_page": "options.html"
}
```

- [ ] **Step 2: Create the options page**

Create `extension/options.html`:

```html
<!DOCTYPE html>
<!-- The user's saved info. Stored in chrome.storage.local and sent to the
     backend with each task so the agent can fill matching form fields. -->
<html>
<head>
<meta charset="utf-8">
<title>Helper — My info</title>
<style>
  body {
    font-family: -apple-system, sans-serif;
    max-width: 480px;
    margin: 32px auto;
    padding: 0 20px;
    color: #111827;
  }
  h1 { font-size: 22px; margin-bottom: 4px; }
  p.sub { color: #6b7280; font-size: 14px; margin-top: 0; }
  label { display: block; margin: 14px 0 4px; font-size: 13px; font-weight: 600; }
  input, textarea {
    width: 100%;
    box-sizing: border-box;
    padding: 8px;
    border: 1px solid #e5e7eb;
    border-radius: 6px;
    font-size: 14px;
    color: #111827;
    background: #ffffff;
  }
  textarea { min-height: 70px; resize: vertical; }
  .row { display: flex; gap: 10px; }
  .row > div { flex: 1; }
  .toggle { display: flex; align-items: center; gap: 8px; margin: 20px 0; font-size: 14px; }
  .toggle input { width: auto; }
  .hint { color: #b45309; font-size: 12px; min-height: 14px; margin-top: 2px; }
  #save {
    margin-top: 16px;
    padding: 10px 20px;
    border: none;
    border-radius: 8px;
    background: #3B82F6;
    color: white;
    font-size: 15px;
    font-weight: 600;
    cursor: pointer;
  }
  #save:hover { background: #2563eb; }
  #status { margin-left: 12px; color: #16a34a; font-size: 14px; }
</style>
</head>
<body>
  <h1>My info</h1>
  <p class="sub">Your helper uses this to fill out forms for you. Stored on this
     computer and sent to your helper's backend only while it works on a task.</p>

  <label for="fullName">Full name</label>
  <input id="fullName" autocomplete="off">

  <div class="row">
    <div>
      <label for="email">Email</label>
      <input id="email" autocomplete="off">
      <div class="hint" id="emailHint"></div>
    </div>
    <div>
      <label for="phone">Phone</label>
      <input id="phone" autocomplete="off">
    </div>
  </div>

  <label for="street">Street address</label>
  <input id="street" autocomplete="off">

  <div class="row">
    <div>
      <label for="city">City</label>
      <input id="city" autocomplete="off">
    </div>
    <div>
      <label for="state">State/region</label>
      <input id="state" autocomplete="off">
    </div>
  </div>

  <div class="row">
    <div>
      <label for="zip">ZIP/postcode</label>
      <input id="zip" autocomplete="off">
    </div>
    <div>
      <label for="country">Country</label>
      <input id="country" autocomplete="off">
    </div>
  </div>

  <label for="notes">Notes (anything else your helper should know)</label>
  <textarea id="notes"></textarea>

  <div class="toggle">
    <input type="checkbox" id="useProfile" checked>
    <label for="useProfile" style="margin:0;font-weight:400;">Let my helper use this info</label>
  </div>

  <button id="save">Save</button>
  <span id="status"></span>

  <script src="options.js"></script>
</body>
</html>
```

- [ ] **Step 3: Verify the manifest is valid JSON**

Run: `python -c "import json; json.load(open('extension/manifest.json')); print('manifest ok')"`
Expected: prints `manifest ok`.

- [ ] **Step 4: Commit**

```bash
git add extension/manifest.json extension/options.html
git commit -m "Add options page for the user profile"
```

---

## Task 6: Options page logic (load / save)

**Files:**
- Create: `extension/options.js`

- [ ] **Step 1: Create the options script**

Create `extension/options.js`:

```javascript
// Loads the saved profile into the form and writes edits back to
// chrome.storage.local. Field ids here match the UserProfile keys the backend
// expects (see backend/agent/schemas.py).

const FIELDS = ['fullName', 'email', 'phone', 'street', 'city', 'state', 'zip', 'country', 'notes']
const statusEl = document.getElementById('status')
const emailHint = document.getElementById('emailHint')
const useProfileEl = document.getElementById('useProfile')

// Populate the form from storage on open.
chrome.storage.local.get(['userProfile', 'useProfile']).then(({ userProfile, useProfile }) => {
    const p = userProfile || {}
    for (const id of FIELDS) {
        const el = document.getElementById(id)
        if (el) el.value = p[id] || ''
    }
    // default the toggle ON when it has never been set
    useProfileEl.checked = useProfile !== false
}).catch(() => {})

// Non-blocking email hint — never prevents saving.
function updateEmailHint() {
    const v = document.getElementById('email').value.trim()
    emailHint.textContent = (v && !/^[^@\s]+@[^@\s]+\.[^@\s]+$/.test(v))
        ? "That doesn't look like an email — saved anyway."
        : ''
}
document.getElementById('email').addEventListener('input', updateEmailHint)

document.getElementById('save').addEventListener('click', () => {
    const profile = {}
    for (const id of FIELDS) {
        const el = document.getElementById(id)
        const v = (el?.value || '').trim()
        if (v) profile[id] = v   // store only non-empty fields
    }
    updateEmailHint()
    chrome.storage.local.set({ userProfile: profile, useProfile: useProfileEl.checked })
        .then(() => {
            statusEl.textContent = 'Saved ✓'
            setTimeout(() => { statusEl.textContent = '' }, 2000)
        })
        .catch(() => { statusEl.textContent = 'Could not save' })
})
```

- [ ] **Step 2: Verify syntax**

Run: `node --check extension/options.js && echo "options.js OK"`
Expected: prints `options.js OK`.

- [ ] **Step 3: Commit**

```bash
git add extension/options.js
git commit -m "Add options page load/save logic"
```

---

## Task 7: Content script — "My info" button + send profile on task start

**Files:**
- Modify: `extension/content.js`

- [ ] **Step 1: Add the "My info" button to the bubble markup**

In `extension/content.js`, find the bubble `innerHTML` template. Locate this line:

```javascript
    <button id="a11y-agent-wake">👂 Say "Hey Helper": Off</button>
```

Add a new button immediately after it:

```javascript
    <button id="a11y-agent-myinfo">⚙ My info</button>
```

- [ ] **Step 2: Grab the button element and wire it to open the options page**

In `extension/content.js`, find the element-handle block that ends with:

```javascript
const status = document.getElementById('a11y-agent-status')
```

Add after it:

```javascript
const myInfoBtn = document.getElementById('a11y-agent-myinfo')

myInfoBtn.addEventListener('click', () => {
    try { chrome.runtime.openOptionsPage() } catch {}
})
```

- [ ] **Step 3: Add a prune helper and read the profile when a task starts**

In `extension/content.js`, replace the entire `startTask` function:

```javascript
function startTask(task) {
    status.textContent = 'Thinking...'
    renderChecklist('')  // clear any previous task's plan
    setTaskRunning(true)
    sendToBackground({
        type: 'start_task',
        task,
        url: window.location.href,
        title: document.title,
        dom_tree: extractAccessibilityTree()
    })
}
```

with:

```javascript
// keep only non-empty string fields, trimmed; return null if nothing is left,
// so an empty profile is never sent and the backend behaves as if there is none
function pruneProfile(p) {
    if (!p || typeof p !== 'object') return null
    const out = {}
    for (const [k, v] of Object.entries(p)) {
        if (typeof v === 'string' && v.trim()) out[k] = v.trim()
    }
    return Object.keys(out).length ? out : null
}

async function startTask(task) {
    status.textContent = 'Thinking...'
    renderChecklist('')  // clear any previous task's plan
    setTaskRunning(true)
    let profile = null
    try {
        const { userProfile, useProfile } = await chrome.storage.local.get(['userProfile', 'useProfile'])
        if (useProfile !== false) profile = pruneProfile(userProfile)  // default-on
    } catch {}
    sendToBackground({
        type: 'start_task',
        task,
        url: window.location.href,
        title: document.title,
        dom_tree: extractAccessibilityTree(),
        ...(profile ? { profile } : {})
    })
}
```

- [ ] **Step 4: Verify syntax**

Run: `node --check extension/content.js && echo "content.js OK"`
Expected: prints `content.js OK`.

- [ ] **Step 5: Commit**

```bash
git add extension/content.js
git commit -m "Add My info button and send profile on task start"
```

---

## Task 8: Background worker — forward the profile over the agent socket

**Files:**
- Modify: `extension/background.js`

- [ ] **Step 1: Include the profile in the start_task payload**

In `extension/background.js`, inside `startTask`, find the `ws.onopen` payload:

```javascript
    ws.onopen = () => {
        ws.send(JSON.stringify({
            type: 'start_task',
            task: msg.task,
            url: msg.url,
            title: msg.title,
            dom_tree: msg.dom_tree
        }))
    }
```

Replace it with:

```javascript
    ws.onopen = () => {
        ws.send(JSON.stringify({
            type: 'start_task',
            task: msg.task,
            url: msg.url,
            title: msg.title,
            dom_tree: msg.dom_tree,
            // present only when the user has a profile and hasn't disabled it
            ...(msg.profile ? { profile: msg.profile } : {})
        }))
    }
```

- [ ] **Step 2: Verify syntax**

Run: `node --check extension/background.js && echo "background.js OK"`
Expected: prints `background.js OK`.

- [ ] **Step 3: Commit**

```bash
git add extension/background.js
git commit -m "Forward profile in the start_task WebSocket payload"
```

---

## Task 9: End-to-end verification

Confirms a saved profile reaches the backend prompt. Uses the headless-Chrome + CDP harness pattern already used in this repo's debugging.

- [ ] **Step 1: Confirm the whole backend suite still passes**

Run: `cd backend && python -m pytest -q`
Expected: all pass.

- [ ] **Step 2: Manual smoke test (recommended before merge)**

1. Start the backend: `cd backend && uvicorn main:app --port 8000` (ensure `backend/.env` has `ANTHROPIC_API_KEY`).
2. Load the unpacked `extension/` in Chrome (`chrome://extensions` → Developer mode → Load unpacked), or reload it if already loaded.
3. Right-click the extension icon → **Options** (or open the bubble on any page and click **⚙ My info**). Enter an email and name, leave "Let my helper use this info" checked, click **Save**.
4. On a normal website with a form (e.g. a demo signup page), open the bubble and give a task like "fill in the email field with my email".
5. In the backend console, confirm the agent fills the field with the saved value. Confirm the profile value does **not** appear in any backend log line (only the task string and DOM are logged).

- [ ] **Step 3: Confirm the toggle suppresses sending**

Uncheck "Let my helper use this info", Save, and start another task. Confirm no profile block influences the run (the agent has no saved values to use).

- [ ] **Step 4: Final commit (if any manual-test tweaks were needed)**

```bash
git add -A
git commit -m "Verify user-profile autofill end to end"
```

---

## Notes for the implementer

- **Do not log the profile.** No new log line in `main.py` or `claude.py` may include profile values. The existing `=== NEW TASK ===` log uses only `msg["task"]` — leave it.
- **`useProfile` default is ON.** Absent (`undefined`) means enabled; only an explicit `false` disables. This is intentional and matches the options page default.
- **Field names are the contract.** The `id`s in `options.html`, the keys in `pruneProfile`, and the `UserProfile` fields in `schemas.py` must stay identical (`fullName`, `email`, `phone`, `street`, `city`, `state`, `zip`, `country`, `notes`).
- **No new action type.** Claude fills forms with the existing `type` action; `content.js` execution is unchanged.
