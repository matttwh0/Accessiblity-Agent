import asyncio
import logging
import os
import re
import time
import json
from typing import Optional
from dotenv import load_dotenv
import anthropic
from anthropic import AsyncAnthropic
from pydantic import ValidationError
from agent.schemas import AgentState, AgentAction, ActionType, UserProfile

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
    if n.options:
        node["options"] = [o[:_TEXT_CAP] for o in n.options[:20]]
    if not n.visible:
        # item inside a closed dropdown/menu — clickable, just not on screen
        node["hidden"] = True
    return json.dumps(node, separators=(",", ":"))


def _group_key(n) -> tuple:
    """Structural signature for spotting repeated content (email rows, cards).

    Elements are addressed by an opaque per-snapshot id ([data-a11y-id="N"]), so
    the selector carries no semantic signal — grouping keys off the descriptive
    fields instead.

    A named link/button carries its identity in its text ("Code" vs "backend"
    are distinct controls, not structural repeats), so each keeps its own group:
    otherwise every named link on a page merges into one capped bucket and a
    repo's file links lose their slots to the nav bar, never reaching the model.
    Everything else (rows, list items, cards) groups by tag+role so real repeats
    still collapse to the first MAX_GROUP_NODES.
    """
    tag = n.tag
    role = n.role or ""
    text = (n.text or "").strip()
    if tag in ("a", "button") and text:
        return (tag, role, text[:_TEXT_CAP])
    return (tag, role)


def _node_priority(n) -> int:
    """Lower = more important to the agent. Controls beat content."""
    tag = n.tag
    role = (n.role or "").lower()
    label = (n.label or "").lower()
    base = 4
    if tag in ("input", "textarea", "select") or role in ("searchbox", "search", "textbox", "combobox"):
        base = 0  # the agent's levers — search/filter inputs always survive
    elif "search" in label:
        base = 0
    elif tag == "button" or role in ("button", "tab", "menuitem", "checkbox", "radio", "switch"):
        base = 1
    elif tag in ("h1", "h2", "h3", "label") or role == "heading":
        base = 2
    elif tag == "a" or role == "link":
        base = 3
    # closed-menu items are useful context but must never crowd out the
    # controls the user can actually see
    if not n.visible:
        base = max(base, 3) + 1
    return base


def node_signature(n) -> str:
    """Identity of a node across snapshots. Selectors are opaque per-snapshot
    ids, so identity lives in the descriptive fields; visibility is included so
    a hidden menu item flipping visible (its menu opened) counts as new."""
    return "|".join((
        n.tag,
        (n.text or "").strip()[:_TEXT_CAP],
        (n.label or "")[:_TEXT_CAP],
        n.role or "",
        "1" if n.visible else "0",
    ))


# Words that carry no target identity: articles/pronouns/question words, plus
# the generic action and web nouns nearly every task contains. What's left in
# a task like "how do i check what i bought in the past" is the payload:
# "bought", "past".
_STOPWORDS = frozenset("""
a an the and or but of to in on at for with from by as my me i you your we our
us is are was were be been am do does did done can could would should will
how what where when which who whose why it its this that these those there
then than please help helper want wants need needs like get got give go goes
going gone come back find found show shows shown open opens click clicks
press check checks look looks looking see seen take takes make makes made
page pages website websites site sites web link links button buttons menu
menus tab tabs bar into onto about not no yes so just some any all each if
have has had they them he she his her hers theirs something anything thing
things stuff way ways out over under again more most very really
""".split())


def mine_task_terms(task: str, checklist: str = "") -> list[str]:
    """Literal fallback search terms: content words from the task and the first
    pending checklist item. Deterministic and free — the model's search_hints
    cover the vocabulary gap ("bought" vs "Orders"); these cover the case where
    the user already used the site's own word ("find watchlist")."""
    source = task
    for line in (checklist or "").splitlines():
        s = line.strip()
        if s.startswith("[ ]"):
            source += " " + s[3:]
            break  # only the step being worked on names the current target
    out, seen = [], set()
    for w in re.findall(r"[a-z0-9']+", source.lower()):
        if len(w) < 3 or w in _STOPWORDS or w in seen:
            continue
        seen.add(w)
        out.append(w)
    return out[:10]


