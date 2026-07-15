#fastAPI + websocket endpoints
import logging
import logging.config
import time

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
from agent.nodes import perceive, decide_action, verify, recover, hash_dom
from agent.schemas import AgentState, PageContext, DOMNode
from clients.assemblyai import proxy_transcription
from clients.claude import reset_usage, usage_summary

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

    # timing markers (perf_counter seconds)
    task_started_at = None   # set when a task begins, for total wall-clock time
    action_sent_at = None    # set when an action is dispatched, for round-trip time

    try:
        while True:
            msg = await ws.receive_json()
            msg_received_at = time.perf_counter()
            # the gap since we dispatched the last action is the extension's
            # execution time: running the action + waiting for the page/expectation
            # to settle + reporting back
            if action_sent_at is not None:
                logger.info("[TIMING] execution (action→settle→report)=%.2fs",
                            msg_received_at - action_sent_at)
                action_sent_at = None
            logger.info("← msg type=%s", msg["type"])

            if msg["type"] == "start_task":
                logger.info("=== NEW TASK: %r ===", msg["task"])
                reset_usage()
                task_started_at = msg_received_at
                state = AgentState(
                    task=msg["task"],
                    context=PageContext(
                        url=msg["url"],
                        title=msg["title"],
                        dom_tree=[DOMNode(**n) for n in msg["dom_tree"]]
                    )
                )
                # planning is merged into the first decide call: one LLM round
                # trip returns the checklist AND the first action
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

                # A re-perceive snapshot we explicitly requested because the
                # agent got stuck: the page has now had time to finish loading,
                # so recover on THIS fresh DOM (with the widened budget) rather
                # than the stale one. Skip verify — we already know it's stuck.
                if msg.get("reperceive"):
                    logger.info("← reperceive snapshot  dom_nodes=%d", len(state.context.dom_tree))
                    state.last_action_result = None
                    state.last_expectation_met = None
                    state = await recover(state)
                    if state.status not in ("done", "failed"):
                        state = await perceive(state)
                else:
                    state.last_action_result = msg.get("action_result")
                    state.last_expectation_met = msg.get("expectation_met")
                    if state.last_action_result:
                        logger.warning("← action_result (FAILED): %s", state.last_action_result)
                    if state.last_expectation_met is False:
                        logger.warning("← expectation NOT met for the last action")
                    state = await verify(state)
                    if state.status == "stuck":
                        if hash_dom(state.context.dom_tree) == state.previous_dom_hash:
                            # Page is provably static (nothing changed since the
                            # last snapshot), so a re-extract would return the
                            # same tree — skip the round trip and recover now.
                            # recover() re-serializes with a widened budget from
                            # the DOM we already hold, which is the part that
                            # actually surfaces truncated-out elements.
                            logger.info("→ page static — recovering without re-extract")
                            state.last_action_result = None
                            state.last_expectation_met = None
                            state = await recover(state)
                            if state.status not in ("done", "failed"):
                                state = await perceive(state)
                        else:
                            # The page DID change recently (may still be loading):
                            # don't recover on a possibly-half-loaded DOM. Ask for
                            # a fresh, settled snapshot; the reperceive reply
                            # (above) drives the actual recovery.
                            logger.info("→ requesting fresh DOM (re-extract on stuck)")
                            await ws.send_json({"type": "collect_context"})
                            # measure the re-extract as the next round trip
                            action_sent_at = time.perf_counter()
                            continue
                    elif state.status not in ("done", "failed"):
                        state = await perceive(state)
                        state = await decide_action(state)
            else:
                logger.debug("unknown msg type=%s — ignoring", msg["type"])
                continue

            last_action = state.actions_taken[-1]
            # server-side processing for this turn: perceive + decide/recover
            # (incl. inference) since the message arrived
            logger.info("[TIMING] server turn (perceive+decide)=%.2fs",
                        time.perf_counter() - msg_received_at)
            logger.info("→ sending action  type=%s  status=%s  step=%d/%d",
                        last_action.type, state.status, state.steps, state.max_steps)
            await ws.send_json({
                "type": "action",
                "action": last_action.model_dump(exclude={"updated_checklist"}),
                "status": state.status,
                "checklist": state.checklist
            })
            action_sent_at = time.perf_counter()

            if state.status in ("done", "failed") or state.steps >= state.max_steps:
                logger.info("=== TASK ENDED  status=%s  steps=%d ===", state.status, state.steps)
                logger.info("=== TOKEN USAGE: %s ===", usage_summary())
                if task_started_at is not None:
                    logger.info("=== [TIMING] TOTAL TRIP=%.2fs  (%.2fs/step avg) ===",
                                time.perf_counter() - task_started_at,
                                (time.perf_counter() - task_started_at) / max(state.steps, 1))
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