# Tests for execution-feedback verification: the extension reports whether an
# action actually executed, and verify() must react to failures immediately.
import asyncio

import pytest

from agent.nodes import verify, hash_dom
from agent.schemas import AgentState, AgentAction, ActionType, PageContext, DOMNode


def make_state(**kwargs):
    return AgentState(
        task="search up cats",
        context=PageContext(
            url="https://example.com",
            title="Test",
            dom_tree=[DOMNode(tag="input", label="Search", selector="#search")],
        ),
        **kwargs,
    )


def click(desc="click search"):
    return AgentAction(type=ActionType.CLICK, selector="#search", description=desc)


# --- execution failure is an immediate stuck signal ---

def test_failed_execution_counts_stuck_even_on_first_action():
    # before: verify() early-returns with < 2 actions, so a first-action
    # failure was invisible
    state = make_state(last_action_result="element not found for selector: #fake")
    state.actions_taken = [click()]
    state = asyncio.run(verify(state))
    assert state.stuck_count == 1


def test_two_consecutive_failures_trigger_stuck_status():
    state = make_state(last_action_result="element not found for selector: #fake")
    state.actions_taken = [click()]
    state.stuck_count = 1  # one failure already recorded
    state = asyncio.run(verify(state))
    assert state.status == "stuck"


def test_successful_execution_does_not_count_stuck():
    # DOM changed, action executed -> healthy step, counter resets
    state = make_state(last_action_result=None)
    state.actions_taken = [
        click(),
        AgentAction(type=ActionType.TYPE, selector="#search", value="cats", description="type"),
    ]
    state.stuck_count = 1
    state.previous_url = "https://example.com"
    state.previous_dom_hash = "something-else"  # page changed
    state = asyncio.run(verify(state))
    assert state.stuck_count == 0


# --- browser-level actions ---

def test_new_browser_action_types_exist():
    # back/forward/reload/new_tab run in the background worker; press_enter
    # submits like a real user
    for t in ("back", "forward", "reload", "new_tab", "press_enter"):
        action = AgentAction(type=t, description=f"do {t}")
        assert action.type == ActionType(t)


def test_browser_action_with_unchanged_page_counts_stuck():
    # "back" that didn't change the page is as suspicious as a dead click
    state = make_state(last_action_result=None)
    state.actions_taken = [
        click(),
        AgentAction(type=ActionType.BACK, description="go back"),
    ]
    state.previous_url = state.context.url
    state.previous_dom_hash = hash_dom(state.context.dom_tree)  # unchanged
    state = asyncio.run(verify(state))
    assert state.stuck_count == 1


# --- hover: a page action that reveals dropdown menu items ---

def test_hover_action_type_exists():
    action = AgentAction(type="hover", selector="#menu", description="hover the menu")
    assert action.type == ActionType.HOVER


def test_hover_with_unchanged_page_counts_stuck():
    # a hover that revealed nothing is as dead as a click that did nothing
    state = make_state(last_action_result=None)
    state.actions_taken = [
        click(),
        AgentAction(type=ActionType.HOVER, selector="#menu", description="hover"),
    ]
    state.previous_url = state.context.url
    state.previous_dom_hash = hash_dom(state.context.dom_tree)  # unchanged
    state = asyncio.run(verify(state))
    assert state.stuck_count == 1


# --- DOM hash must see input values, so typing registers as a page change ---

def test_hash_dom_differs_when_only_input_value_changes():
    before = [DOMNode(tag="input", selector="#search", value="")]
    after = [DOMNode(tag="input", selector="#search", value="cats")]
    assert hash_dom(before) != hash_dom(after)
