# Tests for smart search: model-provided site-vocabulary hints + literal task
# words pinned into every serialization, and newly-revealed nodes (an opened
# hover menu) boosted to the top of the ranking. Guards the Amazon "Watchlist
# was right there" failure: the element existed in the tree but was truncated
# out before the model ever saw it.
import asyncio

from agent.nodes import decide_action, perceive, recover
from agent.schemas import (
    ActionType, AgentAction, AgentState, DOMNode, PageContext,
)
from clients.claude import (
    ACTION_TOOL, RECOVERY_PROMPT, SYSTEM_PROMPT,
    mine_task_terms, node_signature, pinned_indices, serialize_dom,
)


def _link(text, ident=0):
    return DOMNode(tag="a", text=text, selector=f'[data-a11y-id="{ident}"]')


def _button(text, ident=0):
    return DOMNode(tag="button", text=text, selector=f'[data-a11y-id="{ident}"]')


def _state(task="find my watchlist", dom_tree=None, **kwargs):
    return AgentState(
        task=task,
        context=PageContext(url="https://amazon.com", title="Amazon",
                            dom_tree=dom_tree or []),
        **kwargs,
    )


# --- literal term mining ---------------------------------------------------

def test_mine_terms_keeps_content_words():
    terms = mine_task_terms("how do i check what i bought in the past")
    assert "bought" in terms
    assert "check" not in terms  # generic action word
    assert "how" not in terms


def test_mine_terms_includes_first_pending_checklist_item():
    terms = mine_task_terms(
        "find it",
        "[x] Go to the homepage\n[ ] Highlight the Watchlist entry\n[ ] Celebrate",
    )
    assert "watchlist" in terms
    assert "celebrate" not in terms  # only the step being worked on


def test_mine_terms_deduplicates_and_caps():
    terms = mine_task_terms("watchlist " * 30 + "alpha beta gamma")
    assert terms.count("watchlist") == 1
    assert len(terms) <= 10


# --- guarded pinning -------------------------------------------------------

def test_overly_common_term_is_dropped():
    # "amazon" matches every node here — pinning it would pin the whole page
    nodes = [_link(f"Amazon deal {i}", i) for i in range(50)]
    assert pinned_indices(nodes, ["amazon"]) == set()


def test_discriminative_term_still_pins():
    nodes = [_link(f"Amazon deal {i}", i) for i in range(50)] + [_link("Watchlist", 99)]
    assert pinned_indices(nodes, ["watchlist"]) == {50}


def test_total_pin_cap_enforced():
    # 3 terms x 12 matches each = 36 candidates; the cap keeps the first 30
    nodes = ([_link(f"alpha {i}", i) for i in range(12)]
             + [_link(f"beta {i}", 100 + i) for i in range(12)]
             + [_link(f"gamma {i}", 200 + i) for i in range(12)])
    pinned = pinned_indices(nodes, ["alpha", "beta", "gamma"])
    assert len(pinned) == 30


def test_task_word_pins_node_on_crowded_page():
    # the Watchlist regression: hundreds of higher/equal-priority nodes must
    # not crowd out the element the task literally names
    crowd = [_button(f"Buy now {i}", i) for i in range(300)]
    target = _link("Watchlist", 999)
    out = serialize_dom(crowd + [target], search_terms=mine_task_terms("find my watchlist"))
    assert "Watchlist" in out


# --- revealed-node boost ---------------------------------------------------

def _perceive(state):
    return asyncio.run(perceive(state))


def test_revealed_nodes_detected_on_same_page():
    before = [_button(f"b{i}", i) for i in range(20)]
    state = _perceive(_state(dom_tree=before))
    menu_items = [_link("Watchlist", 100), _link("Your Orders", 101)]
    state.context = PageContext(url="https://amazon.com", title="Amazon",
                                dom_tree=before + menu_items)
    state = _perceive(state)
    assert state.revealed_signatures == {node_signature(n) for n in menu_items}


def test_hidden_item_turning_visible_counts_as_revealed():
    hidden = DOMNode(tag="a", text="Watchlist", visible=False,
                     selector='[data-a11y-id="5"]')
    before = [_button(f"b{i}", i) for i in range(10)] + [hidden]
    state = _perceive(_state(dom_tree=before))
    shown = DOMNode(tag="a", text="Watchlist", visible=True,
                    selector='[data-a11y-id="5"]')
    state.context = PageContext(url="https://amazon.com", title="Amazon",
                                dom_tree=before[:-1] + [shown])
    state = _perceive(state)
    assert node_signature(shown) in state.revealed_signatures


def test_navigation_clears_revealed():
    state = _perceive(_state(dom_tree=[_button("b", 0)]))
    state.context = PageContext(url="https://amazon.com/orders", title="Orders",
                                dom_tree=[_link("Order 1", 0)])
    state = _perceive(state)
    assert state.revealed_signatures == set()


def test_wholesale_redraw_is_not_a_reveal():
    # same URL but most nodes replaced (an SPA re-render) — boosting everything
    # would be a no-op ranking change at best; the guard clears it
    state = _perceive(_state(dom_tree=[_button(f"b{i}", i) for i in range(10)]))
    state.context = PageContext(url="https://amazon.com", title="Amazon",
                                dom_tree=[_link(f"new {i}", i) for i in range(10)])
    state = _perceive(state)
    assert state.revealed_signatures == set()


def test_boosted_node_survives_busy_serialization():
    # a revealed menu link must outrank 300 buttons even though links normally
    # rank below buttons
    crowd = [_button(f"Buy now {i}", i) for i in range(300)]
    target = _link("Special offer entry", 999)
    baseline = serialize_dom(crowd + [target])
    assert "Special offer entry" not in baseline  # dropped without the boost
    out = serialize_dom(crowd + [target],
                        boost_signatures={node_signature(target)})
    assert "Special offer entry" in out


# --- hint plumbing ---------------------------------------------------------

def _stub(monkeypatch, target, action):
    async def fake(state, rejection_note=None):
        return action
    monkeypatch.setattr(target, fake)


def test_decide_stores_search_hints(monkeypatch):
    _stub(monkeypatch, "agent.nodes.stream_action", AgentAction(
        type=ActionType.CLICK, selector='[data-a11y-id="0"]',
        description="I'm opening your orders now.",
        search_hints=["orders", "order history", "  purchases  ", ""],
    ))
    state = asyncio.run(decide_action(_state(task="what did i buy")))
    assert state.search_hints == ["orders", "order history", "purchases"]


def test_recover_refreshes_search_hints(monkeypatch):
    async def fake(state):
        return AgentAction(
            type=ActionType.CLICK, selector='[data-a11y-id="0"]',
            description="Let me try a different way.",
            search_hints=["returns", "account"],
        )
    monkeypatch.setattr("agent.nodes.stream_recovery_action", fake)
    state = _state(search_hints=["orders"])
    state = asyncio.run(recover(state))
    assert state.search_hints == ["returns", "account"]


def test_tool_schema_and_prompts_cover_hints():
    assert "search_hints" in ACTION_TOOL["input_schema"]["properties"]
    assert "search_hints" in SYSTEM_PROMPT
    assert "search_hints" in RECOVERY_PROMPT
