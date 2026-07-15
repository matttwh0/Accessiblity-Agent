# Agent Workflow — From Question to Execution

This doc walks through everything that happens between a user typing a task into
the bubble and the agent declaring it "done", with the functions, state, and
timing at each step.

## The big picture

Two programs cooperate over a WebSocket:

- **The Chrome extension** (JavaScript) — the agent's *eyes and hands*. It reads
  the page, executes clicks/typing, and reports what happened. It contains no
  intelligence.
- **The backend** (Python / FastAPI) — the agent's *brain*. It holds all state,
  calls Claude to decide each action, and detects when the agent is stuck. It
  never touches a real webpage.

```
┌─────────────────────── Chrome ───────────────────────┐        ┌────────── Backend ──────────┐
│  content.js (per page)      background.js (worker)   │        │  main.py (WebSocket loop)   │
│  ─ bubble UI                ─ owns the WebSocket     │  ws:// │  ─ perceive()      ┐        │
│  ─ extract a11y tree        ─ session per tab        │◄──────►│  ─ decide_action() │nodes.py│
│  ─ execute actions          ─ browser actions        │  :8000 │    (plans on turn 1)│       │
│  ─ wait for page to settle  ─ survives navigations   │        │  ─ verify()        │        │
│                                                      │        │  ─ recover()       ┘        │
│                                                      │        │        └── claude.py (LLM)  │
└──────────────────────────────────────────────────────┘        └─────────────────────────────┘
```

One loop iteration = one *step*: perceive → decide (LLM) → execute (browser) →
report → verify. The loop runs until status is `done`, `failed`, or 15 steps.

> **Note:** `graph.py` builds a langgraph `StateGraph` wiring these same nodes,
> but the live WebSocket path in `main.py` calls the node functions **directly**
> (the graph is only used by the `POST /test` endpoint). The langgraph edges and
> the manual calls implement the same loop.

## The two core data structures

Everything flows through two Pydantic models in `backend/agent/schemas.py`:

**`AgentState`** — the single source of truth, threaded through every node.
Think of it as the agent's working memory:

| Field | What it means |
|---|---|
| `task` | The user's request, verbatim ("find cheap flights to Tokyo") |
| `context` | What the page looks like right now: URL, title, `dom_tree` |
| `checklist` | The plan, as markdown: `[ ] pending` / `[x] done`, one step per line |
| `actions_taken` | Full history of every `AgentAction` so far |
| `steps` / `max_steps` | Loop counter; hard cap of 15 |
| `status` | `planning → executing → verifying → (stuck → recovering) → done/failed` |
| `last_action_result` | `None` = last action executed; a string = why it did NOT |
| `last_expectation_met` | Did the action's predicted outcome come true? |
| `stuck_count`, `recovery_attempts` | Stuck-detection bookkeeping (recover caps at 2) |
| `previous_url`, `previous_dom_hash` | Snapshot taken in `perceive()`, compared in `verify()` |

**`AgentAction`** — one decision from Claude. `type` (click / type / scroll /
press_enter / navigate / back / … / done / failed), `selector` (which element),
`value` (text to type or URL), `description` (narration shown to the user),
`expect` (the *predicted outcome* — more below), and optionally
`updated_checklist` (checklist with a step flipped to `[x]`).

## Concept: the accessibility tree (the agent's perception)

The agent never sees pixels or raw HTML. `extractAccessibilityTree()`
(`content.js:77`) queries the page for interactive elements
(`button, a, input, select, textarea, [role], h1-h3, label`), filters out
invisible ones, and stamps each with an opaque id: `data-a11y-id="N"`.

**That id is the selector.** When Claude later says "click
`[data-a11y-id="17"]`", resolving it is an exact `querySelector` lookup — it
can't match the wrong element the way a text-based selector could. The ids are
per-snapshot: stale stamps are cleared before every extraction, so the agent
always acts on its freshest view.

Each node carries `{tag, text, label, role, selector, value}` — `value` matters
because it lets the backend *see* that typing into an input actually changed
something.

**Settling:** on SPAs the interesting controls often hydrate after page load, so
`extractWhenSettled()` (`content.js:110`) uses a `MutationObserver` to wait for
a 200 ms quiet gap (capped at 2.5 s) before snapshotting. Snapshotting too early
strands the agent — it can't click what it never saw.

**Dropdowns:** a `<select>`'s option list is browser UI that never appears in
the DOM, so each select node carries its choices in an `options` field and the
agent is instructed to pick one via `type` (which sets the value + fires
`change`) instead of clicking. Custom hover menus get synthetic
`mouseover`/`mouseenter` events before every click so they actually open.

