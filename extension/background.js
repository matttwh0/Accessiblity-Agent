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

// Chrome locks the tab strip while the user drags a tab, and every tab-edit
// API rejects with "Tabs cannot be edited right now". The lock is transient
// (it lifts when the drag ends), so retry briefly instead of failing the
// agent's action — and never let the rejection escape uncaught.
async function editTab(fn, attempts = 4) {
    for (let i = 0; ; i++) {
        try {
            return await fn()
        } catch (err) {
            const transient = /cannot be edited/i.test(String(err?.message || err))
            if (!transient || i >= attempts - 1) throw err
            await sleep(250)
        }
    }
}

// MV3 terminates an idle service worker after ~30s. An open chrome.runtime
// port from the content script is the only reliable keepalive — setInterval
// can be suspended by Chrome before it fires and doesn't prevent termination.
chrome.runtime.onConnect.addListener((port) => {
    if (port.name === 'keepalive') {
        // Reading lastError consumes it so Chrome doesn't log an "Unchecked
        // runtime.lastError" when the content page is bfcached and its keepalive
        // port is severed — an expected disconnect, not an error worth surfacing.
        port.onDisconnect.addListener(() => { void chrome.runtime.lastError })
    }
})

// --- offscreen voice host ------------------------------------------------
// All microphone work (wake word + dictation) lives in ONE extension-owned
// offscreen document: the mic permission is granted once to the extension
// (no per-site prompts) and exactly one recognizer exists browser-wide (no
// per-tab contention). This worker creates that document, decides when it
// should listen, and routes its results to the right tab's content script.

let creatingOffscreen = null   // in-flight createDocument, to dedupe calls
let dictationTabId = null      // which tab the current dictation belongs to

async function ensureOffscreen() {
    const contexts = await chrome.runtime.getContexts({ contextTypes: ['OFFSCREEN_DOCUMENT'] })
    if (contexts.length > 0) return
    if (!creatingOffscreen) {
        creatingOffscreen = chrome.offscreen.createDocument({
            url: 'offscreen.html',
            reasons: ['USER_MEDIA'],
            justification: 'Microphone for voice commands and the "hey helper" wake phrase',
        }).finally(() => { creatingOffscreen = null })
    }
    await creatingOffscreen
}

function sendToOffscreen(msg) {
    chrome.runtime.sendMessage({ target: 'offscreen', ...msg }).catch(() => {})
}

// The Voice Hub: ONE pinned extension tab (voice.html) hosts the wake-word
// listener — the only context where getUserMedia and SpeechRecognition both
// answer to the extension's own permission (content scripts and iframes are
// subject to each site's Permissions-Policy; offscreen docs can't run SR).
// The hub exists while the toggle is on and pauses when Chrome loses focus.
const VOICE_HUB_URL = chrome.runtime.getURL('voice.html')

async function findVoiceHub() {
    try {
        const tabs = await chrome.tabs.query({ url: VOICE_HUB_URL })
        return tabs[0]?.id ?? null
    } catch { return null }
}

function sendHubCmd(tabId, on, off = false) {
    chrome.tabs.sendMessage(tabId, { target: 'voice-hub', type: 'wake_listen', on, off }).catch(() => {})
}

async function updateVoiceHub() {
    let enabled = false
    try { enabled = !!(await chrome.storage.local.get('wakeWordEnabled')).wakeWordEnabled } catch {}
    let focused = false
    try { focused = !!(await chrome.windows.getLastFocused()).focused } catch {}
    let hub = await findVoiceHub()
    if (!enabled) {
        // toggle is off — the hub tab has no job; close it to keep things tidy
        if (hub != null) editTab(() => chrome.tabs.remove(hub)).catch(() => {})
        return
    }
    // pre-create the offscreen document while voice is on: the wake→dictation
    // hand-off then skips document creation, so fewer of the user's first
    // words fall into the startup gap
    ensureOffscreen().catch(() => {})
    if (hub == null) {
        try {
            const tab = await editTab(() => chrome.tabs.create({ url: VOICE_HUB_URL, pinned: true, active: false }))
            hub = tab.id
            // freshly created page isn't listening yet — it announces with
            // voice_hub_ready, which re-runs this reconcile
            return
        } catch (err) {
            console.warn('a11y-agent: could not open voice hub:', err)
            return
        }
    }
    sendHubCmd(hub, focused)  // paused (not off) while Chrome is unfocused
}

