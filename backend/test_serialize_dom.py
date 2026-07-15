# backend/test_serialize_dom.py
# Regression tests for DOM serialization grouping.
#
# Elements are addressed by an opaque, per-snapshot stable id
# ([data-a11y-id="N"]) stamped by the extension. The selector therefore carries
# NO semantic signal — grouping and targeted search must work off the
# descriptive fields (tag/role/text/label) instead.
#
# Bug still guarded here: on a GitHub repo page the agent looped SCROLL forever
# because the file-listing links (backend, extension, ...) never reached the
# model. They must not be collapsed into one capped group with the nav links.
from agent.schemas import (
    AgentAction, AgentState, ActionType, DOMNode, Expectation, PageContext,
)
from clients.claude import (
    serialize_dom, _group_key, find_matching_indices, _search_terms_from_state,
)


def _link(text, ident=0):
    # mirrors the extension: opaque stable-id selector, identity lives in text
    return DOMNode(tag="a", text=text, selector=f'[data-a11y-id="{ident}"]')


def _row(text, ident=0):
    return DOMNode(tag="tr", text=text, selector=f'[data-a11y-id="{ident}"]')


def test_file_links_survive_named_nav_links():
    nav = [_link(t, i) for i, t in enumerate((
        "Code", "Issues", "Pull requests", "Agents", "Actions",
        "Projects", "Wiki", "Security", "Insights", "Settings",
    ))]
    files = [_link(t, 100 + i) for i, t in enumerate(
        ("LICENSE", "backend", "extension", ".gitignore", "README.md"))]
    out = serialize_dom(nav + files)
    for name in ("backend", "extension", "README.md"):
        assert name in out, f"{name!r} was dropped from the serialized DOM"


def test_distinct_named_links_are_not_one_group():
    # "Code" and "backend" are semantically distinct controls, not structural
    # repeats — identity is in the text, not the opaque id selector.
    assert _group_key(_link("Code", 0)) != _group_key(_link("backend", 1))


def test_positional_repeats_still_collapse():
    # real repeated rows (Gmail-style) still share one group key even though
    # their stable-id selectors differ — grouping keys off tag/role, not the id.
    assert _group_key(_row("row a", 0)) == _group_key(_row("row b", 37))


def test_many_positional_rows_capped():
    rows = [_row(f"row {i}", i) for i in range(1, 401)]
    out = serialize_dom(rows)
    assert "more elements hidden" in out  # the cap still fires on a huge list


# --- targeted search ("Ctrl-F for the agent"): pin nodes matching a query into
# the serialized view, bypassing the group cap and the character budget.

def test_find_matches_text_and_label():
    nodes = [
        _link("backend", 0),
        DOMNode(tag="button", label="Open Backend panel", selector='[data-a11y-id="1"]'),
        _link("frontend", 2),
    ]
    # case-insensitive substring across text / label; "frontend" excluded
    assert find_matching_indices(nodes, ["backend"]) == {0, 1}


def test_find_no_terms_matches_nothing():
    nodes = [_link("backend", 0)]
    assert find_matching_indices(nodes, []) == set()
    assert find_matching_indices(nodes, [""]) == set()


def test_search_pins_node_dropped_by_group_cap():
    # row 250 is far past MAX_GROUP_NODES, so it never survives grouping normally
    rows = [_row(f"row {i}", i) for i in range(1, 401)]
    assert "row 250" not in serialize_dom(rows)  # baseline: dropped
    out = serialize_dom(rows, search_terms=["row 250"])
    assert "row 250" in out  # pinned despite the group cap


def test_search_pins_node_past_char_budget():
    rows = [_row(f"row {i}", i) for i in range(1, 401)]
    # a budget so tiny nothing would normally fit, yet the matched node survives
    out = serialize_dom(rows, char_budget=10, search_terms=["row 300"])
    assert "row 300" in out


def test_search_with_no_match_changes_nothing():
    rows = [_row(f"row {i}", i) for i in range(1, 401)]
    assert serialize_dom(rows, search_terms=["no such element"]) == serialize_dom(rows)


def _state_with(action):
    return AgentState(
        task="open the backend folder",
        context=PageContext(url="http://x", title="x", dom_tree=[]),
        actions_taken=[action],
    )


def test_terms_ignore_opaque_id_selector():
    # the opaque stable-id selector names no target — its digits must not leak
    # into the search terms and pin unrelated nodes that happen to contain them.
    state = _state_with(AgentAction(
        type=ActionType.CLICK, selector='[data-a11y-id="42"]', description="click backend",
    ))
    state.last_action_result = "element not found"
    assert "42" not in _search_terms_from_state(state)


def test_terms_from_expectation_text():
    state = _state_with(AgentAction(
        type=ActionType.CLICK, selector='[data-a11y-id="0"]', description="click",
        expect=Expectation(text_contains="Checkout"),
    ))
    assert "Checkout" in _search_terms_from_state(state)


def test_terms_empty_with_no_actions():
    state = AgentState(
        task="do a thing",
        context=PageContext(url="http://x", title="x", dom_tree=[]),
    )
    assert _search_terms_from_state(state) == []
