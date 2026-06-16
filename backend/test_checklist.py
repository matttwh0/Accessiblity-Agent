# Tests for the checklist plan + server-side done-gate.
# Mocks the Claude client functions — no live API calls.
import asyncio
from unittest.mock import AsyncMock, patch

import pytest

import agent.nodes as nodes
from agent.nodes import plan_task, decide_action, recover, has_unchecked
from agent.schemas import AgentState, AgentAction, ActionType, PageContext, DOMNode


def make_state(checklist=""):
    return AgentState(
        task="search up cats on this website",
        checklist=checklist,
        context=PageContext(
            url="https://example.com",
            title="Test",
            dom_tree=[DOMNode(tag="input", label="Search", selector="#search")],
        ),
    )


# --- has_unchecked: defensive parsing of the checklist string ---

def test_has_unchecked_true_when_items_pending():
    assert has_unchecked("[x] open search bar\n[ ] type query\n[ ] submit")


def test_has_unchecked_false_when_all_done():
    assert not has_unchecked("[x] open search bar\n[x] type query\n[x] submit")


def test_has_unchecked_empty_checklist_is_not_unchecked():
    # no plan -> no gate; don't block done forever
    assert not has_unchecked("")


def test_has_unchecked_malformed_line_fails_safe():
    # a line that isn't clearly [x] counts as not-done
    assert has_unchecked("[x] open search bar\nfinish the search somehow")


def test_has_unchecked_tolerates_dash_prefix():
    assert has_unchecked("- [x] open search bar\n- [ ] type query")


# --- plan_task: initial decomposition ---

def test_plan_task_sets_checklist():
    state = make_state()
    plan = "[ ] click search icon\n[ ] type query\n[ ] press enter"
    with patch.object(nodes, "stream_plan", AsyncMock(return_value=plan)):
        state = asyncio.run(plan_task(state))
    assert state.checklist == plan


def test_plan_task_falls_back_to_raw_task_on_failure():
    state = make_state()
    with patch.object(nodes, "stream_plan", AsyncMock(side_effect=RuntimeError("api down"))):
        state = asyncio.run(plan_task(state))
    assert state.checklist == "[ ] search up cats on this website"


# --- decide_action: check-off flips travel with the action ---

def test_decide_action_applies_updated_checklist():
    state = make_state(checklist="[ ] click search icon\n[ ] type query")
    action = AgentAction(
        type=ActionType.CLICK,
        selector="#search",
        description="Click the search icon",
        updated_checklist="[x] click search icon\n[ ] type query",
    )
    with patch.object(nodes, "stream_action", AsyncMock(return_value=action)):
        state = asyncio.run(decide_action(state))
    assert state.checklist == "[x] click search icon\n[ ] type query"


def test_decide_action_keeps_checklist_when_not_updated():
    state = make_state(checklist="[ ] click search icon")
    action = AgentAction(type=ActionType.CLICK, selector="#search", description="click")
    with patch.object(nodes, "stream_action", AsyncMock(return_value=action)):
        state = asyncio.run(decide_action(state))
    assert state.checklist == "[ ] click search icon"


# --- done-gate: reject premature done, accept legitimate done ---

def test_done_rejected_when_items_unchecked_then_retries():
    state = make_state(checklist="[x] click search icon\n[ ] type query")
    premature_done = AgentAction(type=ActionType.DONE, description="Task complete")
    real_next = AgentAction(
        type=ActionType.TYPE, selector="#search", value="cats", description="Type the query"
    )
    mock = AsyncMock(side_effect=[premature_done, real_next])
    with patch.object(nodes, "stream_action", mock):
        state = asyncio.run(decide_action(state))
    last = state.actions_taken[-1]
    assert last.type == ActionType.TYPE
    assert state.status != "done"
    # the rejected done must NOT appear in history
    assert all(a.type != ActionType.DONE for a in state.actions_taken)


def test_done_accepted_when_all_items_checked():
    state = make_state(checklist="[x] click search icon\n[x] type query")
    done = AgentAction(type=ActionType.DONE, description="Task complete")
    with patch.object(nodes, "stream_action", AsyncMock(return_value=done)):
        state = asyncio.run(decide_action(state))
    assert state.status == "done"


def test_done_accepted_after_retry_budget_exhausted():
    # if Claude insists on done 3x in a row, accept it (avoid infinite loop)
    state = make_state(checklist="[x] click search icon\n[ ] type query")
    done = AgentAction(type=ActionType.DONE, description="Task complete")
    mock = AsyncMock(return_value=done)
    with patch.object(nodes, "stream_action", mock):
        state = asyncio.run(decide_action(state))
    assert state.status == "done"
    assert mock.call_count == 3  # initial + 2 retries


def test_done_accepted_when_retry_checks_off_remaining():
    # retry returns done again but with everything checked -> accept immediately
    state = make_state(checklist="[x] click\n[ ] type")
    done_incomplete = AgentAction(type=ActionType.DONE, description="done")
    done_complete = AgentAction(
        type=ActionType.DONE, description="done",
        updated_checklist="[x] click\n[x] type",
    )
    mock = AsyncMock(side_effect=[done_incomplete, done_complete])
    with patch.object(nodes, "stream_action", mock):
        state = asyncio.run(decide_action(state))
    assert state.status == "done"
    assert mock.call_count == 2


# --- recover: failure path may rewrite the whole plan ---

def test_recover_applies_revised_checklist():
    state = make_state(checklist="[x] click search icon\n[ ] type query")
    state.stuck_count = 2
    revised = "[x] click search icon\n[ ] open advanced search page\n[ ] type query there"
    action = AgentAction(
        type=ActionType.NAVIGATE,
        value="https://example.com/advanced",
        description="Try advanced search instead",
        updated_checklist=revised,
    )
    with patch.object(nodes, "stream_recovery_action", AsyncMock(return_value=action)):
        state = asyncio.run(recover(state))
    assert state.checklist == revised
    assert state.status == "executing"