chrome.windows.onFocusChanged.addListener(() => { updateVoiceHub() })
chrome.storage.onChanged.addListener((changes, area) => {
    if (area === 'local' && changes.wakeWordEnabled) updateVoiceHub()
})
chrome.runtime.onStartup.addListener(() => { updateVoiceHub() })
updateVoiceHub()  // service worker (re)started — reconcile now

// closing the hub tab = turning voice off; flip the shared toggle so every
// page's 👂 button reflects reality (they watch storage)
chrome.tabs.onRemoved.addListener(async () => {
    try {
        const { wakeWordEnabled } = await chrome.storage.local.get('wakeWordEnabled')
        if (wakeWordEnabled && (await findVoiceHub()) == null) {
            chrome.storage.local.set({ wakeWordEnabled: false })
        }
    } catch {}
})

async function activeTabId() {
    const [tab] = await chrome.tabs.query({ active: true, lastFocusedWindow: true })
    return tab?.id ?? null
}

async function dictationTarget() {
    if (dictationTabId != null) return dictationTabId
    // service worker may have restarted mid-dictation — try the saved id
    try {
        const { dictationTabId: saved } = await chrome.storage.session.get('dictationTabId')
        if (saved != null) return saved
    } catch {}
    return activeTabId()
}

// The dictation's transcripts and the resulting start_task all land in the
// target tab's content script — if none is listening there, the whole command
// would vanish silently. A script can be missing because the page loaded
// before the extension was (re)loaded (orphaned copy) — re-inject it — or
// because the page can't be scripted at all (chrome://, Web Store), in which
// case say so on the Voice Hub instead of dying silently.
async function ensureContentScript(tabId) {
    if (tabId == null) return false
    try {
        await chrome.tabs.sendMessage(tabId, { type: 'ping' })
        return true
    } catch {}
    try {
        await chrome.scripting.insertCSS({ target: { tabId }, files: ['bubble.css'] })
        await chrome.scripting.executeScript({ target: { tabId }, files: ['content.js'] })
        return true
    } catch {
        return false
    }
}

async function startDictationForTab(tabId) {
    dictationTabId = tabId ?? (await activeTabId())
    if (!(await ensureContentScript(dictationTabId))) {
        const hub = await findVoiceHub()
        if (hub != null) chrome.tabs.sendMessage(hub, {
            target: 'voice-hub', type: 'hub_notice',
            text: "I can't help on this page — switch to a normal website tab and say \"Hey Helper\" again.",
        }).catch(() => {})
        dictationTabId = null
        updateVoiceHub()  // resume wake listening instead of hanging
        return
    }
    try { chrome.storage.session.set({ dictationTabId }) } catch {}
    await ensureOffscreen()
    sendToOffscreen({ type: 'offscreen_dictate_start' })
}

