import asyncio
import logging
import os
import re
import time
import json
from dotenv import load_dotenv
import anthropic
from anthropic import AsyncAnthropic
from agent.schemas import AgentState, AgentAction

logger = logging.getLogger("agent.claude")

load_dotenv()

MODEL = "claude-sonnet-4-5"

# The org's per-minute input-token rate limit. We pace requests to stay under
# a slightly lower ceiling (headroom for output tokens + estimation error).
# Override with RATE_LIMIT_TOKENS_PER_MIN in .env if your tier is higher.
RATE_LIMIT_PER_MIN = int(os.getenv("RATE_LIMIT_TOKENS_PER_MIN", "30000"))
TOKEN_BUDGET_PER_MIN = int(RATE_LIMIT_PER_MIN * 0.85)

# Hard ceiling on input tokens per request. The DOM tree gets whatever budget
# remains after the fixed prompt parts (system prompt, tool schemas, task,
# checklist, action history) — so one call can never blow the per-minute cap.
MAX_INPUT_TOKENS = int(os.getenv("MAX_INPUT_TOKENS", "10000"))
_PROMPT_OVERHEAD_TOKENS = 2500  # reserved for everything that isn't the DOM tree
_DOM_CHAR_BUDGET = max(MAX_INPUT_TOKENS - _PROMPT_OVERHEAD_TOKENS, 1000) * 4

# Cap on how much of the accessibility tree we send. A single Gmail-class page
# emits 400+ nodes (~35K tokens pretty-printed) — more than the whole per-minute
# budget in ONE call. Compact JSON + a node cap keeps each call to a few K tokens.
MAX_DOM_NODES = int(os.getenv("MAX_DOM_NODES", "120"))
# Repeated structures (email rows, product cards, search results) are capped at
# the first N per group — the agent sees the top of the list plus the controls
# (search box, nav) it needs to narrow things down further.
MAX_GROUP_NODES = int(os.getenv("MAX_GROUP_NODES", "10"))
_TEXT_CAP = 80

_client = None

def get_client():
    global _client
    if _client is None:
        api_key = os.getenv("ANTHROPIC_API_KEY")
        if not api_key:
            raise RuntimeError("ANTHROPIC_API_KEY not found in .env file")
        _client = AsyncAnthropic(api_key=api_key)
    return _client


def _compact_node(n) -> str:
    """One node as compact JSON — drops empty fields, shortens text."""
    node = {"tag": n.tag, "selector": n.selector}
    text = (n.text or "").strip()
    if text:
        node["text"] = text[:_TEXT_CAP]
    if n.label:
        node["label"] = n.label[:_TEXT_CAP]
    if n.role:
        node["role"] = n.role
    if n.value:
        node["value"] = n.value[:_TEXT_CAP]
    return json.dumps(node, separators=(",", ":"))


def _group_key(n) -> tuple:
    """Structural signature for spotting repeated content (email rows, cards).

    Numbers and quoted text in the selector are masked so tr:nth-of-type(1)
    and tr:nth-of-type(37) land in the same group.
    """
    pattern = re.sub(r'"[^"]*"', '"…"', n.selector or "")
    pattern = re.sub(r"\d+", "N", pattern)
    return (n.tag, n.role or "", pattern)


def _node_priority(n) -> int:
    """Lower = more important to the agent. Controls beat content."""
    tag = n.tag
    role = (n.role or "").lower()
    label = (n.label or "").lower()
    if tag in ("input", "textarea", "select") or role in ("searchbox", "search", "textbox", "combobox"):
        return 0  # the agent's levers — search/filter inputs always survive
    if "search" in label:
        return 0
    if tag == "button" or role in ("button", "tab", "menuitem", "checkbox", "radio", "switch"):
        return 1
    if tag in ("h1", "h2", "h3", "label") or role == "heading":
        return 2
    if tag == "a" or role == "link":
        return 3
    return 4