# A term matching more nodes than this identifies nothing on this page (e.g.
# "amazon" on amazon.com) and is dropped for the snapshot; the total pin cap
# keeps pinning — which is exempt from the char budget — from blowing it.
PIN_TERM_MAX_MATCHES = int(os.getenv("PIN_TERM_MAX_MATCHES", "15"))
PIN_TOTAL_CAP = int(os.getenv("PIN_TOTAL_CAP", "30"))


def pinned_indices(dom_tree, terms) -> set:
    """Guarded find-in-page: indices to pin for `terms`, dropping any term too
    common to be discriminative and capping the total (document order)."""
    out = set()
    for t in terms or []:
        matches = find_matching_indices(dom_tree, [t])
        if not matches or len(matches) > PIN_TERM_MAX_MATCHES:
            continue
        out |= matches
    return set(sorted(out)[:PIN_TOTAL_CAP])


def find_matching_indices(dom_tree, terms) -> set:
    """Indices of nodes whose text/label/selector contain any of `terms`.

    Case-insensitive substring match — this is the "Ctrl-F for the agent"
    primitive: given what the agent is hunting for, locate it anywhere in the
    full in-memory tree regardless of how its group was capped. Empty terms (and
    empty/whitespace-only term strings) match nothing.
    """
    needles = [t.strip().lower() for t in (terms or []) if t and t.strip()]
    if not needles:
        return set()
    matches = set()
    for i, n in enumerate(dom_tree):
        haystack = " ".join(p for p in (n.text, n.label, n.selector) if p).lower()
        if any(needle in haystack for needle in needles):
            matches.add(i)
    return matches


_QUOTED = re.compile(r'"([^"]+)"')


def _search_terms_from_state(state) -> list:
    """What the stuck agent is hunting for, mined from its own recent intent.

    The predicted outcome (expect.text_contains) is the main signal — it names
    what the agent expected to see. Element selectors are opaque per-snapshot ids
    ([data-a11y-id="N"]) that carry no semantic meaning, so we only mine a
    selector when it's a text-anchor (:has-text("…"), e.g. a model-authored
    expectation). We surface these literals so the next serialization can pin the
    matching node even if the group cap or budget had dropped it. Deterministic,
    no extra model round-trip.
    """
    terms: list = []
    for action in state.actions_taken[-2:]:
        # Only a text-anchor selector (:has-text("…")) names its target
        # semantically; the opaque stable-id selectors ([data-a11y-id="N"]) carry
        # no signal, so mining their digits would pin unrelated nodes.
        if action.selector and ":has-text(" in action.selector:
            terms.extend(_QUOTED.findall(action.selector))
        if action.expect:
            if action.expect.text_contains:
                terms.append(action.expect.text_contains)
            if action.expect.selector and ":has-text(" in action.expect.selector:
                terms.extend(_QUOTED.findall(action.expect.selector))
    # de-dupe while preserving order
    seen = set()
    out = []
    for t in terms:
        key = t.strip().lower()
        if key and key not in seen:
            seen.add(key)
            out.append(t)
    return out