// messages from extension pages: offscreen doc, permission page, wake frames
async function onExtensionPageMessage(msg, sender) {
    if (msg.type === 'voice_hub_ready') {
        // the hub page (re)loaded — reconcile so it gets its listen command
        updateVoiceHub()
    } else if (msg.type === 'wake_heard') {
        // the hub stopped listening before sending; the command belongs to
        // whatever tab the user is actually looking at
        startDictationForTab(null)
    } else if (msg.type === 'dictation_started') {
        if (dictationTabId == null) {  // wake-word-triggered — bind to active tab
            dictationTabId = await activeTabId()
            try { chrome.storage.session.set({ dictationTabId }) } catch {}
        }
        notify(await dictationTarget(), { type: 'voice_state', recording: true })
    } else if (msg.type === 'dictation_ended') {
        notify(await dictationTarget(), { type: 'voice_state', recording: false })
        dictationTabId = null
        try { chrome.storage.session.remove('dictationTabId') } catch {}
        updateVoiceHub()  // hand the mic back to the wake listener
    } else if (msg.type === 'dictation_transcript') {
        notify(await dictationTarget(), { type: 'voice_transcript', text: msg.text, is_final: msg.is_final })
        // end-of-turn = the command is complete; stop capturing (and billing)
        if (msg.is_final) sendToOffscreen({ type: 'offscreen_dictate_stop' })
    } else if (msg.type === 'dictation_error') {
        notify(await dictationTarget(), { type: 'voice_error', error: msg.error })
    } else if (msg.type === 'stop_phrase_heard') {
        // "thank you helper": stop the active tab's task (or any running task)
        const tabId = await activeTabId()
        if (tabId != null && sessions.has(tabId)) {
            stopTask(tabId)
            notify(tabId, { type: 'agent_update', description: "You're welcome! I've stopped.", ended: true })
        } else {
            for (const id of [...sessions.keys()]) stopTask(id)
        }
    } else if (msg.type === 'mic_permission_needed') {
        // dictation (offscreen getUserMedia) failed. If the extension already
        // holds the grant, the failure is elsewhere — e.g. Chrome lacks
        // OS-level mic access — and no setup UI can help; say so instead.
        let granted = false
        try { granted = (await navigator.permissions.query({ name: 'microphone' })).state === 'granted' } catch {}
        const t = await dictationTarget()
        if (granted) {
            if (t != null) notify(t, {
                type: 'voice_error',
                error: 'Microphone unavailable — check that Chrome has mic access in your system settings'
            })
        } else {
            // send the user to the Voice Hub, which hosts the grant button —
            // focusing an existing hub is idempotent, so this can never spam
            let hub = await findVoiceHub()
            if (hub == null) {
                try { hub = (await editTab(() => chrome.tabs.create({ url: VOICE_HUB_URL, pinned: true }))).id } catch {}
            }
            if (hub != null) editTab(() => chrome.tabs.update(hub, { active: true })).catch(() => {})
            if (t != null) notify(t, { type: 'voice_error', error: 'Voice needs a one-time setup — see the Helper voice tab' })
        }
        dictationTabId = null
        try { chrome.storage.session.remove('dictationTabId') } catch {}
    } else if (msg.type === 'mic_permission_granted') {
        updateVoiceHub()  // hub granted — reconcile so it starts listening
    }
}

chrome.runtime.onInstalled.addListener(() => {
    console.log('Accessibility Agent installed')
})

chrome.runtime.onMessage.addListener((msg, sender) => {
    // Extension-page messages (offscreen doc, permission page) are tagged
    // target:'background'. Route on the TAG, not on sender.tab — the
    // permission page lives in a real tab, so a sender.tab check misroutes
    // its 'mic_permission_granted' into the content-script branch where it
    // silently matches nothing (and the wake listener never unblocks).
    if (msg.target === 'background') {
        onExtensionPageMessage(msg, sender)
        return
    }
    const tabId = sender.tab?.id
    if (tabId == null) return
    console.log(`a11y-agent [tab ${tabId}]:`, msg.type)

    if (msg.type === 'start_task') startTask(tabId, msg)
    else if (msg.type === 'stop_task') stopTask(tabId)
    else if (msg.type === 'voice_start') startDictationForTab(tabId)
    else if (msg.type === 'voice_stop') sendToOffscreen({ type: 'offscreen_dictate_stop' })
    else if (msg.type === 'action_executed') onActionExecuted(tabId, msg)
    else if (msg.type === 'content_ready') onContentReady(tabId, msg)
})