def serialize_dom(dom_tree) -> str:
    """Compact the accessibility tree to the top-k elements under a token cap.

    Three stages:
    1. Cap repeated structures at the first MAX_GROUP_NODES per structural
       group — on Gmail this keeps the top ~10 email rows instead of all 400+.
    2. Rank survivors by importance (inputs > buttons > headings > links >
       rest, document order as tiebreak) and admit them until the node cap or
       the character budget derived from MAX_INPUT_TOKENS runs out.
    3. Emit the chosen nodes in document order so the page still reads
       top-to-bottom.
    """
    group_counts: dict[tuple, int] = {}
    survivors = []
    for i, n in enumerate(dom_tree):
        key = _group_key(n)
        seen = group_counts.get(key, 0)
        group_counts[key] = seen + 1
        if seen < MAX_GROUP_NODES:
            survivors.append((i, n))

    ranked = sorted(survivors, key=lambda t: (_node_priority(t[1]), t[0]))
    chosen: dict[int, str] = {}
    chars = 0
    for i, n in ranked:
        if len(chosen) >= MAX_DOM_NODES:
            break
        entry = _compact_node(n)
        if chars + len(entry) + 1 > _DOM_CHAR_BUDGET:
            continue  # a smaller node later in the ranking may still fit
        chosen[i] = entry
        chars += len(entry) + 1

    out = "[" + ",".join(chosen[i] for i in sorted(chosen)) + "]"
    hidden = len(dom_tree) - len(chosen)
    if hidden > 0:
        out += (
            f"\n({hidden} more elements hidden to fit the token budget — "
            f"repeated items (lists, rows) are truncated to the first "
            f"{MAX_GROUP_NODES}; use the page's search/filter controls or "
            f"scroll to reveal more)"
        )
    return out


class _TokenBudget:
    """Sliding-window limiter that keeps input tokens under the per-minute cap.

    Before each call we estimate its input tokens and, if the trailing-60s
    window would exceed the budget, sleep until enough of it ages out. This is
    the 'pause in between' — requests self-pace instead of hitting 429s.
    """

    def __init__(self, max_per_min: int):
        self.max = max_per_min
        self.window: list[tuple[float, int]] = []  # (timestamp, est_tokens)
        self.lock = asyncio.Lock()

    async def acquire(self, est_tokens: int, label: str) -> None:
        async with self.lock:
            while True:
                now = time.monotonic()
                self.window = [(t, n) for t, n in self.window if now - t < 60]
                used = sum(n for _, n in self.window)
                # let an oversized lone request through — sleeping can't help it
                if not self.window or used + est_tokens <= self.max:
                    self.window.append((now, est_tokens))
                    return
                oldest = min(t for t, _ in self.window)
                sleep_for = 60 - (now - oldest) + 0.2
                logger.info(
                    "  [throttle] %s — %d/%d tok used this minute, pausing %.1fs",
                    label, used, self.max, sleep_for,
                )
                await asyncio.sleep(sleep_for)


_budget = _TokenBudget(TOKEN_BUDGET_PER_MIN)


def _estimate_tokens(kwargs: dict) -> int:
    """Rough input-token estimate (~4 chars/token) for budgeting, pre-call."""
    chars = len(kwargs.get("system", "") or "")
    for m in kwargs.get("messages", []):
        content = m.get("content", "")
        chars += len(content) if isinstance(content, str) else len(json.dumps(content))
    chars += len(json.dumps(kwargs.get("tools", []) or []))
    return chars // 4 + 200  # small fixed overhead


async def _call_claude(label: str, **kwargs):
    """Call the Claude API with exponential-backoff retry on rate limits."""
    max_retries = 4
    est_tokens = _estimate_tokens(kwargs)
    for attempt in range(max_retries):
        await _budget.acquire(est_tokens, label)
        logger.info("  [Claude] %s — calling API (attempt %d, ~%d input tok)",
                    label, attempt + 1, est_tokens)
        try:
            response = await get_client().messages.create(**kwargs)
            usage = response.usage
            logger.info(
                "  [Claude] %s — done  in=%d out=%d tokens",
                label, usage.input_tokens, usage.output_tokens,
            )
            return response
        except anthropic.RateLimitError as exc:
            if attempt == max_retries - 1:
                logger.error("  [Claude] %s — rate limit, no retries left: %s", label, exc)
                raise
            wait = 5 * 2 ** attempt  # 5, 10, 20 s
            logger.warning(
                "  [Claude] %s — rate limit hit, retrying in %ds (%d/%d)",
                label, wait, attempt + 1, max_retries - 1,
            )
            await asyncio.sleep(wait)