def serialize_dom(dom_tree, max_nodes: int = None, char_budget: int = None,
                  search_terms=None, boost_signatures=None) -> str:
    """Compact the accessibility tree to the top-k elements under a token cap.

    Three stages:
    1. Cap repeated structures at the first MAX_GROUP_NODES per structural
       group — on Gmail this keeps the top ~10 email rows instead of all 400+.
    2. Rank survivors by importance (inputs > buttons > headings > links >
       rest, document order as tiebreak) and admit them until the node cap or
       the character budget derived from MAX_INPUT_TOKENS runs out.
    3. Emit the chosen nodes in document order so the page still reads
       top-to-bottom.

    max_nodes / char_budget default to the standard caps but can be widened on
    recovery: when the agent gets stuck because the element it needs was
    truncated out, re-serializing with a larger budget gives lower-priority
    content (e.g. repository file links) another chance to make the cut.

    search_terms is targeted find-in-page: every node matching a term is pinned
    into the output regardless of the group cap or budget, so the element the
    agent is hunting for is surfaced wherever it lives in the tree. Terms too
    common to identify anything on this page are dropped (see pinned_indices).

    boost_signatures are node signatures that rank FIRST among the non-pinned
    nodes (above inputs) — used for nodes the last action just revealed, e.g.
    a hover-menu's items — but still count against the normal budgets, so a
    huge reveal degrades gracefully instead of blowing the token cap.
    """
    max_nodes = max_nodes or MAX_DOM_NODES
    char_budget = char_budget or _DOM_CHAR_BUDGET

    # Pinned matches jump the queue: they're admitted first and exempt from the
    # group cap and char budget, so a searched-for element can't be crowded out.
    pinned = pinned_indices(dom_tree, search_terms)
    chosen: dict[int, str] = {}
    chars = 0
    for i in pinned:
        entry = _compact_node(dom_tree[i])
        chosen[i] = entry
        chars += len(entry) + 1

    group_counts: dict[tuple, int] = {}
    survivors = []
    for i, n in enumerate(dom_tree):
        if i in chosen:
            continue  # already pinned
        key = _group_key(n)
        seen = group_counts.get(key, 0)
        group_counts[key] = seen + 1
        if seen < MAX_GROUP_NODES:
            survivors.append((i, n))

    boost = boost_signatures or set()
    ranked = sorted(survivors, key=lambda t: (
        -1 if node_signature(t[1]) in boost else _node_priority(t[1]), t[0]))
    for i, n in ranked:
        if len(chosen) >= max_nodes:
            break
        entry = _compact_node(n)
        if chars + len(entry) + 1 > char_budget:
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


def recovery_budget(attempt: int) -> tuple[int, int]:
    """Escalating (max_nodes, char_budget) for re-perception while stuck.

    Each recovery attempt widens the window (2x on the first, 3x on the
    second, ...) so a target that was truncated out of the default view gets
    another, larger chance to appear. Kept modest so a recovery call can't blow
    past the per-minute rate limit — recovery is rare (capped at 2 attempts).
    """
    factor = 1 + max(attempt, 1)
    return MAX_DOM_NODES * factor, _DOM_CHAR_BUDGET * factor


def _log_serialized_dom(label: str, dom_tree, dom_str: str) -> None:
    """Log how much of the accessibility tree the model received for this call.

    The full serialized tree is the single most useful artifact when the agent
    loops or hallucinates a control, but dumping it every call is very noisy, so
    we log only the node counts here. Set DEBUG_DOM=1 in the environment to
    restore the full tree dump for after-the-fact diagnosis.
    """
    serialized_count = dom_str.count('{"tag"')
    logger.info("[DOM] %s — %d source nodes, %d serialized",
                label, len(dom_tree), serialized_count)
    if os.getenv("DEBUG_DOM"):
        logger.info("[DOM] %s — full tree:\n%s", label, dom_str)


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


class _UsageTracker:
    """Accumulates real token usage across a task so we can report the total
    when it ends. Reset at the start of each task."""

    def __init__(self):
        self.reset()

    def reset(self) -> None:
        self.input_tokens = 0
        self.output_tokens = 0
        self.calls = 0

    def record(self, usage) -> None:
        self.input_tokens += usage.input_tokens
        self.output_tokens += usage.output_tokens
        self.calls += 1

    def summary(self) -> str:
        total = self.input_tokens + self.output_tokens
        return (f"{self.calls} API calls — "
                f"in={self.input_tokens} out={self.output_tokens} "
                f"total={total} tokens")


_usage = _UsageTracker()


def reset_usage() -> None:
    """Start counting token usage fresh (call at the start of a task)."""
    _usage.reset()


