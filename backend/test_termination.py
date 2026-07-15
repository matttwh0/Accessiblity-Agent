# backend/test_termination.py
# Regression test: when the agent is stuck and recovery is exhausted, the graph
# must reach a terminal state ("failed") instead of looping forever between
# recover and verify. Uses a patched LLM so no API calls are made.
import asyncio
import agent.nodes as nodes
from agent.schemas import AgentState, PageContext, DOMNode, AgentAction, ActionType
from langgraph.errors import GraphRecursionError


async def stuck_action(state):
    # Always returns the same non-terminal action -> guarantees "stuck".
    return AgentAction(
        type=ActionType.CLICK,
        selector="a:nth-of-type(1)",
        description="canned click",
    )


def make_state():
    return AgentState(
        task="find the company number",
        context=PageContext(
            url="https://example.com",
            title="Test",
            dom_tree=[DOMNode(tag="a", text="Home", selector="a:nth-of-type(1)")],
        ),
    )


async def done_action(state):
    # checks off the only checklist item so the done-gate accepts immediately
    return AgentAction(
        type=ActionType.DONE,
        description="task complete",
        updated_checklist="[x] find the company number",
    )


async def main():
    from agent.graph import build_graph

    # Case 1: permanently stuck agent must give up with status="failed".
    # (planning is merged into the first decide; stuck_action returns no
    # checklist, so decide_action seeds the single-item fallback)
    nodes.stream_action = stuck_action
    nodes.stream_recovery_action = stuck_action
    try:
        result = await build_graph().ainvoke(
            make_state(), config={"recursion_limit": 100}
        )
    except GraphRecursionError:
        raise AssertionError(
            "FAIL: graph never terminated — infinite recover<->verify loop"
        )
    status = result.get("status")
    assert status == "failed", f"FAIL: expected 'failed', got {status!r}"
    print(f"PASS [stuck]: terminated with status={status!r}, steps={result.get('steps')}")

    # Case 2: happy path — a "done" action must terminate immediately.
    nodes.stream_action = done_action
    result = await build_graph().ainvoke(make_state(), config={"recursion_limit": 100})
    status = result.get("status")
    assert status == "done", f"FAIL: expected 'done', got {status!r}"
    print(f"PASS [done]:  terminated with status={status!r}, steps={result.get('steps')}")


asyncio.run(main())
