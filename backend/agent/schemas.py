#pydantic models
from pydantic import BaseModel
from typing import Literal, Optional
from enum import Enum

class ActionType(str, Enum):
    # page actions (executed by the content script)
    CLICK = "click"
    TYPE = "type"
    SCROLL = "scroll"
    HIGHLIGHT = "highlight"
    WAIT = "wait"
    PRESS_ENTER = "press_enter"   # submit like a real user
    # browser actions (executed by the background worker via chrome.tabs)
    NAVIGATE = "navigate"
    BACK = "back"
    FORWARD = "forward"
    RELOAD = "reload"
    NEW_TAB = "new_tab"
    # terminal
    DONE = "done"
    FAILED = "failed"

class DOMNode(BaseModel):
    tag: str
    text: Optional[str] = None
    label: Optional[str] = None
    role: Optional[str] = None
    selector: str
    visible: bool = True
    # current value of inputs/selects/textareas — lets verify() see that
    # typing actually changed the page
    value: Optional[str] = None
    # a <select>'s option texts — its dropdown is browser UI the agent can
    # never see or click, so the choices must travel with the node
    options: Optional[list[str]] = None

class Expectation(BaseModel):
    """The observable outcome the agent PREDICTS its action will produce.

    Optional, and any combination of fields may be set — all present fields
    must hold for the expectation to count as met. The extension waits for
    these to become true (instead of merely waiting for the DOM to go quiet)
    so an async result isn't reported before it lands.
    """
    url_contains: Optional[str] = None    # substring expected in the URL afterward
    selector: Optional[str] = None        # an element expected to appear (same selector syntax as actions)
    text_contains: Optional[str] = None   # text expected to appear on the page

class AgentAction(BaseModel):
    type: ActionType
    selector: Optional[str] = None
    value: Optional[str] = None
    description: str       # narration shown to user
    reasoning: Optional[str] = None
    # what the agent predicts this action will make true on the page — drives
    # expectation-based waiting + a precise post-action correctness check
    expect: Optional[Expectation] = None
    # full checklist string, returned only when a step was just completed
    # (decide) or the plan needs restructuring (recover)
    updated_checklist: Optional[str] = None

class PageContext(BaseModel):
    url: str
    title: str
    dom_tree: list[DOMNode]

class AgentState(BaseModel):
    task: str
    context: PageContext
    # markdown checklist string: "[ ] pending" / "[x] done", one item per line.
    # Updated when a step completes (decide) or the plan is revised (recover).
    checklist: str = ""
    actions_taken: list[AgentAction] = []
    steps: int = 0
    max_steps: int = 15
    status: Literal[
        "planning", "executing", "verifying", 
        "stuck", "recovering", "done", "failed"
    ] = "planning"
    
    # execution feedback from the extension: None = last action executed,
    # str = error message explaining why it did NOT execute
    last_action_result: Optional[str] = None

    # whether the last action's predicted outcome (AgentAction.expect) came
    # true: True = met, False = the prediction did NOT materialize (a precise
    # failure signal), None = the action made no prediction
    last_expectation_met: Optional[bool] = None

    # recovery tracking
    stuck_count: int = 0
    recovery_attempts: int = 0
    max_recovery_attempts: int = 2
    previous_url: Optional[str] = None
    previous_dom_hash: Optional[str] = None