def usage_summary() -> str:
    """Human-readable total token usage since the last reset."""
    return _usage.summary()


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
            t0 = time.perf_counter()
            response = await get_client().messages.create(**kwargs)
            inference_s = time.perf_counter() - t0
            usage = response.usage
            _usage.record(usage)
            # cache_read>0 confirms the system+tools prefix was served from cache
            # (a fraction of the cost/latency of reprocessing it). cache_write>0
            # is the first call of a task paying the ~1.25x write premium once.
            cache_read = getattr(usage, "cache_read_input_tokens", 0) or 0
            cache_write = getattr(usage, "cache_creation_input_tokens", 0) or 0
            logger.info(
                "  [Claude] %s — done  in=%d out=%d cache_read=%d cache_write=%d  [TIMING] inference=%.2fs",
                label, usage.input_tokens, usage.output_tokens,
                cache_read, cache_write, inference_s,
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
                         "hover", "navigate", "back", "forward", "reload", "new_tab",
                         "done", "failed", "answer"],
            },
            "selector": {"type": "string"},
            "value": {"type": "string"},
            "description": {"type": "string"},
            "reasoning": {"type": "string"},
            "expect": {
                "type": "object",
                "description": (
                    "OPTIONAL. The observable outcome you predict this action will "
                    "produce, so the browser waits for that exact result instead of "
                    "guessing when the page settled. Set only the fields you're "
                    "confident about; all set fields must come true. Prefer url_contains "
                    "for navigations and text_contains for results — they're robust. "
                    "Use selector only for an element you're sure will appear. Omit "
                    "entirely for actions with no clear visible outcome (scroll, highlight)."
                ),
                "properties": {
                    "url_contains": {"type": "string", "description": "a substring the URL should contain afterward (e.g. 'pulls')"},
                    "selector": {"type": "string", "description": "an element that should appear (same selector syntax as the action)"},
                    "text_contains": {"type": "string", "description": "text that should appear on the page afterward"},
                },
            },
            "updated_checklist": {
                "type": "string",
                "description": (
                    "The ENTIRE checklist with any newly completed step flipped from "
                    "'[ ]' to '[x]'. Include this ONLY when the current page state "
                    "confirms a step just finished — omit it otherwise. Keep the exact "
                    "'[ ] text' / '[x] text' one-item-per-line format. Do not add, "
                    "remove, or reword items here. EXCEPTION: on the first turn, when "
                    "no checklist exists yet, return the NEW checklist you created here."
                ),
            },
            "search_hints": {
                "type": "array",
                "items": {"type": "string"},
                "description": (
                    "FIRST TURN only (or when returning a revised plan after being "
                    "stuck): 5-8 short words or phrases likely to label THIS site's "
                    "buttons, links, or menus for this task — the site's own "
                    "vocabulary, not the user's. User says 'what I bought' -> hints "
                    "like 'orders', 'order history', 'purchases', 'returns'. Page "
                    "elements matching a hint are guaranteed to stay visible to you "
                    "on crowded pages. Omit on later turns."
                ),
            },
        },
        "required": ["type", "description"]
    }
}