// User asked the agent to stand down (Stop button or "thank you helper").
// Closing the session's WebSocket makes the backend's task loop exit on its
// next send/receive — at most one in-flight LLM call completes, nothing new starts.
function stopTask(tabId) {
    if (!sessions.has(tabId)) return
    endSession(tabId)
    notify(tabId, { type: 'agent_update', description: 'Stopped.', ended: true })
}

// a closed tab ends its session
chrome.tabs.onRemoved.addListener((tabId) => endSession(tabId))

// When a click inside an executing tab opens a new tab (target="_blank" links,
// window.open, etc.), transfer the session to the new tab so the agent follows
// the result of its own action instead of staying stranded on the source page.
//
// Timing: onCreated fires synchronously with tab creation, well before
// content_ready or awaitSettle's 350ms quiescence timer. So by the time the
// old page reports action_executed it finds no session (ignored), and the new
// tab's content_ready correctly picks up the transferred session.
chrome.tabs.onCreated.addListener((tab) => {
    if (tab.openerTabId == null) return
    const session = sessions.get(tab.openerTabId)
    if (!session || session.state !== 'executing') return
    sessions.delete(tab.openerTabId)
    sessions.set(tab.id, session)
})

function startTask(tabId, msg) {
    endSession(tabId) // a new task replaces any previous one in this tab

    const ws = new WebSocket('ws://localhost:8000/agent')
    const session = { ws, state: 'waiting_backend', pendingAction: null, pendingExpect: null, lastUpdate: null, navFallback: null }
    sessions.set(tabId, session)

    ws.onopen = () => {
        ws.send(JSON.stringify({
            type: 'start_task',
            task: msg.task,
            url: msg.url,
            title: msg.title,
            dom_tree: msg.dom_tree,
            // present only when the user has a profile and hasn't disabled it
            ...(msg.profile ? { profile: msg.profile } : {})
        }))
    }

    ws.onmessage = (e) => onBackendMessage(tabId, JSON.parse(e.data))

    ws.onerror = (e) => {
        console.error(`a11y-agent [tab ${tabId}]: websocket error`, e)
        notify(tabId, {
            type: 'agent_update',
            description: 'Connection failed — is the backend running?',
            ended: true
        })
    }

    ws.onclose = () => {
        // only report unexpected closes (endSession removes us first)
        const s = sessions.get(tabId)
        if (s && s.ws === ws) {
            sessions.delete(tabId)
            notify(tabId, { type: 'agent_update', description: 'Agent connection closed', ended: true })
        }
    }
}

// actions the browser executes directly (chrome.tabs) — no content script involved
const BROWSER_ACTIONS = new Set(['navigate', 'back', 'forward', 'reload', 'new_tab'])

