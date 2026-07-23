# A malformed tool call from the model must never crash the task. The model
# occasionally emits `expect` as a bare string (its XML tool-call syntax
# bleeding into the JSON value) — seen in production on Amazon — which used to
# raise a pydantic ValidationError straight up through the WebSocket handler.
from types import SimpleNamespace

from clients.claude import _parse_action
from agent.schemas import ActionType


def _resp(tool_input):
    block = SimpleNamespace(type="tool_use", input=tool_input)
    return SimpleNamespace(content=[block])


def test_malformed_expect_string_is_dropped_not_crashed():
    # the exact production failure
    resp = _resp({
        "type": "click", "selector": "#x", "description": "opening the menu",
        "expect": '\n<parameter name="text_contains">orders',
    })
    action = _parse_action(resp, "decide")
    assert action.type == ActionType.CLICK
    assert action.expect is None  # the bad field was dropped, action survives


def test_valid_expect_dict_is_kept():
    resp = _resp({
        "type": "navigate", "value": "https://x.com", "description": "going there",
        "expect": {"url_contains": "x.com"},
    })
    action = _parse_action(resp, "decide")
    assert action.expect is not None
    assert action.expect.url_contains == "x.com"


def test_unparseable_output_returns_graceful_failed():
    # missing the required 'type' — unbuildable; must degrade, not raise
    resp = _resp({"description": "no type field here"})
    action = _parse_action(resp, "decide")
    assert action.type == ActionType.FAILED
    assert action.description  # carries a spoken, user-facing message


def test_no_tool_use_block_returns_graceful_failed():
    resp = SimpleNamespace(content=[SimpleNamespace(type="text", text="hi")])
    action = _parse_action(resp, "decide")
    assert action.type == ActionType.FAILED


def test_hover_is_offered_in_the_tool_schema():
    # the model can only emit actions the tool schema advertises
    from clients.claude import ACTION_TOOL
    assert "hover" in ACTION_TOOL["input_schema"]["properties"]["type"]["enum"]
