// Owns the agent WebSocket so tasks survive page navigations.
// The content script is a thin DOM executor: it announces itself on every
// page load (content_ready), executes actions, and reports results. All
// session state lives here, keyed by tab.
//
// Session states:
//   waiting_backend  - context sent, waiting for the next action
//   executing        - action delivered to the tab, waiting for its result
//   pending_delivery - tab was mid-navigation when an action arrived;
//                      deliver it when the new page's content_ready fires

const sessions = new Map() // tabId -> { ws, state, pendingAction }

const sleep = (ms) => new Promise((r) => setTimeout(r, ms))

// MV3 terminates an idle service worker after ~30s, which would silently drop
// the agent WebSocket mid-task (and with it all session state). While any task
// is live, ping a trivial extension API every 20s: each call resets the idle
// timer, keeping the worker — and its sockets — alive. Stopped once the last
// session ends so we don't hold the worker open for nothing.
let keepAliveTimer = null

function startKeepAlive() {
    if (keepAliveTimer) return
    keepAliveTimer = setInterval(() => chrome.runtime.getPlatformInfo(), 20000)
}

function stopKeepAlive() {
    if (keepAliveTimer && sessions.size === 0) {
        clearInterval(keepAliveTimer)
        keepAliveTimer = null
    }
}

chrome.runtime.onInstalled.addListener(() => {
    console.log('Accessibility Agent installed')
})

chrome.runtime.onMessage.addListener((msg, sender) => {
    const tabId = sender.tab?.id
    if (tabId == null) return
    console.log(`a11y-agent [tab ${tabId}]:`, msg.type)

    if (msg.type === 'start_task') startTask(tabId, msg)
    else if (msg.type === 'action_executed') onActionExecuted(tabId, msg)
    else if (msg.type === 'content_ready') onContentReady(tabId, msg)
})

// a closed tab ends its session
chrome.tabs.onRemoved.addListener((tabId) => endSession(tabId))

function startTask(tabId, msg) {
    endSession(tabId) // a new task replaces any previous one in this tab

    const ws = new WebSocket('ws://localhost:8000/agent')
    const session = { ws, state: 'waiting_backend', pendingAction: null, lastUpdate: null }
    sessions.set(tabId, session)
    startKeepAlive()

    ws.onopen = () => {
        ws.send(JSON.stringify({
            type: 'start_task',
            task: msg.task,
            url: msg.url,
            title: msg.title,
            dom_tree: msg.dom_tree
        }))
    }

    ws.onmessage = (e) => onBackendMessage(tabId, JSON.parse(e.data))

    ws.onerror = (e) => {
        console.error(`a11y-agent [tab ${tabId}]: websocket error`, e)
        notify(tabId, {
            type: 'agent_update',
            description: 'Connection failed — is the backend running?'
        })
    }

    ws.onclose = () => {
        // only report unexpected closes (endSession removes us first)
        const s = sessions.get(tabId)
        if (s && s.ws === ws) {
            sessions.delete(tabId)
            stopKeepAlive()
            notify(tabId, { type: 'agent_update', description: 'Agent connection closed' })
        }
    }
}

// actions the browser executes directly (chrome.tabs) — no content script involved
const BROWSER_ACTIONS = new Set(['navigate', 'back', 'forward', 'reload', 'new_tab'])

function onBackendMessage(tabId, msg) {
    const session = sessions.get(tabId)
    if (!session) return

    // remember the latest UI state so a freshly-loaded page can restore it
    session.lastUpdate = { description: msg.action.description, checklist: msg.checklist }

    if (msg.status === 'done' || msg.status === 'failed') {
        notify(tabId, {
            type: 'agent_update',
            description: msg.action.description,
            checklist: msg.checklist
        })
        endSession(tabId)
        return
    }

    if (BROWSER_ACTIONS.has(msg.action.type)) {
        executeBrowserAction(tabId, session, msg)
        return
    }

    deliverAction(tabId, session, {
        type: 'execute_action',
        action: msg.action,
        checklist: msg.checklist
    })
}