ACTION_TOOL = {
    "name": "execute_action",
    "description": "Execute an action on the current webpage to progress toward the user's task.",
    "input_schema": {
        "type": "object",
        "properties": {
            "type": {
                "type": "string",
                "enum": ["click", "type", "scroll", "highlight", "wait", "press_enter",
                         "navigate", "back", "forward", "reload", "new_tab",
                         "done", "failed"],
            },
            "selector": {"type": "string"},
            "value": {"type": "string"},
            "description": {"type": "string"},
            "reasoning": {"type": "string"},
            "updated_checklist": {
                "type": "string",
                "description": (
                    "The ENTIRE checklist with any newly completed step flipped from "
                    "'[ ]' to '[x]'. Include this ONLY when the current page state "
                    "confirms a step just finished — omit it otherwise. Keep the exact "
                    "'[ ] text' / '[x] text' one-item-per-line format. Do not add, "
                    "remove, or reword items here."
                ),
            },
        },
        "required": ["type", "description"]
    }
}

PLAN_TOOL = {
    "name": "create_plan",
    "description": "Decompose the user's task into an ordered checklist of concrete steps.",
    "input_schema": {
        "type": "object",
        "properties": {
            "checklist": {
                "type": "string",
                "description": (
                    "One step per line, each starting with '[ ] '. Steps must be "
                    "concrete page actions (e.g. '[ ] click the search icon', "
                    "'[ ] type the query into the search box', '[ ] press enter or "
                    "click submit'), in execution order."
                ),
            }
        },
        "required": ["checklist"]
    }
}

SYSTEM_PROMPT = """You are an accessibility agent helping a non-technical user
navigate the web. You'll receive the page's accessibility tree, the user's task,
and a CHECKLIST of steps for the overall task. Call the execute_action tool with
a single next action.

Checklist rules:
- '[ ]' = pending step, '[x]' = completed step. Work through pending steps in order.
- Multi-step tasks require MULTIPLE actions across turns. Opening a search bar is
  NOT searching; typing a query is NOT submitting it. Do each step.
- When the current page state confirms a step finished, return the whole checklist
  in updated_checklist with that step flipped to '[x]'. Never flip a box based on
  an action you only INTEND to take — only based on what the page shows.
- Do NOT restructure the checklist here (no adding/removing/rewording items).

Your capabilities (everything a regular user can do):
- Page actions (need a selector from the accessibility tree): "click", "type"
  (text in "value"), "press_enter" (press Enter in the input you just typed
  in — the normal way to submit a search), "scroll", "highlight", "wait".
- Browser actions (no selector — the browser executes them for you):
  "navigate" (full URL in "value"), "back", "forward", "reload",
  "new_tab" (full URL in "value"; the task continues in the new tab).

Operating boundary:
- For page actions you can ONLY use selectors from the provided accessibility
  tree. Browser UI (address bar, bookmarks, menus) does NOT exist for you —
  the browser actions above replace it. Don't invent selectors for it.
- The accessibility tree is TRUNCATED to the most important elements; long
  lists (emails, results, products) show only their first items. If something
  you need isn't listed, it may still be on the page — prefer the page's own
  search or filter inputs to narrow down to it, or scroll to reveal more.
- If you are told your previous action FAILED to execute, it did NOT happen,
  no matter what your action history says. Never flip its checklist box;
  pick a different element or approach (e.g. after a failed click, try
  "navigate" directly, or "back" to return to a working page).

Use "done" ONLY when every checklist item is '[x]' and the user's full task is
complete. Use "failed" only if the task is impossible from the current state.
Be patient, explain each step clearly, prefer the safest action."""

PLAN_PROMPT = """You plan web-navigation tasks for an accessibility agent that
helps non-technical users. Given a task and the current page, call create_plan
with a short ordered checklist of concrete page actions.

Keep it minimal (typically 2-6 steps). Each step must be a single observable
action on a page. Cover the FULL task — e.g. a search task includes opening the
search input, typing the query, AND submitting it (pressing enter).

The agent can also use the browser itself: navigate to a URL, go back/forward,
reload, and open a new tab — plan with those when the task spans sites
(e.g. '[ ] navigate to google.com', '[ ] type the query', '[ ] press enter')."""

