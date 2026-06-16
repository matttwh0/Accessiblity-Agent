#fastAPI + websocket endpoints
import logging
import logging.config

logging.config.dictConfig({
    "version": 1,
    "disable_existing_loggers": False,
    "formatters": {
        "agent": {
            "format": "%(asctime)s %(levelname)-7s %(message)s",
            "datefmt": "%H:%M:%S",
        }
    },
    "handlers": {
        "console": {
            "class": "logging.StreamHandler",
            "formatter": "agent",
        }
    },
    "root": {"level": "INFO", "handlers": ["console"]},
    "loggers": {
        "agent": {"level": "DEBUG"},
        "uvicorn.access": {"level": "WARNING"},  # suppress per-request noise
    },
})

logger = logging.getLogger("agent.main")

from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from dotenv import load_dotenv
import json

from agent.graph import build_graph
from agent.nodes import plan_task, perceive, decide_action, verify, recover
from agent.schemas import AgentState, PageContext, DOMNode
from clients.assemblyai import proxy_transcription

load_dotenv()
app = FastAPI()

app.add_middleware(
    CORSMiddleware,
    allow_origins=["chrome-extension://*"],
    allow_methods=["*"],
    allow_headers=["*"]
)

agent_graph = build_graph()

@app.websocket("/transcribe")
async def transcribe_endpoint(ws: WebSocket):
    await ws.accept()
    try:
        await proxy_transcription(ws)
    except WebSocketDisconnect:
        pass

@app.websocket("/agent")
async def agent_endpoint(ws: WebSocket):
    await ws.accept()
    logger.info("=== WebSocket connected ===")
    state = None  # persistent across messages in this connection

    try:
        while True:
            msg = await ws.receive_json()
            logger.info("← msg type=%s", msg["type"])

            if msg["type"] == "start_task":
                logger.info("=== NEW TASK: %r ===", msg["task"])
                state = AgentState(
                    task=msg["task"],
                    context=PageContext(
                        url=msg["url"],
                        title=msg["title"],
                        dom_tree=[DOMNode(**n) for n in msg["dom_tree"]]
                    )
                )
                state = await plan_task(state)
                state = await perceive(state)
                state = await decide_action(state)

            elif msg["type"] == "context_update":
                if state is None:
                    logger.warning("context_update before start_task — ignoring")
                    continue
                state.context = PageContext(
                    url=msg["url"],
                    title=msg["title"],
                    dom_tree=[DOMNode(**n) for n in msg["dom_tree"]]
                )
                state.last_action_result = msg.get("action_result")
                if state.last_action_result:
                    logger.warning("← action_result (FAILED): %s", state.last_action_result)
                state = await verify(state)
                if state.status == "stuck":
                    state = await recover(state)
                    if state.status not in ("done", "failed"):
                        state = await perceive(state)
                elif state.status not in ("done", "failed"):
                    state = await perceive(state)
                    state = await decide_action(state)
            else:
                logger.debug("unknown msg type=%s — ignoring", msg["type"])
                continue

            last_action = state.actions_taken[-1]
            logger.info("→ sending action  type=%s  status=%s  step=%d/%d",
                        last_action.type, state.status, state.steps, state.max_steps)
            await ws.send_json({
                "type": "action",
                "action": last_action.model_dump(exclude={"updated_checklist"}),
                "status": state.status,
                "checklist": state.checklist
            })

            if state.status in ("done", "failed") or state.steps >= state.max_steps:
                logger.info("=== TASK ENDED  status=%s  steps=%d ===", state.status, state.steps)
                break

    except WebSocketDisconnect:
        logger.info("=== WebSocket disconnected ===")
    except Exception as exc:
        logger.exception("=== Unhandled error: %s ===", exc)
        raise
    
@app.post("/test")
async def test_endpoint(payload: dict):
    state = AgentState(
        task=payload["task"],
        context=PageContext(**payload["context"])
    )
    result = await agent_graph.ainvoke(state)
    return {
        "actions": [a.model_dump() for a in result["actions_taken"]],
        "status": result["status"],
        "steps": result["steps"],
        "checklist": result["checklist"]
    }
    
if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000)