// Executes browser-level actions via chrome.tabs. On success the resulting
// page load fires content_ready, which reports the outcome — same machinery
// as a navigating click. On failure we report through the action_result
// feedback channel so the agent knows it didn't happen.
async function executeBrowserAction(tabId, session, msg) {
    const action = msg.action
    notify(tabId, {
        type: 'agent_update',
        description: action.description,
        checklist: msg.checklist
    })
    session.state = 'executing' // the loaded page's content_ready replies

    try {
        switch (action.type) {
            case 'navigate':
                if (!action.value) throw new Error('navigate action is missing a URL in "value"')
                await chrome.tabs.update(tabId, { url: action.value })
                break
            case 'back':
                await chrome.tabs.goBack(tabId)
                break
            case 'forward':
                await chrome.tabs.goForward(tabId)
                break
            case 'reload':
                await chrome.tabs.reload(tabId)
                break
            case 'new_tab': {
                if (!action.value) throw new Error('new_tab action is missing a URL in "value"')
                const tab = await chrome.tabs.create({ url: action.value, active: true })
                // the session follows the agent into the new tab
                sessions.delete(tabId)
                sessions.set(tab.id, session)
                break
            }
        }
    } catch (err) {
        console.warn(`a11y-agent [tab ${tabId}]: browser action failed:`, err)
        await reportActionFailure(tabId, session, String(err?.message || err))
    }
}

// Report that an action didn't happen: fetch the current DOM from the content
// script and send it back as an action_result error so the agent re-perceives
// and adapts (e.g. "back" with no history, or an undeliverable action).
async function reportActionFailure(tabId, session, error) {
    session.state = 'waiting_backend'
    let ctx = null
    try {
        ctx = await chrome.tabs.sendMessage(tabId, { type: 'collect_context' })
    } catch {}
    if (session.ws.readyState !== WebSocket.OPEN) return
    session.ws.send(JSON.stringify({
        type: 'context_update',
        action_result: error,
        url: ctx?.url ?? '',
        title: ctx?.title ?? '',
        dom_tree: ctx?.dom_tree ?? []
    }))
}

async function deliverAction(tabId, session, payload, attempt = 0) {
    try {
        await chrome.tabs.sendMessage(tabId, payload)
        session.state = 'executing'
        session.pendingAction = null
    } catch {
        // No receiving end. Two cases: (1) the content script is still
        // loading — common right after new_tab — or (2) the page is mid
        // navigation. Retry briefly; a transient load resolves within this
        // window without us doing anything special.
        if (attempt < 6) {
            await sleep(250)
            return deliverAction(tabId, session, payload, attempt + 1)
        }

        // Still no receiver after ~1.5s. Park it so a real navigation's
        // content_ready can still resume it — but a static page (e.g. the
        // search box we just focused) will never fire content_ready, which
        // is exactly how the agent got stranded before. So arm a fallback:
        // if nothing resumes this shortly, tell the backend the action
        // failed so the agent re-perceives instead of hanging silently.
        session.pendingAction = payload
        session.state = 'pending_delivery'
        setTimeout(() => {
            if (session.pendingAction === payload && session.state === 'pending_delivery') {
                session.pendingAction = null
                reportActionFailure(tabId, session, `could not deliver ${payload.action.type} to the page`)
            }
        }, 4000)
    }
}

function onActionExecuted(tabId, msg) {
    const session = sessions.get(tabId)
    if (!session || session.ws.readyState !== WebSocket.OPEN) return

    session.state = 'waiting_backend'
    session.ws.send(JSON.stringify({
        type: 'context_update',
        action_result: msg.action_result,
        url: msg.url,
        title: msg.title,
        dom_tree: msg.dom_tree
    }))
}

function onContentReady(tabId, msg) {
    const session = sessions.get(tabId)
    if (!session) return // no task in this tab — ignore

    // restore the bubble UI (status + checklist) on the freshly loaded page
    if (session.lastUpdate) {
        notify(tabId, { type: 'agent_update', ...session.lastUpdate })
    }

    if (session.state === 'pending_delivery' && session.pendingAction) {
        // an action arrived while the page was navigating; run it here
        deliverAction(tabId, session, session.pendingAction)
    } else if (session.state === 'executing') {
        // the last action navigated, so the old page never replied.
        // The new page itself is the action's outcome — report it.
        session.state = 'waiting_backend'
        if (session.ws.readyState === WebSocket.OPEN) {
            session.ws.send(JSON.stringify({
                type: 'context_update',
                action_result: null,
                url: msg.url,
                title: msg.title,
                dom_tree: msg.dom_tree
            }))
        }
    }
}

function notify(tabId, payload) {
    chrome.tabs.sendMessage(tabId, payload).catch(() => {})
}

function endSession(tabId) {
    const session = sessions.get(tabId)
    if (!session) return
    sessions.delete(tabId) // delete first so onclose stays silent
    try { session.ws.close() } catch {}
    stopKeepAlive()
}