RECOVERY_PROMPT = """You are an accessibility agent that got STUCK.

Your last few actions did not produce expected results. Stop and reconsider.
Pick a DIFFERENT approach than what you've been trying.

You will see the current checklist ('[ ]' pending / '[x]' done). If the plan
itself is wrong — a step is impossible on this site, or a different path is
needed — return a REVISED checklist in updated_checklist: keep completed '[x]'
items as-is, and replace/add/reorder the pending steps to reflect the new
approach. If the plan is fine and only the action needs to change, omit
updated_checklist.

Call the execute_action tool with your new action."""


async def stream_plan(state: AgentState) -> str:
    """One-shot decomposition of the task into a checklist string."""
    user_message = f"""Task: {state.task}

Current URL: {state.context.url}
Page title: {state.context.title}

Accessibility tree:
{serialize_dom(state.context.dom_tree)}

Create the checklist for this task."""

    response = await _call_claude(
        "plan",
        model="claude-sonnet-4-5",
        max_tokens=1024,
        system=PLAN_PROMPT,
        tools=[PLAN_TOOL],
        tool_choice={"type": "tool", "name": "create_plan"},
        messages=[{"role": "user", "content": user_message}]
    )

    tool_use = next(block for block in response.content if block.type == "tool_use")
    return tool_use.input["checklist"].strip()


async def stream_action(state: AgentState, rejection_note: str = None) -> AgentAction:
    user_message = f"""Task: {state.task}

Checklist (work through pending '[ ]' items in order):
{state.checklist or '(no checklist — complete the task directly)'}

Current URL: {state.context.url}
Page title: {state.context.title}

Accessibility tree:
{serialize_dom(state.context.dom_tree)}

Previous actions: {json.dumps([a.model_dump(exclude={'updated_checklist'}, exclude_none=True) for a in state.actions_taken[-5:]], separators=(",", ":"))}

What is the next action?"""

    if state.last_action_result:
        user_message += (
            f"\n\nWARNING: your previous action FAILED to execute on the page: "
            f"{state.last_action_result}. The page never received it. Do not "
            f"mark its checklist item complete — choose a different element or "
            f"approach."
        )

    if rejection_note:
        user_message += f"\n\nIMPORTANT: {rejection_note}"

    response = await _call_claude(
        "decide",
        model="claude-sonnet-4-5",
        max_tokens=1024,
        system=SYSTEM_PROMPT,
        tools=[ACTION_TOOL],
        tool_choice={"type": "tool", "name": "execute_action"},
        messages=[{"role": "user", "content": user_message}]
    )

    tool_use = next(block for block in response.content if block.type == "tool_use")
    return AgentAction(**tool_use.input)


async def stream_recovery_action(state: AgentState) -> AgentAction:
    user_message = f"""Task: {state.task}

Checklist ('[ ]' pending / '[x]' done — revise it via updated_checklist if the plan itself is wrong):
{state.checklist or '(no checklist)'}

Current URL: {state.context.url}
Page title: {state.context.title}

YOU ARE STUCK. Here's what you've tried (last 6 actions):
{json.dumps([a.model_dump(exclude={'updated_checklist'}, exclude_none=True) for a in state.actions_taken[-6:]], separators=(",", ":"))}

{f"Your most recent action FAILED to execute: {state.last_action_result}. It never reached the page." if state.last_action_result else ""}

Current accessibility tree:
{serialize_dom(state.context.dom_tree)}

Reconsider and choose a DIFFERENT next action."""

    response = await _call_claude(
        "recover",
        model="claude-sonnet-4-5",
        max_tokens=1024,
        system=RECOVERY_PROMPT,
        tools=[ACTION_TOOL],
        tool_choice={"type": "tool", "name": "execute_action"},
        messages=[{"role": "user", "content": user_message}]
    )

    tool_use = next(block for block in response.content if block.type == "tool_use")
    return AgentAction(**tool_use.input)