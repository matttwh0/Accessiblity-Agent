import hashlib
import json
import logging
import re
import time
from .schemas import AgentState, AgentAction, ActionType
from clients.claude import stream_action, stream_recovery_action

logger = logging.getLogger("agent.nodes")

# a line counts as DONE only if it clearly matches "[x]"; anything else
# (including malformed lines) counts as unchecked, so the done-gate fails safe
_DONE_LINE = re.compile(r"^\s*(?:-\s*)?\[[xX]\]")
# one retry: if the model insists on a premature "done" twice against explicit
# instructions, a third identical prompt rarely changes the answer — each
# retry is a full LLM call, so keep the budget tight
_MAX_DONE_RETRIES = 1

def has_unchecked(checklist: str) -> bool:
    """True if any checklist line is not clearly marked [x]."""
    lines = [l for l in checklist.splitlines() if l.strip()]
    if not lines:
        return False  # no plan -> no gate
    return any(not _DONE_LINE.match(l) for l in lines)

def hash_dom(dom_tree):
    """Quick fingerprint of the page state for change detection."""
    # include value so typing into an input registers as a page change
    serialized = json.dumps(
        [(n.tag, n.text, n.selector, n.value) for n in dom_tree],
        sort_keys=True
    )
    return hashlib.md5(serialized.encode()).hexdigest()

async def perceive(state: AgentState) -> AgentState:
    state.previous_url = state.context.url
    state.previous_dom_hash = hash_dom(state.context.dom_tree)
    logger.info("[PERCEIVE] step=%d  url=%s  dom_nodes=%d",
                state.steps, state.context.url, len(state.context.dom_tree))
    return state

async def decide_action(state: AgentState) -> AgentState:
    logger.info("[DECIDE] step=%d  last_result=%s", state.steps, state.last_action_result or "ok")
    t0 = time.perf_counter()
    first_turn = not state.checklist.strip()
    action = await stream_action(state)
    if action.updated_checklist:
        state.checklist = action.updated_checklist
        if first_turn:
            logger.info("[PLAN] checklist (merged with first action):\n%s", state.checklist)
    elif first_turn:
        # the merged plan didn't come back — seed a single-item checklist so
        # the done-gate still has something to gate on
        state.checklist = f"[ ] {state.task}"
        logger.warning("[PLAN] first turn returned no checklist — using fallback")

    # done-gate: refuse "done" while checklist items remain unchecked.
    # Re-prompt with the remaining items; rejected "done" actions never enter
    # history. After the retry budget, accept to avoid an infinite loop.
    retries = 0
    while (action.type == ActionType.DONE
           and has_unchecked(state.checklist)
           and retries < _MAX_DONE_RETRIES):
        remaining = "\n".join(
            l for l in state.checklist.splitlines()
            if l.strip() and not _DONE_LINE.match(l)
        )
        action = await stream_action(
            state,
            rejection_note=(
                "Your previous 'done' was REJECTED — these checklist items are "
                f"still pending:\n{remaining}\n"
                "Do NOT return 'done'. Take the next action toward the first "
                "pending item (or flip items to [x] via updated_checklist if the "
                "page already shows them complete)."
            ),
        )
        if action.updated_checklist:
            state.checklist = action.updated_checklist
        retries += 1

    logger.info("[TIMING] decide=%.2fs (incl %d done-gate retr%s)",
                time.perf_counter() - t0, retries, "y" if retries == 1 else "ies")
    # `type` is the only action carrying user-typed text — including saved-profile
    # PII — so its value must never be logged. Other actions' values are URLs/keys
    # and stay visible for debugging. (The recover-path log already omits value.)
    _log_value = "***" if action.type == ActionType.TYPE else action.value
    logger.info("[DECIDE] → %s  selector=%s  value=%r  desc=%r",
                action.type, action.selector or "-", _log_value, action.description)
    state.actions_taken.append(action)
    state.steps += 1
    if action.type in (ActionType.DONE, ActionType.FAILED):
        state.status = action.type.value
    elif action.type == ActionType.ANSWER:
        # a chat answer completes the session — there was never a page task
        state.status = "done"
    return state

