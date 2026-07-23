# Tests for the chat-answer path: a terminal `answer` action for questions
# unrelated to web navigation, and the spoken-narration prompt guidance.
import asyncio

from agent.nodes import decide_action
from agent.schemas import (
    ActionType, AgentAction, AgentState, DOMNode, PageContext,
)
from clients.claude import ACTION_TOOL, SYSTEM_PROMPT, RECOVERY_PROMPT


def make_state(**kwargs):
    return AgentState(
        task="what year did the moon landing happen?",
        context=PageContext(
            url="https://example.com",
            title="Test",
            dom_tree=[DOMNode(tag="input", label="Search", selector="#search")],
        ),
        **kwargs,
    )


def _stub_answer(monkeypatch):
    async def fake_stream_action(state, rejection_note=None):
        return AgentAction(
            type=ActionType.ANSWER,
            value="It happened in 1969.",
            description="Here's your answer.",
        )
    monkeypatch.setattr("agent.nodes.stream_action", fake_stream_action)


def test_answer_action_is_terminal(monkeypatch):
    _stub_answer(monkeypatch)
    state = asyncio.run(decide_action(make_state()))
    assert state.status == "done"
    assert state.actions_taken[-1].type == ActionType.ANSWER
    assert state.actions_taken[-1].value == "It happened in 1969."


def test_answer_bypasses_checklist_done_gate(monkeypatch):
    # the done-gate re-prompts on premature "done"; an answer must NOT be
    # gated even when checklist items are still unchecked
    calls = {"n": 0}

    async def fake_stream_action(state, rejection_note=None):
        calls["n"] += 1
        return AgentAction(
            type=ActionType.ANSWER,
            value="It happened in 1969.",
            description="Here's your answer.",
        )
    monkeypatch.setattr("agent.nodes.stream_action", fake_stream_action)
    state = asyncio.run(decide_action(make_state(checklist="[ ] find the page")))
    assert state.status == "done"
    assert calls["n"] == 1  # no done-gate retry happened


def test_action_tool_enum_includes_answer():
    assert "answer" in ACTION_TOOL["input_schema"]["properties"]["type"]["enum"]


def test_system_prompt_has_answer_rule():
    # the rule must exist, be scoped away from navigation, and demand brevity
    assert '"answer"' in SYSTEM_PROMPT
    assert "verbose" in SYSTEM_PROMPT


def test_prompts_have_spoken_language_guidance():
    # descriptions are read ALOUD to an older listener — both prompts must say so
    for prompt in (SYSTEM_PROMPT, RECOVERY_PROMPT):
        assert "aloud" in prompt.lower()