## Concept: DOM serialization (fitting a page into a token budget)

A Gmail-class page emits 400+ nodes ≈ 35K tokens — more than the entire
per-minute API budget in one call. `serialize_dom()` (`claude.py:164`) compacts
the tree in three stages:

1. **Group-cap repeats** — rows/cards with the same structural signature
   (`_group_key`) keep only their first 10. The agent sees the top of the inbox
   plus the controls (search box, nav) it needs to narrow further. Named
   links/buttons each keep their own group so "Code" and "Issues" don't collapse
   into one bucket.
2. **Rank by importance** (`_node_priority`): inputs (the agent's levers) >
   buttons > headings > links > everything else. Admit in rank order until the
   node cap (120) or character budget (~7.5K tokens' worth) runs out.
3. **Emit in document order** so the page still reads top-to-bottom, with a
   trailer telling the model how many elements were hidden and what to do about
   it ("use the page's search/filter controls").

There's also a *find-in-page* primitive: `search_terms` pins any node matching
what the agent was hunting for (mined from its own recent `expect` predictions)
into the output, exempt from all caps. Used during recovery.

## Concept: expectations (predict-then-check)

Instead of guessing when a page has "settled" after an action, Claude is asked
to *predict the observable outcome*: `expect.url_contains` for navigations,
`expect.text_contains` for search results, `expect.selector` for revealed
elements.

The extension then waits for that exact prediction to come true
(`awaitSettle()`, polling every 100 ms, capped at 2.5 s) rather than merely
waiting for DOM quiescence — with an early exit: if the page visibly reacted
(mutations) and then settled but the prediction still isn't true after ~1.2 s,
it's a misprediction (e.g. a dropdown opened instead of navigating) and the
extension reports immediately instead of paying the full cap. Two payoffs:

- **Latency**: report the instant the result lands, not after an arbitrary timeout.
- **Verification**: a prediction that never materialized (`expectation_met:
  false`) is a precise failure signal the backend uses for stuck detection.

## The walkthrough, step by step

Timings marked ⏱ are logged by the code (`[TIMING]` lines in the uvicorn
console). LLM inference times are typical for Sonnet 4.5 with these prompt
sizes; your exact numbers appear in the logs.

### Phase 0 — Page load (before any task)

Every page load, `content.js` injects the 🤖 bubble, waits for the DOM to settle
(⏱ 200 ms–2.5 s), and announces itself to the background worker with
`content_ready` + a fresh tree. This is also how a mid-task navigation resumes —
see Phase 5b.

### Phase 1 — The user asks (t = 0)

User clicks the bubble, types "look up cats on this website", presses Enter
(`content.js:53`). The content script extracts the tree and sends `start_task`
to `background.js`, which opens `ws://localhost:8000/agent` and forwards
`{task, url, title, dom_tree}`.

⏱ **~50–100 ms** (tree extraction ~10–50 ms on a big page + localhost WebSocket
handshake).

Backend (`main.py:84`): stamps `task_started_at`, calls `reset_usage()` (token
accounting starts fresh), constructs the initial `AgentState`, then runs
`perceive()` → `decide_action()`:

### Phase 2 — Planning (merged into the first decide — no separate call)

Planning no longer costs its own LLM call. On the first turn (`state.checklist`
is empty), `stream_action()` asks Claude to do both at once: create the 2–6 step
checklist AND take its first action, returning the plan via the action's
`updated_checklist` field:

```
[x] click the search icon      <- first action, already taken
[ ] type "cats" into the search box
[ ] press enter
```

If the merged call comes back without a checklist, `decide_action()` seeds a
single-item fallback (`[ ] <task>`) so the done-gate still has something to
gate on. A 4-step task is now exactly 4 LLM calls.

### Phase 3 — `perceive()` — ⏱ <1 ms

`nodes.py:47` is deliberately trivial: it snapshots `previous_url` and
`previous_dom_hash` (an MD5 over `(tag, text, selector, value)` of every node).
These are the *before* photo that `verify()` will compare against after the
action. No LLM.

### Phase 4 — `decide_action()` — one LLM call ⏱ ~2–5 s

`nodes.py:54` → `stream_action()` (`claude.py:585`). This is the hot path. The
user message contains: task, checklist, URL/title, serialized tree, and the last
5 actions. Claude must call the `execute_action` tool and return one
`AgentAction` — e.g.:

```json
{"type": "click", "selector": "[data-a11y-id=\"12\"]",
 "description": "Clicking the search icon",
 "expect": {"selector": "input[type=search]"}}
```

Two guards live here:

- **The done-gate** (`nodes.py:64`): if Claude says `done` while checklist items
  are unchecked, the answer is *rejected* and re-prompted with the pending items
  (1 retry — an extra LLM call ⏱ +2–5 s). This stops premature victory
  declarations.
- **Checklist updates**: if the action carries `updated_checklist`, it replaces
  `state.checklist` — but the model may only flip boxes based on what the page
  *shows*, never on intent.

`steps` increments; the action is appended to history. Logged as ⏱
`[TIMING] decide=X.XXs`.

Before every LLM call, two mechanisms in `claude.py` protect the rate limit:

- **`_TokenBudget`** (`claude.py:259`) — a sliding 60 s window of estimated
  input tokens. If this call would exceed ~25.5K tokens/min, it *sleeps* until
  enough of the window ages out (⏱ logged as `[throttle] … pausing X.Xs` —
  this is the one step that can add tens of seconds on token-heavy tasks).
- **Prompt caching** (`_cached_system`) — system prompt + tool schema are
  byte-identical every call, so they're served from Anthropic's cache after the
  first call (~0.1× cost, faster time-to-first-token). Watch `cache_read` in the
  logs.

### Phase 5 — Execution in the browser ⏱ ~0.4–2.5 s

The backend sends `{type: "action", action, status, checklist}` over the
WebSocket (⏱ server turn so far logged as `[TIMING] server turn`). From here,
`background.js:101` routes by action type:

**Page actions** (click, type, press_enter, scroll, …) → delivered to the
content script. `executeAction()` (`content.js:333`) resolves the selector,
outlines the element in blue, scrolls it into view instantly (⏱ 100 ms), then
performs the action. Clicks are preceded by synthetic hover events (so hover
menus open); `type` on a `<select>` picks the matching option; `type` elsewhere
does a *readback check* — if the value didn't stick (React controlled inputs
can reset it), that's a failure. `press_enter` dispatches real keyboard events
and calls `form.requestSubmit()`, because many sites listen to the form, not
the key.

Then `awaitSettle()` (⏱ 200 ms quiet-gap, or expectation-poll up to 2.5 s):

- **Expectation given** → poll every 100 ms until the prediction holds
  (`expectation_met: true`), the misprediction early-exit fires (page mutated +
  settled + ~1.2 s elapsed → `false`), or the 2.5 s cap (`false`).
- **No expectation** → wait for a 200 ms mutation-free gap.
- **Hard navigation** → `pagehide` fires; the old page goes *silent* (reporting
  from a dying page would ship a stale snapshot). See 5b.

**Browser actions** (navigate, back, reload, new_tab) → executed by the
background worker via `chrome.tabs` — no content script involved. A 6 s
fallback timer catches navigations that never happen (e.g. `back` with no
history) and reports them as failures.

### Phase 5b — Reporting back ⏱ ~10 ms (or one page load)

Two report paths, both ending in a `context_update` message to the backend:

- **Same-page action**: the content script sends `action_executed` with
  `{action_result, expectation_met, url, title, dom_tree}` (fresh tree).
- **Navigation**: the *new* page's `content_ready` (Phase 0 machinery, ⏱ page
  load + 200 ms–2.5 s settle) is the report. The predicted outcome is checked
  against the freshly loaded page — the session, owned by the background worker,
  survives the navigation. New tabs are followed too: `tabs.onCreated`
  transfers the session when the agent's own click opened them.

