from langgraph.graph import StateGraph, END
from .schemas import AgentState
from .nodes import perceive, decide_action, verify, recover

def build_graph():
    graph = StateGraph(AgentState)

    graph.add_node("perceive", perceive)
    graph.add_node("decide", decide_action)
    graph.add_node("verify", verify)
    graph.add_node("recover", recover)

    # planning is merged into the first decide call (one LLM round trip
    # returns checklist + first action), so the loop starts at perceive
    graph.set_entry_point("perceive")
    graph.add_edge("perceive", "decide")
    graph.add_edge("decide", "verify")
    graph.add_edge("recover", "verify")  # recovery loops back through verify
    
    def route_after_verify(state: AgentState):
        if state.status in ("done", "failed"):
            return END
        if state.status == "stuck":
            return "recover"
        if state.steps >= state.max_steps:
            return END
        return "perceive"
    
    graph.add_conditional_edges("verify", route_after_verify)
    
    return graph.compile()