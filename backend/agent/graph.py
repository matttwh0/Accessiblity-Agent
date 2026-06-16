from langgraph.graph import StateGraph, END
from .schemas import AgentState
from .nodes import plan_task, perceive, decide_action, verify, recover

def build_graph():
    graph = StateGraph(AgentState)

    graph.add_node("plan", plan_task)
    graph.add_node("perceive", perceive)
    graph.add_node("decide", decide_action)
    graph.add_node("verify", verify)
    graph.add_node("recover", recover)

    # plan runs once up front; the loop re-enters at perceive
    graph.set_entry_point("plan")
    graph.add_edge("plan", "perceive")
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