SYSTEM_PROMPT = """You are an accessibility agent helping a non-technical user
navigate the web. You'll receive the page's accessibility tree, the user's task,
and a CHECKLIST of steps for the overall task. Call the execute_action tool with
a single next action.

Interpreting the task:
- Users are often non-technical and phrase tasks as questions. These are
  navigation requests, not questions to answer: "where is X?", "how do I get
  to X?", "can you show me X?" all mean find X on this site, navigate to it,
  and SHOW it — scroll it into view and "highlight" it as your final action
  before "done". "how do I do X?" means DO X for them, narrating each step.
- Never finish by only explaining where something is. The task is complete
  when the user is LOOKING at what they asked about.
- If the request is vague, choose the most likely meaning on this site and
  proceed — there is no way to ask a clarifying question.
- The user's words are their INTENT, not the site's vocabulary. They often
  use the wrong name for a thing: "returns" when the site says "Your Orders",
  "movies" when it says "Prime Video", "my page" when it says "Profile".
  Match what they MEAN against the labels actually present in the tree, and
  prefer the site's own wording when typing into a search box. If searching
  their literal word found nothing, search once more with the site's own term
  or a broader one — never fail just because their exact word isn't on the page.
- EXCEPTION — general questions: if the request is clearly a general-knowledge
  question that visiting or navigating a website would NOT satisfy (e.g.
  "what year did the moon landing happen?", "how many cups are in a quart?"),
  do not navigate. Respond with a single "answer" action: put the answer in
  "value". Keep it to ONE plain sentence, never verbose — it is read aloud in
  full, so write it the way you'd say it out loud to an older person. Do not
  create a checklist.
  This exception NEVER applies to anything findable or doable on a website:
  "where is X", "show me X", "how do I do X" stay navigation tasks.

Your "description" is READ ALOUD to an older, non-technical person:
- ONE short, friendly sentence in plain everyday words — the way a patient
  helper would speak. Present tense: "I'm opening the search box now."
- No technical terms. Never say URL, selector, DOM, element, or tab — say
  "the page", "the address", "the search box", "the list".
- Say what you're doing and, when it helps, what will happen next
  ("I'm pressing Enter — the results will come up in a moment.").
- The final "done"/"failed" description is read aloud in full: it must be
  exactly ONE short, calm sentence — never a recap of everything you did.

Checklist rules:
- '[ ]' = pending step, '[x]' = completed step. Work through pending steps in order.
- On the FIRST turn there is no checklist yet: create a short one (2-6 concrete
  single-action steps, one per line, each starting '[ ] ') covering the FULL
  task — e.g. a search includes opening the search input, typing the query, AND
  submitting it. A "where is X" / "how do I find X" task ends with a step to
  highlight X so the user can see it. Return the checklist in updated_checklist
  and take its first action now.
- Multi-step tasks require MULTIPLE actions across turns. Opening a search bar is
  NOT searching; typing a query is NOT submitting it. Do each step.
- When the current page state confirms a step finished, return the whole checklist
  in updated_checklist with that step flipped to '[x]'. Never flip a box based on
  an action you only INTEND to take — only based on what the page shows.
- After the first turn, do NOT restructure the checklist (no adding/removing/
  rewording items) — only flip boxes.

Search hints (first turn, in search_hints):
- With your first action, also return 5-8 short words or phrases likely to
  label the controls this task needs on THIS site — the site's own vocabulary,
  not the user's ("what did I buy" -> "orders", "order history", "purchases",
  "returns"). Include likely synonyms. Elements matching a hint stay visible
  to you even on crowded pages, so good hints keep your target in view.

Your capabilities (everything a regular user can do):
- Page actions (need a selector from the accessibility tree): "click", "type"
  (text in "value"), "press_enter" (press Enter in the input you just typed
  in — the normal way to submit a search; ALWAYS include that input's
  selector, do not rely on focus), "hover" (open a hover-menu so its items
  appear in the next tree), "scroll", "highlight", "wait".
- Browser actions (no selector — the browser executes them for you):
  "navigate" (full URL in "value"), "back", "forward", "reload",
  "new_tab" (full URL in "value"; the task continues in the new tab).

Dropdowns and menus:
- A <select> element lists its choices in "options". NEVER click a select —
  its dropdown is browser UI you cannot see or operate. Instead use "type"
  with the chosen option's exact text from "options" as the "value".
- Elements marked "hidden":true sit inside a CLOSED dropdown/hover menu.
  You can "click" them DIRECTLY — the click works even though the item is not
  on screen. Prefer that over opening the menu first; it is one step instead
  of two.
- If a nav item looks like it hides a menu but no matching "hidden" items are
  in the tree, "hover" it: the menu opens and its items appear in the next
  tree. Then click the revealed item. Never click a hover-trigger expecting
  to navigate — hover it instead.
- Custom dropdown menus (comboboxes): clicking them reveals their
  items in the NEXT accessibility tree. Click to open, then on the next turn
  click the revealed item. If a click changed the page but not how you
  predicted, check the new tree for revealed menu items before retrying.

Predicting outcomes (optional but encouraged):
- When an action has a clear visible result, set "expect" to that result so the
  browser waits for it specifically rather than guessing the page settled:
  a click that navigates -> expect.url_contains (e.g. "pulls"); a search that
  loads results -> expect.text_contains; an action that reveals an element ->
  expect.selector. Set only fields you're confident about. Omit "expect" for
  actions with no clear observable outcome (scroll, highlight, wait). A wrong
  prediction makes the action look failed, so predict conservatively.

Operating boundary:
- For page actions you can ONLY use selectors from the provided accessibility
  tree. Browser UI (address bar, bookmarks, menus) does NOT exist for you —
  the browser actions above replace it. Don't invent selectors for it.
- The accessibility tree is TRUNCATED to the most important elements; long
  lists (emails, results, products) show only their first items. If something
  you need isn't listed, prefer the page's own search/filter inputs to narrow
  to it. Scrolling rarely helps here: the tree already includes off-screen
  elements, so a control that isn't listed is usually genuinely absent, not
  just below the fold. Do NOT scroll more than once chasing the same missing
  control — if one scroll doesn't surface it, the tree confirms it isn't here.
  Instead navigate straight to it by URL when you know it (e.g. a GitHub "New
  issue" button lives at <repo-url>/issues/new), or return "failed" if you
  truly can't reach it. Never emit a string of scrolls hoping a control appears.
- If you are told your previous action FAILED to execute, it did NOT happen,
  no matter what your action history says. Never flip its checklist box;
  pick a different element or approach (e.g. after a failed click, try
  "navigate" directly, or "back" to return to a working page).

Use "done" ONLY when every checklist item is '[x]' and the user's full task is
complete. Use "failed" only if the task is impossible from the current state.
Be patient, explain each step clearly, prefer the safest action."""