async def verify(state: AgentState) -> AgentState:
    """Detect three kinds of stuck conditions."""
    # Don't clobber a terminal status. `recover` routes back through `verify`,
    # so if recovery already gave up (status="failed"), re-running stuck
    # detection here would flip it back to "stuck" and loop forever.
    if state.status in ("done", "failed"):
        return state

    is_stuck = False

    # did the page visibly change after the last action? (URL or DOM). Computed
    # once and reused below so a successful-but-mispredicted action isn't
    # punished as if nothing happened.
    current_hash = hash_dom(state.context.dom_tree)
    page_changed = (state.previous_dom_hash != current_hash or
                    state.previous_url != state.context.url)

    # case 0: the extension reported the action never executed (element not
    # found, unsupported action, ...). Definite failure — no heuristics
    # needed, and it must count even on the very first action.
    if state.last_action_result:
        is_stuck = True

    # case 0b: the action ran but its PREDICTED outcome didn't materialize
    # (e.g. claimed it would land on /pulls). Only count this as stuck if the
    # page ALSO didn't change — a click that changed the DOM but missed its
    # prediction (a dropdown opened instead of navigating) made real progress:
    # the revealed items are in the new tree, so the normal decide loop handles
    # it in one step without burning a recovery cycle.
    if state.last_expectation_met is False and not page_changed:
        is_stuck = True

    if len(state.actions_taken) >= 2:
        last = state.actions_taken[-1]
        prev = state.actions_taken[-2]

        # case 1: same action repeated (Claude is looping)
        if (last.type == prev.type and
            last.selector == prev.selector and
            last.value == prev.value):
            is_stuck = True

        # case 2: page didn't change after an action that should change it.
        # SCROLL is included: the accessibility tree already carries off-screen
        # elements, so a scroll that doesn't change the tree (and URL) revealed
        # nothing. A genuinely productive scroll (lazy-loaded content) DOES move
        # the dom hash and so won't trip this — only dead-end scrolls do, which
        # stops the "scroll forever chasing a missing button" loop fast.
        # WAIT is included: if waiting produced no observable change, the page
        # wasn't loading anything — waiting again is pointless.
        if last.type in (ActionType.CLICK, ActionType.TYPE, ActionType.NAVIGATE,
                         ActionType.PRESS_ENTER, ActionType.BACK, ActionType.FORWARD,
                         ActionType.RELOAD, ActionType.NEW_TAB, ActionType.SCROLL,
                         ActionType.WAIT):
            if not page_changed:
                is_stuck = True

        # case 3: cycling between two states (A → B → A → B)
        if len(state.actions_taken) >= 4:
            a, b, c, d = state.actions_taken[-4:]
            if a.selector == c.selector and b.selector == d.selector:
                is_stuck = True

    if is_stuck:
        state.stuck_count += 1
    else:
        state.stuck_count = 0

    if state.stuck_count >= 2:
        state.status = "stuck"

    logger.info("[VERIFY] stuck=%s  stuck_count=%d  status=%s",
                is_stuck, state.stuck_count, state.status)
    return state

async def recover(state: AgentState) -> AgentState:
    """Re-plan with explicit context about what went wrong."""
    state.recovery_attempts += 1
    logger.warning("[RECOVER] attempt=%d/%d", state.recovery_attempts, state.max_recovery_attempts)

    if state.recovery_attempts > state.max_recovery_attempts:
        logger.error("[RECOVER] max attempts reached — marking failed")
        state.status = "failed"
        return state

    t0 = time.perf_counter()
    action = await stream_recovery_action(state)
    logger.info("[TIMING] recover=%.2fs", time.perf_counter() - t0)
    if action.updated_checklist:
        state.checklist = action.updated_checklist
        logger.info("[RECOVER] revised checklist:\n%s", state.checklist)
    state.actions_taken.append(action)
    state.steps += 1
    state.stuck_count = 0
    state.status = "executing"

    logger.info("[RECOVER] → %s  selector=%s  desc=%r",
                action.type, action.selector or "-", action.description)
    if action.type in (ActionType.DONE, ActionType.FAILED):
        state.status = action.type.value

    return state