function onBackendMessage(tabId, msg) {
    const session = sessions.get(tabId)
    if (!session) return

    // The backend got stuck and wants a fresh look at the page (the element it
    // needed may have finished loading, or been truncated out of the last
    // snapshot). Re-collect a settled DOM and send it back — no action runs.
    if (msg.type === 'collect_context') {
        reperceive(tabId, session)
        return
    }

    // remember the latest UI state so a freshly-loaded page can restore it
    session.lastUpdate = { description: msg.action.description, checklist: msg.checklist }

    // the action's predicted outcome — kept so that if the action navigates,
    // the new page (not the dying old one) can still check the prediction
    session.pendingExpect = msg.action.expect ?? null

    if (msg.status === 'done' || msg.status === 'failed') {
        notify(tabId, {
            type: 'agent_update',
            description: msg.action.description,
            checklist: msg.checklist,
            ended: true
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

    // If content_ready doesn't arrive within 6s the navigation either didn't
    // happen (e.g. 'back' with no history) or landed somewhere unreachable.
    // Report the current page so the agent can recover instead of hanging.
    session.navFallback = setTimeout(async () => {
        if (session.state !== 'executing') return
        session.navFallback = null
        await reportActionFailure(tabId, session, `${action.type} did not trigger a navigation`)
    }, 6000)

    try {
        switch (action.type) {
            case 'navigate':
                if (!action.value) throw new Error('navigate action is missing a URL in "value"')
                await editTab(() => chrome.tabs.update(tabId, { url: action.value }))
                break
            case 'back':
                await editTab(() => chrome.tabs.goBack(tabId))
                break
            case 'forward':
                await editTab(() => chrome.tabs.goForward(tabId))
                break
            case 'reload':
                await editTab(() => chrome.tabs.reload(tabId))
                break
            case 'new_tab': {
                if (!action.value) throw new Error('new_tab action is missing a URL in "value"')
                const tab = await editTab(() => chrome.tabs.create({ url: action.value, active: true }))
                // the session follows the agent into the new tab
                sessions.delete(tabId)
                sessions.set(tab.id, session)
                break
            }
        }
    } catch (err) {
        clearTimeout(session.navFallback)
        session.navFallback = null
        console.warn(`a11y-agent [tab ${tabId}]: browser action failed:`, err)
        await reportActionFailure(tabId, session, String(err?.message || err))
    }
}

// Report that an action didn't happen: fetch the current DOM from the content
// script and send it back as an action_result error so the agent re-perceives
// and adapts (e.g. "back" with no history, or an undeliverable action).
async function reportActionFailure(tabId, session, error) {
    session.pendingExpect = null
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

// Re-collect the current page (a settled snapshot — collect_context waits for
// the DOM to go quiet) and send it back flagged as a re-perceive, so the
// backend recovers against fresh, fully-loaded content instead of the stale
// snapshot it was stuck on.
async function reperceive(tabId, session) {
    let ctx = null
    try {
        ctx = await chrome.tabs.sendMessage(tabId, { type: 'collect_context' })
    } catch {}
    if (session.ws.readyState !== WebSocket.OPEN) return
    session.state = 'waiting_backend'
    session.ws.send(JSON.stringify({
        type: 'context_update',
        reperceive: true,
        action_result: null,
        expectation_met: null,
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

    // the in-page path already checked the prediction; consume it
    session.pendingExpect = null
    session.state = 'waiting_backend'
    session.ws.send(JSON.stringify({
        type: 'context_update',
        action_result: msg.action_result,
        expectation_met: msg.expectation_met ?? null,
        url: msg.url,
        title: msg.title,
        dom_tree: msg.dom_tree
    }))
}

async function onContentReady(tabId, msg) {
    const session = sessions.get(tabId)
    if (!session) return // no task in this tab — ignore

    // a real page loaded — cancel the no-navigation fallback if armed
    if (session.navFallback) {
        clearTimeout(session.navFallback)
        session.navFallback = null
    }

    // restore the bubble UI (status + checklist) on the freshly loaded page
    if (session.lastUpdate) {
        notify(tabId, { type: 'agent_update', ...session.lastUpdate })
    }

    if (session.state === 'pending_delivery' && session.pendingAction) {
        // an action arrived while the page was navigating; run it here
        deliverAction(tabId, session, session.pendingAction)
    } else if (session.state === 'executing') {
        // the last action navigated, so the old page never replied.
        // The new page itself is the action's outcome — report it. If the
        // action carried a prediction (e.g. url should contain "pulls"), check
        // it here against the page that actually loaded.
        let expectation_met = null
        if (session.pendingExpect) {
            try {
                expectation_met = await chrome.tabs.sendMessage(tabId, {
                    type: 'evaluate_expectation',
                    expect: session.pendingExpect,
                })
            } catch { /* content script not ready — leave null */ }
        }
        session.pendingExpect = null
        session.state = 'waiting_backend'
        if (session.ws.readyState === WebSocket.OPEN) {
            session.ws.send(JSON.stringify({
                type: 'context_update',
                action_result: null,
                expectation_met,
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
    if (session.navFallback) { clearTimeout(session.navFallback); session.navFallback = null }
    sessions.delete(tabId) // delete first so onclose stays silent
    try { session.ws.close() } catch {}
}