RECOVERY_PROMPT = """You are an accessibility agent that got STUCK.

Your last few actions did not produce expected results. Stop and reconsider.
Pick a DIFFERENT approach than what you've been trying. Do NOT repeat the same
action again — that's what got you stuck.

Common reasons for getting stuck and what to try instead:
- Clicking a nav item that opens a dropdown (DOM changes, URL doesn't): the
  sub-menu items are now visible in the tree — click one, OR pivot to a
  different nav section that better matches the task.
- The thing you need is in a hover-menu: items marked "hidden":true can be
  clicked directly without opening their menu. If they're not in the tree,
  "hover" the menu trigger — its items appear in the next tree.
- You searched or hunted for the USER'S literal word but this site names it
  differently ("returns" vs "Your Orders"). Re-read the tree for the site's
  own label that matches their intent and use that instead.
- Waiting after a dropdown opened: the page is already settled. Read the
  visible sub-items and act on them or pivot.
- "Testing" navigation on DMV-style sites is for knowledge/written tests, NOT
  for scheduling a driving test appointment — look under "Appointments" instead.

You will see the current checklist ('[ ]' pending / '[x]' done). If the plan
itself is wrong — a step is impossible on this site, or a different path is
needed — return a REVISED checklist in updated_checklist: keep completed '[x]'
items as-is, and replace/add/reorder the pending steps to reflect the new
approach. If the plan is fine and only the action needs to change, omit
updated_checklist. When you DO revise the plan, also return fresh
search_hints — 5-8 words or phrases the elements you now need would use on
this site (your earlier guesses were probably wrong).

Your "description" is READ ALOUD to an older, non-technical person: ONE short,
friendly sentence in plain everyday words, present tense, no technical terms
(never say URL, selector, DOM, element, or tab). Stay calm and reassuring —
never mention being stuck; just say what you're doing next.

Call the execute_action tool with your new action."""


def _cached_system(text: str) -> list:
    """Wrap a static system prompt in a cache_control block.

    The system prompt and tool schemas are byte-identical across every call in a
    task (and across concurrent tasks), so caching this prefix turns each step
    after the first into a cache read (~0.1x input cost, faster time-to-first-
    token) instead of reprocessing the same ~1K tokens of instructions + tool
    schema every step. Tools render before system, so the breakpoint on the
    system block caches the tool schema too. Volatile per-step content (task,
    checklist, DOM, action history) stays in the user message — after the cached
    prefix — so it never invalidates the cache.

    The hot path (decide) benefits most: it fires many times per task in quick
    succession, well within the 5-minute cache TTL. Plan (once/task) and recovery
    (rare) benefit less but the wrapper is harmless when a prefix is below the
    model's minimum cacheable size — it just won't cache (cache_write=0).
    """
    return [{"type": "text", "text": text, "cache_control": {"type": "ephemeral"}}]


def _profile_block(profile: Optional[UserProfile]) -> str:
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


def _parse_action(response, label: str) -> AgentAction:
    """Turn Claude's tool_use block into an AgentAction, tolerating the
    occasional malformed output the model produces. One bad generation must
    never tear down the task.

    In particular `expect` is optional and the model sometimes emits it as a
    bare string — its tool-call XML syntax (e.g. '<parameter name=...>orders')
    bleeding into the JSON value — which pydantic rejects. A wrong expectation
    is never worth crashing over, so drop anything that isn't an object. Any
    other unparseable output degrades to a graceful 'failed' the user hears,
    instead of an unhandled 500 that closes the socket mid-task.
    """
    try:
        tool_use = next(b for b in response.content if b.type == "tool_use")
        raw = dict(tool_use.input)
        if not isinstance(raw.get("expect"), dict):
            raw.pop("expect", None)  # keep only a well-formed expectation object
        return AgentAction(**raw)
    except (ValidationError, StopIteration, TypeError, AttributeError) as e:
        logger.warning("[%s] unusable model output — ending gracefully: %s", label, e)
        return AgentAction(
            type=ActionType.FAILED,
            description="I'm having a little trouble with this page. Let's try that again.",
        )