`action_result` is the anti-hallucination channel: `null` means "it happened",
a string means "it did NOT happen and here's why" — and the next prompt tells
Claude in bold not to believe its own action history.

### Phase 6 — `verify()` — ⏱ <1 ms, no LLM

Back in `main.py:128`, the backend stores the feedback and runs `verify()`
(`nodes.py:96`) — pure heuristics comparing the *before* photo from
`perceive()` to the new context:

| Case | Signal |
|---|---|
| 0 | Extension said the action never executed |
| 0b | Prediction failed AND page didn't change |
| 1 | Same action repeated back-to-back |
| 2 | Page didn't change after an action that should change it (URL + DOM hash both identical) |
| 3 | A→B→A→B cycling between two selectors |

(A failed prediction where the page *did* change — e.g. a click opened a menu
instead of navigating — is deliberately NOT stuck: the revealed items are in
the new tree, so the next decide handles it in one normal step.)

One suspicious step increments `stuck_count`; **two consecutive** flip
`status = "stuck"` (one-off glitches are forgiven — a clean step resets the
count to 0).

### Phase 7 — Loop or recover

**Normal path** (`status` still `executing`): `perceive()` → `decide_action()`
→ Phase 5 again. Each loop iteration costs roughly ⏱ **3–9 s** (one LLM call +
browser round-trip).

