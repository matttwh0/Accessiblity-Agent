# backend/test_agent.py
import asyncio
from dotenv import load_dotenv
load_dotenv()  # load ANTHROPIC_API_KEY from .env (test_agent runs standalone)
from agent.graph import build_graph
from agent.schemas import AgentState, PageContext, DOMNode

async def main():
    graph = build_graph()
    state = AgentState(
        task='delete my Facebook account',
        context=PageContext(
            url='https://example.com',
            title='Test',
            dom_tree=[
                DOMNode(tag='input', label='Search', selector='input#search'),
                DOMNode(tag='button', text='Submit', selector='button[type="submit"]'),
            ]
        )
    )
    
    print("Starting graph...")
    result = await graph.ainvoke(state)
    print(f"Result type: {type(result)}")
    print(f"Result keys/attrs: {result}")
    
    # try both access patterns
    if isinstance(result, dict):
        actions = result.get('actions_taken', [])
    else:
        actions = result.actions_taken
    
    print(f"\nActions taken ({len(actions)}):")
    for i, action in enumerate(actions):
        print(f"{i+1}. {action}")

asyncio.run(main())