async def stream_action(state: AgentState, rejection_note: str = None) -> AgentAction:
    # smart search: the model's site-vocabulary hints plus literal task words
    # pin matching elements into the snapshot; nodes the last action just
    # revealed (an opened menu) rank first
    terms = list(state.search_hints) + mine_task_terms(state.task, state.checklist)
    dom_str = serialize_dom(state.context.dom_tree, search_terms=terms,
                            boost_signatures=state.revealed_signatures)
    _log_serialized_dom(f"decide (terms={terms or '-'}, "
                        f"revealed={len(state.revealed_signatures)})",
                        state.context.dom_tree, dom_str)
    # first turn: planning is merged into this call — one LLM round trip
    # returns both the checklist (via updated_checklist) and the first action
    first_turn = not state.checklist.strip()
    checklist_block = (
        "(none yet — FIRST TURN: create it in updated_checklist and take its first action)"
        if first_turn else state.checklist
    )
    user_message = f"""Task: {state.task}

Checklist (work through pending '[ ]' items in order):
{checklist_block}

Current URL: {state.context.url}
Page title: {state.context.title}

Accessibility tree:
{dom_str}

Previous actions: {json.dumps([a.model_dump(exclude={'updated_checklist', 'search_hints'}, exclude_none=True) for a in state.actions_taken[-5:]], separators=(",", ":"))}

What is the next action?"""
    user_message += _profile_block(state.profile)

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
        system=_cached_system(SYSTEM_PROMPT),
        tools=[ACTION_TOOL],
        tool_choice={"type": "tool", "name": "execute_action"},
        messages=[{"role": "user", "content": user_message}]
    )

    return _parse_action(response, "decide")


async def stream_recovery_action(state: AgentState) -> AgentAction:
    # widen the serialization budget on each recovery attempt so an element
    # that was truncated out of the default view gets another chance to appear
    max_nodes, char_budget = recovery_budget(state.recovery_attempts)
    # find-in-page: pin whatever the agent was hunting for — its site-vocabulary
    # hints, literal task words, and anything its recent actions named (e.g. the
    # "backend" link a failed click named in its selector) — so a target the
    # group cap or budget had dropped is surfaced wherever it lives in the tree.
    search_terms = (list(state.search_hints)
                    + mine_task_terms(state.task, state.checklist)
                    + _search_terms_from_state(state))
    dom_str = serialize_dom(state.context.dom_tree, max_nodes, char_budget,
                            search_terms=search_terms)
    _log_serialized_dom(f"recover (attempt {state.recovery_attempts}, "
                        f"budget {max_nodes} nodes/{char_budget} chars, "
                        f"pinned terms={search_terms or '-'})",
                        state.context.dom_tree, dom_str)
    user_message = f"""Task: {state.task}

Checklist ('[ ]' pending / '[x]' done — revise it via updated_checklist if the plan itself is wrong):
{state.checklist or '(no checklist)'}

Current URL: {state.context.url}
Page title: {state.context.title}

YOU ARE STUCK. Here's what you've tried (last 6 actions):
{json.dumps([a.model_dump(exclude={'updated_checklist', 'search_hints'}, exclude_none=True) for a in state.actions_taken[-6:]], separators=(",", ":"))}

{f"Your most recent action FAILED to execute: {state.last_action_result}. It never reached the page." if state.last_action_result else ""}

Current accessibility tree:
{dom_str}

Reconsider and choose a DIFFERENT next action."""
    user_message += _profile_block(state.profile)

    response = await _call_claude(
        "recover",
        model="claude-sonnet-4-5",
        max_tokens=1024,
        system=_cached_system(RECOVERY_PROMPT),
        tools=[ACTION_TOOL],
        tool_choice={"type": "tool", "name": "execute_action"},
        messages=[{"role": "user", "content": user_message}]
    )

    return _parse_action(response, "recover")