**Stuck path**: if the DOM hash shows the page is *provably static* (nothing
changed since the last snapshot), the backend skips the re-extract round-trip
entirely and recovers immediately — re-extracting a static page would return
the same tree. Only when the page recently changed (may still be loading) does
it send `collect_context`, and the extension re-extracts a *settled* snapshot
(⏱ 0.2–2.5 s) flagged `reperceive`. Either way, `recover()` (`nodes.py`) then
makes an LLM call ⏱ ~3–6 s with three upgrades:

1. **RECOVERY_PROMPT** — "you are STUCK, do something DIFFERENT", plus the last
   6 actions as evidence.
2. **Widened perception** — `recovery_budget()` doubles/triples the node cap and
   char budget, so an element truncated out of the default view gets another
   chance to appear.
3. **Find-in-page pinning** — terms mined from the agent's own failed
   predictions pin matching nodes into the serialization regardless of caps.

Recovery may also return a **revised checklist** (replacing pending steps while
keeping `[x]` ones). Capped at 2 attempts; the third strike is `status =
"failed"`. Logged as ⏱ `[TIMING] recover=X.XXs`.

### Phase 8 — Completion

When `decide_action()` (or `recover()`) returns `type: "done"` — and the
done-gate agrees — `status` becomes `done`. The final action flows to the
extension one last time (the bubble shows the closing narration and the fully
checked list), the session ends, and the backend logs the totals:

```
=== TASK ENDED  status=done  steps=4 ===
=== TOKEN USAGE: 5 API calls — in=9911 out=612 total=10523 tokens ===
=== [TIMING] TOTAL TRIP=21.87s  (5.47s/step avg) ===
```

## Timing budget at a glance

For a well-behaved 4-step task (no stuck, no throttle):

| Step | Cost | Notes |
|---|---|---|
| Tree extraction + settle | 0.2–2.5 s | MutationObserver quiet-gap (200 ms) |
| Planning | 0 s extra | merged into the first `decide_action` call |
| `perceive` | <1 ms | hash + snapshot |
| `decide_action` (1 LLM call) | 2–5 s | per step; the single done-gate retry adds 2–5 s |
| Browser execution + settle | 0.4–2.5 s | 100 ms scroll + action + settle/expectation (early-exit ~1.2 s on mispredictions) |
| Navigation (when it happens) | 1–4 s | page load + settle before `content_ready` |
| `verify` | <1 ms | pure heuristics |
| `recover` (1 LLM call) | 3–6 s | rare; re-extract skipped when the page is static |
| **Total, typical** | **~12–25 s** | dominated by LLM inference (~70 %) |

The wildcard is **throttling**: `_TokenBudget` sleeps when the 60 s window
would exceed ~25.5K input tokens. On token-heavy pages a pause of 10–50 s can
appear between steps — visible in the logs as `[throttle] … pausing X.Xs`.

## Token economics per task

- Every LLM call carries ~2.5K tokens of overhead (system prompt + tool schema
  + task + checklist + history) plus the DOM budget (≤ ~7.5K tokens), hard-capped
  at 10K input tokens per call (`MAX_INPUT_TOKENS`).
- The system prompt + tool schema are **cached** after the first call — each
  subsequent step reads them at ~0.1× cost.
- A typical 4-step task ≈ 4 calls (plan merged into the first decide ±
  recovery) ≈ 8–20K input tokens total, reported at task end by
  `usage_summary()`.

## Watching it live

Run the backend (`uvicorn main:app --reload --port 8000`) and every phase above
is a log line: `[PLAN]`, `[PERCEIVE]`, `[DECIDE]`, `[VERIFY]`, `[RECOVER]`,
`[DOM]` (how much of the tree survived serialization), `[Claude]` (per-call
tokens, cache hits, ⏱ inference time), `[TIMING]` (per-phase durations), and
`[throttle]`. Set `DEBUG_DOM=1` to also dump the full serialized tree the model
saw — the single most useful artifact when the agent loops or hallucinates a
control.
