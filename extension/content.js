// create the floating bubble — removing any copy left by a previous injection
// (reloading the extension orphans this script but leaves its DOM behind, and
// the background re-injects us into such tabs before starting a dictation)
document.getElementById('a11y-agent-bubble')?.remove()
const bubble = document.createElement('div')
bubble.id = 'a11y-agent-bubble'
bubble.innerHTML = `
  <div id="a11y-agent-icon">🤖</div>
  <div id="a11y-agent-panel" style="display:none">
    <div id="a11y-agent-input-row">
      <input id="a11y-agent-input" placeholder="What do you need help with?" />
      <button id="a11y-agent-mic" title="Speak your request">🎤</button>
    </div>
    <button id="a11y-agent-stop" style="display:none">⏹ Stop helping</button>
    <button id="a11y-agent-wake">👂 Say "Hey Helper": Off</button>
    <button id="a11y-agent-myinfo">⚙ My info</button>
    <div id="a11y-agent-checklist" style="display:none"></div>
    <div id="a11y-agent-status"></div>
  </div>
`
document.body.appendChild(bubble)

const icon = document.getElementById('a11y-agent-icon')
const panel = document.getElementById('a11y-agent-panel')
const input = document.getElementById('a11y-agent-input')
const mic = document.getElementById('a11y-agent-mic')
const stopBtn = document.getElementById('a11y-agent-stop')
const wakeBtn = document.getElementById('a11y-agent-wake')
const checklist = document.getElementById('a11y-agent-checklist')
const status = document.getElementById('a11y-agent-status')
const myInfoBtn = document.getElementById('a11y-agent-myinfo')

myInfoBtn.addEventListener('click', () => {
    if (!extensionAlive()) {
        status.textContent = '⚠ Extension was updated — refresh this page and try again'
        return
    }
    try { chrome.runtime.openOptionsPage() } catch {}
})

function renderChecklist(text) {
    if (!text || !text.trim()) {
        checklist.style.display = 'none'
        return
    }
    checklist.innerHTML = ''
    let currentMarked = false
    for (const line of text.split('\n')) {
        if (!line.trim()) continue
        const done = /^\s*(-\s*)?\[[xX]\]/.test(line)
        const label = line.replace(/^\s*(-\s*)?\[\s*[xX]?\s*\]\s*/, '')
        const item = document.createElement('div')
        item.className = 'a11y-agent-step' + (done ? ' done' : '')
        // emphasize the first pending step (the one being worked on)
        if (!done && !currentMarked) {
            item.classList.add('current')
            currentMarked = true
        }
        const mark = document.createElement('span')
        mark.className = 'a11y-agent-step-mark'
        mark.textContent = done ? '✓' : '○'
        item.appendChild(mark)
        item.appendChild(document.createTextNode(label))
        checklist.appendChild(item)
    }
    checklist.style.display = 'block'
}

icon.addEventListener('click', () => {
    // a press that turned into a drag reaches here as a click too — ignore it
    // so repositioning the icon never also opens/closes the panel
    if (suppressClick) { suppressClick = false; return }
    if (panel.style.display === 'none') {
        positionPanel()
        panel.style.display = 'block'
        input.focus()
    } else {
        panel.style.display = 'none'
    }
})

// --- draggable icon -------------------------------------------------------
// The bubble sits bottom-right by default; dragging the 🤖 icon pins it with
// left/top instead and remembers the spot (per browser) so it stays put across
// page loads. A drag must not also fire the panel-toggle click, and the panel
// opens toward whichever screen edges have room once the icon has moved.
const DRAG_THRESHOLD = 4  // px of travel before a press counts as a drag
let dragState = null      // { startX, startY, grabX, grabY, moved } while pressing
let suppressClick = false

function clampToViewport(left, top) {
    const maxLeft = Math.max(0, window.innerWidth - bubble.offsetWidth)
    const maxTop = Math.max(0, window.innerHeight - bubble.offsetHeight)
    return {
        left: Math.max(0, Math.min(left, maxLeft)),
        top: Math.max(0, Math.min(top, maxTop)),
    }
}

function moveBubbleTo(left, top) {
    const c = clampToViewport(left, top)
    bubble.style.left = c.left + 'px'
    bubble.style.top = c.top + 'px'
    bubble.style.right = 'auto'
    bubble.style.bottom = 'auto'
    return c
}

// Anchor the panel to the icon's corner that has the most room, so a bubble
// dragged to the top or left doesn't open its panel off-screen.
function positionPanel() {
    const r = icon.getBoundingClientRect()
    if (r.top < window.innerHeight / 2) {
        panel.style.top = '70px'; panel.style.bottom = 'auto'
    } else {
        panel.style.bottom = '70px'; panel.style.top = 'auto'
    }
    if (r.left > window.innerWidth / 2) {
        panel.style.right = '0'; panel.style.left = 'auto'
    } else {
        panel.style.left = '0'; panel.style.right = 'auto'
    }
}

icon.addEventListener('pointerdown', (e) => {
    if (e.button !== 0) return
    const rect = bubble.getBoundingClientRect()
    dragState = {
        startX: e.clientX,
        startY: e.clientY,
        // where inside the bubble the pointer grabbed, so it doesn't jump
        grabX: e.clientX - rect.left,
        grabY: e.clientY - rect.top,
        moved: false,
    }
    try { icon.setPointerCapture(e.pointerId) } catch {}
})

icon.addEventListener('pointermove', (e) => {
    if (!dragState) return
    if (!dragState.moved &&
        Math.hypot(e.clientX - dragState.startX, e.clientY - dragState.startY) < DRAG_THRESHOLD) return
    dragState.moved = true
    icon.classList.add('dragging')
    // moving the icon closes the panel so it can't hang detached mid-drag
    panel.style.display = 'none'
    moveBubbleTo(e.clientX - dragState.grabX, e.clientY - dragState.grabY)
})

function endDrag(e) {
    if (!dragState) return
    const moved = dragState.moved
    dragState = null
    icon.classList.remove('dragging')
    try { icon.releasePointerCapture(e.pointerId) } catch {}
    if (moved) {
        suppressClick = true  // this press was a drag — the click it spawns is not a toggle
        const r = bubble.getBoundingClientRect()
        try { chrome.storage.local.set({ bubblePos: { left: r.left, top: r.top } }) } catch {}
    }
}
icon.addEventListener('pointerup', endDrag)
icon.addEventListener('pointercancel', endDrag)

// restore the saved spot (persisted per browser, not per site)
try {
    chrome.storage.local.get('bubblePos').then(({ bubblePos }) => {
        if (bubblePos) moveBubbleTo(bubblePos.left, bubblePos.top)
    }).catch(() => {})
} catch {}

// keep the bubble on-screen when the window shrinks past its pinned spot
window.addEventListener('resize', () => {
    if (bubble.style.left) moveBubbleTo(parseFloat(bubble.style.left), parseFloat(bubble.style.top))
})

// keep only non-empty string fields, trimmed; return null if nothing is left,
// so an empty profile is never sent and the backend behaves as if there is none
function pruneProfile(p) {
    if (!p || typeof p !== 'object') return null
    const out = {}
    for (const [k, v] of Object.entries(p)) {
        if (typeof v === 'string' && v.trim()) out[k] = v.trim()
    }
    return Object.keys(out).length ? out : null
}

async function startTask(task) {
    status.textContent = 'Thinking...'
    renderChecklist('')  // clear any previous task's plan
    setTaskRunning(true)
    let profile = null
    try {
        const { userProfile, useProfile } = await chrome.storage.local.get(['userProfile', 'useProfile'])
        if (useProfile !== false) profile = pruneProfile(userProfile)  // default-on
    } catch {}
    sendToBackground({
        type: 'start_task',
        task,
        url: window.location.href,
        title: document.title,
        dom_tree: extractAccessibilityTree(),
        ...(profile ? { profile } : {})
    })
}

// the Stop button is only visible while a task is running; the background
// tells us when the task ended (msg.ended on its final agent_update)
function setTaskRunning(running) {
    stopBtn.style.display = running ? 'block' : 'none'
}

function stopAgent(voiceThanks) {
    sendToBackground({ type: 'stop_task' })
    setTaskRunning(false)
    status.textContent = voiceThanks ? "You're welcome! I've stopped." : 'Stopped.'
}

stopBtn.addEventListener('click', () => stopAgent(false))

input.addEventListener('keydown', (e) => {
    if (e.key !== 'Enter') return
    const task = input.value.trim()
    if (task) startTask(task)
})

// --- voice input: remote-controls the extension's offscreen microphone ---
// ALL capture (wake word + dictation) lives in the extension's offscreen
// document, so the mic permission is granted ONCE to the extension instead
// of per-site, and exactly one listener exists browser-wide. This script
// only renders state and forwards button presses; transcripts arrive as
// voice_* messages from the background worker.
let voiceActive = false  // a dictation for THIS tab is in progress

function setMicRecording(recording) {
    voiceActive = recording
    mic.classList.toggle('recording', recording)
    mic.title = recording ? 'Stop and send' : 'Speak your request'
}

mic.addEventListener('click', () => {
    if (!extensionAlive()) {
        status.textContent = '⚠ Extension was updated — refresh this page and try again'
        return
    }
    if (voiceActive) {
        // manual stop: submit whatever was heard so far
        setMicRecording(false)
        sendToBackground({ type: 'voice_stop' })
        const task = input.value.trim()
        if (task) startTask(task)
        else status.textContent = "I didn't catch that — try again"
    } else {
        showPanel()
        input.value = ''
        status.textContent = 'Starting microphone…'
        sendToBackground({ type: 'voice_start' })
    }
})


// --- wake word toggle -----------------------------------------------------
// The actual listener lives in the extension's offscreen document (one
// recognizer browser-wide; see background.js). This button just flips the
// shared setting in chrome.storage — the background watches it and arms or
// pauses the offscreen listener, including the privacy rule "never listen
// while no Chrome window has focus".
let wakeOn = false

function renderWakeBtn() {
    wakeBtn.textContent = wakeOn ? '\u{1F442} Say "Hey Helper": On' : '\u{1F442} Say "Hey Helper": Off'
    wakeBtn.classList.toggle('on', wakeOn)
}

wakeBtn.addEventListener('click', () => {
    if (!extensionAlive()) {
        status.textContent = '\u26A0 Extension was updated \u2014 refresh this page and try again'
        return
    }
    wakeOn = !wakeOn
    renderWakeBtn()
    try { chrome.storage.local.set({ wakeWordEnabled: wakeOn }) } catch {}
})

// restore the user's choice (persisted per browser, not per site), and stay
// in sync: the toggle can change from any tab, and closing the Voice Hub tab
// flips it off — every page's 👂 button should reflect that immediately
try {
    chrome.storage.local.get('wakeWordEnabled').then(({ wakeWordEnabled }) => {
        wakeOn = !!wakeWordEnabled
        renderWakeBtn()
    }).catch(() => {})
    chrome.storage.onChanged.addListener((changes, area) => {
        if (area === 'local' && changes.wakeWordEnabled) {
            wakeOn = !!changes.wakeWordEnabled.newValue
            renderWakeBtn()
        }
    })
} catch {}

// Extracts the interactive elements as an accessibility tree, addressing each
// by an opaque, per-snapshot stable id stamped onto the DOM node itself
// (data-a11y-id="N"). The id IS the selector: resolving it later is an exact
// document.querySelector lookup that can't match the wrong element or miss the
// way a text/position-based selector could (whitespace, duplicate text, a list
// that reordered). The id is only valid within THIS snapshot — stale stamps
// from a previous extraction are cleared first so no id is ambiguous, and the
// agent always acts on the freshest snapshot.
function extractAccessibilityTree() {
    const selectors = 'button, a, input, select, textarea, [role], h1, h2, h3, label'
    // drop ids from a previous extraction so each id points at exactly one
    // element in this snapshot (a shrinking page could otherwise leave a stale
    // duplicate that querySelector would resolve to instead)
    document.querySelectorAll('[data-a11y-id]').forEach(el => el.removeAttribute('data-a11y-id'))
    return [...document.querySelectorAll(selectors)]
        .filter(el => {
            const r = el.getBoundingClientRect()
            return r.width > 0 && r.height > 0
        })
        .map((el, i) => {
            el.setAttribute('data-a11y-id', String(i))
            const node = {
                tag: el.tagName.toLowerCase(),
                text: (el.innerText || '').slice(0, 100),
                label: el.ariaLabel || el.placeholder || el.title || null,
                role: el.getAttribute('role'),
                selector: `[data-a11y-id="${i}"]`,
                visible: true,
                // current input value so the backend can see typing took effect
                value: typeof el.value === 'string' ? el.value.slice(0, 100) : null
            }
            // a <select>'s dropdown is browser UI the agent can never see or
            // click — surface its choices so the agent can pick one by "type"
            if (el.tagName === 'SELECT') {
                node.options = [...el.options].slice(0, 20).map(o => o.text.trim().slice(0, 80))
            }
            return node
        })
}

// Like extractAccessibilityTree(), but waits for the DOM to stop mutating
// first. Many sites (GitHub, other SPAs) inject the script before deferred /
// turbo-frame content hydrates, so an immediate snapshot misses controls that
// load a beat later — e.g. the "New issue" button on a GitHub issues page.
// Snapshotting too early strands the agent: it can't click what it never sees,
// so it loops scrolling for a control that isn't in its perception. We wait for
// a quiet gap (capped) so the first snapshot reflects the settled page.
function extractWhenSettled({ quietMs = 200, maxMs = 2500 } = {}) {
    return new Promise((resolve) => {
        let done = false, quietTimer = null, observer = null, capTimer = null
        function finish() {
            if (done) return
            done = true
            clearTimeout(quietTimer); clearTimeout(capTimer)
            if (observer) observer.disconnect()
            resolve(extractAccessibilityTree())
        }
        const bump = () => {
            clearTimeout(quietTimer)
            quietTimer = setTimeout(finish, quietMs)
        }
        observer = new MutationObserver(bump)
        observer.observe(document.documentElement, { childList: true, subtree: true, attributes: true })
        bump()  // start the quiet clock so a static page still resolves promptly
        capTimer = setTimeout(finish, maxMs)  // never hang on a perpetually-animating page
    })
}

function findElement(selector) {
    // handle our pseudo-selector for text matching
    const textMatch = selector.match(/^(\w+):has-text\("(.+)"\)$/)
    if (textMatch) {
        const [, tag, text] = textMatch
        return [...document.querySelectorAll(tag)]
            .find(el => el.innerText?.trim().includes(text))
    }
    try {
        return document.querySelector(selector)
    } catch (e) {
        if (selector.startsWith('#')) {
            return document.getElementById(selector.slice(1))
        }
        return null
    }
}

// Sends a message to the background worker and makes failures VISIBLE.
// A rejection here almost always means this content script is orphaned
// (the extension was reloaded after this page loaded) or the background
// worker failed to register its listener.
function sendToBackground(payload) {
    let p
    try {
        p = chrome.runtime.sendMessage(payload)
    } catch (err) {
        showPanel()
        status.textContent = '⚠ Extension was updated — refresh this page and try again'
        console.error('a11y-agent: sendMessage threw:', err)
        return
    }
    p.catch((err) => {
        showPanel()
        status.textContent = '⚠ Cannot reach extension background — reload the extension, then refresh this page'
        console.error('a11y-agent: background unreachable:', err)
    })
}

// The background service worker owns the WebSocket (so the task survives
// page navigations). This script just executes actions and reports back.
chrome.runtime.onMessage.addListener((msg, sender, sendResponse) => {
    if (msg.type === 'ping') {
        // reachability probe from the background: an orphaned copy of this
        // script (extension was reloaded) can never answer, so a reply proves
        // a live script is here to receive voice transcripts
        sendResponse('pong')
    } else if (msg.type === 'execute_action') {
        setTaskRunning(true)  // a freshly-loaded page learns a task is mid-flight
        handleExecuteAction(msg)
    } else if (msg.type === 'agent_update') {
        showPanel()
        if (msg.description) status.textContent = msg.description
        if (msg.checklist !== undefined) renderChecklist(msg.checklist)
        setTaskRunning(!msg.ended)
    } else if (msg.type === 'voice_state') {
        // the offscreen mic started/stopped a dictation bound to this tab
        if (msg.recording) {
            showPanel()
            input.value = ''
            setMicRecording(true)
            status.textContent = 'Listening… speak now'
        } else {
            setMicRecording(false)
        }
    } else if (msg.type === 'voice_transcript') {
        if (voiceActive) {
            if (msg.text) input.value = msg.text   // live partial transcript
            if (msg.is_final && msg.text && msg.text.trim()) {
                setMicRecording(false)
                startTask(msg.text.trim())
            }
        }
    } else if (msg.type === 'voice_error') {
        showPanel()
        setMicRecording(false)
        status.textContent = `⚠ ${msg.error}`
    } else if (msg.type === 'collect_context') {
        // background needs the current page state (after a failed browser
        // action, or a re-perceive while stuck). Wait for the DOM to settle
        // first so deferred-loaded content (e.g. a repo's file list) is in the
        // snapshot — this is the whole point of re-extracting on stuck.
        extractWhenSettled().then((dom_tree) => {
            sendResponse({
                url: window.location.href,
                title: document.title,
                dom_tree
            })
        })
        return true  // keep the message channel open for the async sendResponse
    } else if (msg.type === 'evaluate_expectation') {
        // the previous action navigated here; check its predicted outcome
        // against THIS freshly loaded page (the prediction couldn't be checked
        // on the old page, which had already unloaded)
        sendResponse(expectationMet(msg.expect))
    }
})

// Tracks whether this page has begun unloading (a navigation). Set once and
// read by settleOrNavigate so an action that navigates stays silent.
let pageIsUnloading = false
window.addEventListener('pagehide', () => { pageIsUnloading = true })
window.addEventListener('beforeunload', () => { pageIsUnloading = true })

// True if every field the agent predicted (expect) currently holds on the page.
// Missing/empty expect counts as met (no prediction to satisfy). This is the
// "positive signal" the agent waits for instead of mere DOM quiescence.
function expectationMet(expect) {
    if (!expect) return true
    if (expect.url_contains && !window.location.href.includes(expect.url_contains)) return false
    if (expect.selector && !findElement(expect.selector)) return false
    if (expect.text_contains && !(document.body?.innerText || '').includes(expect.text_contains)) return false
    return true
}

// Waits for the page to be safe to read after an action, resolving as soon as
// the *right* signal fires instead of paying a flat timeout. Returns
// { navigated, expectationMet }:
//   - navigated:true       -> a hard navigation began; stay silent, the new
//                             page's content_ready reports the outcome.
//   - expectationMet:bool  -> if the action carried a prediction (expect),
//                             whether it came true (true) or timed out (false).
//   - expectationMet:null  -> no prediction; we reported on DOM quiescence.
// Three strategies race, whichever is relevant:
//   1. pagehide/beforeunload -> hard nav (highest priority).
//   2. expect given -> poll until the prediction holds; if the page visibly
//      reacted (mutations) then settled and the prediction STILL isn't true
//      after minMs, it's a misprediction — report early instead of paying maxMs.
//   3. no expect -> a MutationObserver reports once edits stop for quietMs.
function awaitSettle(expect, { quietMs = 200, minMs = 1200, maxMs = 2500 } = {}) {
    if (pageIsUnloading) return Promise.resolve({ navigated: true, expectationMet: null })
    return new Promise((resolve) => {
        let resolved = false
        let observer = null, quietTimer = null, pollTimer = null, capTimer = null

        const onUnload = () => done({ navigated: true, expectationMet: null })
        function done(result) {
            if (resolved) return
            resolved = true
            clearTimeout(quietTimer); clearTimeout(pollTimer); clearTimeout(capTimer)
            if (observer) observer.disconnect()
            window.removeEventListener('pagehide', onUnload)
            window.removeEventListener('beforeunload', onUnload)
            resolve(result)
        }

        // a hard navigation always wins, in either mode
        window.addEventListener('pagehide', onUnload)
        window.addEventListener('beforeunload', onUnload)

        if (expect) {
            // wait for the PREDICTED outcome — don't report on a quiet gap that
            // an async result hasn't filled yet. Two exits for a wrong
            // prediction: early (the page mutated, went quiet for quietMs, and
            // minMs elapsed — it did SOMETHING, just not what was predicted,
            // e.g. a dropdown opened instead of navigating) or the maxMs cap
            // (nothing observable happened at all).
            const start = performance.now()
            let lastMutation = null
            observer = new MutationObserver(() => { lastMutation = performance.now() })
            observer.observe(document.documentElement, { childList: true, subtree: true, attributes: true })
            const poll = () => {
                if (expectationMet(expect)) return done({ navigated: false, expectationMet: true })
                const now = performance.now()
                if (lastMutation !== null && now - lastMutation > quietMs && now - start > minMs) {
                    return done({ navigated: false, expectationMet: false })
                }
                pollTimer = setTimeout(poll, 100)
            }
            poll()
            capTimer = setTimeout(() => done({ navigated: false, expectationMet: false }), maxMs)
        } else {
            // no prediction: report once the DOM stops mutating for quietMs.
            // observe documentElement (the whole doc) since a soft nav can swap body.
            const bump = () => {
                clearTimeout(quietTimer)
                quietTimer = setTimeout(() => done({ navigated: false, expectationMet: null }), quietMs)
            }
            observer = new MutationObserver(bump)
            observer.observe(document.documentElement, {
                childList: true, subtree: true, attributes: true, characterData: true,
            })
            bump() // start the quiet clock so a no-op action still reports promptly
            capTimer = setTimeout(() => done({ navigated: false, expectationMet: null }), maxMs)
        }
    })
}

async function handleExecuteAction(msg) {
    showPanel()
    console.log('Agent action:', msg)
    status.textContent = msg.action.description
    renderChecklist(msg.checklist)  // re-render: check-offs and mid-loop revisions

    const result = await executeAction(msg.action)
    if (!result.success) {
        console.warn('Action failed:', result.error)
        status.textContent = `⚠ ${result.error}`
    }

    // Wait for the page to settle. If the action carried a prediction (expect),
    // we wait for THAT to come true; otherwise we wait for the DOM to go quiet.
    // If it triggered a hard navigation, this page is about to unload — reporting
    // from a dying page would ship a stale snapshot that races the new page's
    // content_ready and strands the agent. So stay silent and let content_ready
    // report (where the carried-over expectation is checked on the new page).
    const settle = await awaitSettle(msg.action.expect)
    if (settle.navigated) return

    sendToBackground({
        type: 'action_executed',
        // null = executed; string = why it did NOT execute. The backend
        // feeds this to the agent so it can't believe phantom actions.
        action_result: result.success ? null : result.error,
        // true/false = whether the predicted outcome came true; null = no prediction
        expectation_met: settle.expectationMet,
        url: window.location.href,
        title: document.title,
        dom_tree: extractAccessibilityTree()
    })
}

function showPanel() {
    if (panel.style.display === 'none') {
        positionPanel()
        panel.style.display = 'block'
    }
}

// announce this page to the background worker — if a task is mid-flight in
// this tab, this is how it resumes after a navigation. Wait for the page to
// settle first so deferred-hydrated controls are in the snapshot the agent
// plans against (otherwise it perceives a half-loaded page).
extractWhenSettled().then((dom_tree) => {
    chrome.runtime.sendMessage({
        type: 'content_ready',
        url: window.location.href,
        title: document.title,
        dom_tree
    }).catch(() => {})
})

// The element the agent last successfully typed into — the natural target
// for a follow-up press_enter without a selector. document.activeElement is
// too fragile for that job: React re-renders drop focus during the backend
// round-trip, and the user may have clicked (even into our own bubble).
let lastTypedEl = null

// Executes an action and reports whether it actually ran.
// Returns { success: true } or { success: false, error: '<why>' } — the error
// is sent back to the backend so the agent KNOWS the action never happened.
async function executeAction(action) {
    const needsElement = ['click', 'type', 'highlight'].includes(action.type)
    let el = null

    if (needsElement) {
        if (!action.selector) {
            return { success: false, error: `"${action.type}" action is missing a selector` }
        }
        el = findElement(action.selector)
        if (!el) {
            return { success: false, error: `element not found for selector: ${action.selector}` }
        }
        // highlight + bring into view. Instant scroll: el.click() works
        // regardless of scroll position, so the scroll is purely so the user
        // can see what's happening — no need to pay for a smooth animation.
        el.style.outline = '3px solid #3B82F6'
        el.style.outlineOffset = '2px'
        el.scrollIntoView({ behavior: 'auto', block: 'center' })
        await sleep(100)
    }

    switch (action.type) {
        case 'click':
            // hover first: menus that open on :hover / mouseover never see a
            // bare programmatic click, so the agent could never open them
            el.dispatchEvent(new MouseEvent('pointerover', { bubbles: true }))
            el.dispatchEvent(new MouseEvent('mouseover', { bubbles: true }))
            el.dispatchEvent(new MouseEvent('mouseenter', { bubbles: false }))
            el.click()
            break
        case 'type': {
            const value = action.value ?? ''
            // a <select> can't be typed into or clicked open — picking one of
            // its options (matched by text or value) IS the type action here
            if (el.tagName === 'SELECT') {
                const opt = [...el.options].find(o => o.text.trim() === value || o.value === value)
                if (!opt) {
                    return { success: false, error: `no option matching "${value}" in ${action.selector}` }
                }
                el.value = opt.value
                el.dispatchEvent(new Event('input', { bubbles: true }))
                el.dispatchEvent(new Event('change', { bubbles: true }))
                break
            }
            el.focus()
            el.value = value
            el.dispatchEvent(new Event('input', { bubbles: true }))
            el.dispatchEvent(new Event('change', { bubbles: true }))
            // readback check: did the value actually stick? (catches
            // non-input elements and controlled inputs that reset)
            if (el.value !== value) {
                return { success: false, error: `typed text did not stick in ${action.selector}` }
            }
            lastTypedEl = el
            break
        }
        case 'press_enter': {
            // press Enter like a real user — on the given element, the input
            // the agent last typed into, or whatever currently has focus
            let target = document.activeElement
            if (action.selector) {
                target = findElement(action.selector)
                if (!target) {
                    return { success: false, error: `element not found for selector: ${action.selector}` }
                }
            }
            // never act on our own UI — if the user clicked the bubble, its
            // input is the activeElement and Enter there would start a task
            if (target && bubble.contains(target)) target = null
            // focus is fragile (React re-renders drop it during the backend
            // round-trip) — the input the agent just typed into is the target
            // it almost certainly means
            if ((!target || target === document.body) && lastTypedEl && lastTypedEl.isConnected) {
                target = lastTypedEl
            }
            if (!target || target === document.body) {
                return { success: false, error: 'press_enter has no target — provide the input\'s selector' }
            }
            target.focus()
            const opts = { key: 'Enter', code: 'Enter', keyCode: 13, which: 13, bubbles: true, cancelable: true }
            target.dispatchEvent(new KeyboardEvent('keydown', opts))
            target.dispatchEvent(new KeyboardEvent('keypress', opts))
            target.dispatchEvent(new KeyboardEvent('keyup', opts))
            // many sites submit via the surrounding form rather than the key event
            if (target.form) target.form.requestSubmit()
            break
        }
        case 'scroll':
            window.scrollBy({ top: window.innerHeight * 0.8, behavior: 'smooth' })
            break
        case 'wait':
            await sleep(1500)
            break
        case 'highlight':
            break // highlighted above
        default:
            return { success: false, error: `unsupported action type: ${action.type}` }
    }

    if (el) setTimeout(() => { el.style.outline = '' }, 800)
    return { success: true }
}

function sleep(ms) {
    return new Promise(r => setTimeout(r, ms))
}

// Keep the background service worker alive during long backend throttle pauses.
// An open port is the only reliable MV3 keepalive — setInterval can be
// suspended by Chrome before it fires and doesn't prevent SW termination.
//
// Guards three lifecycle hazards that otherwise spam the console (and can strand
// a task):
//   1. Extension reloaded/updated -> this content script is orphaned and
//      chrome.runtime is dead; connect() throws "Extension context invalidated"
//      synchronously. Catch it and STOP — nothing to keep alive until the page
//      is refreshed. (Blindly reconnecting is what threw the uncaught error.)
//   2. Back/forward cache -> Chrome force-closes ports on frozen pages. We drop
//      the port on pagehide and restore it on pageshow so Chrome never has to
//      sever it, and read lastError so a severed port isn't logged "unchecked".
//   3. Service worker cycled normally -> reconnect after a short delay.
// Note: a merely backgrounded tab does NOT fire pagehide, so its keepalive is
// left intact — a task started then switched away from keeps running.
let keepAlivePort = null

// chrome.runtime.id is undefined once the extension context is invalidated —
// the cheapest reliable "are we still a live extension?" check.
function extensionAlive() {
    try { return !!chrome.runtime?.id } catch { return false }
}

function connectKeepAlive() {
    if (keepAlivePort || !extensionAlive()) return
    try {
        keepAlivePort = chrome.runtime.connect({ name: 'keepalive' })
    } catch {
        keepAlivePort = null   // context invalidated — give up (page needs a refresh)
        return
    }
    keepAlivePort.onDisconnect.addListener(() => {
        try { void chrome.runtime.lastError } catch {}  // consume so it isn't logged "unchecked"
        keepAlivePort = null
        if (extensionAlive()) setTimeout(connectKeepAlive, 1000)
    })
}

// bfcache: release the port before the page freezes, restore it when it returns,
// so Chrome never force-closes a port on a cached page.
window.addEventListener('pagehide', () => {
    if (keepAlivePort) {
        try { keepAlivePort.disconnect() } catch {}
        keepAlivePort = null
    }
})
window.addEventListener('pageshow', connectKeepAlive)

connectKeepAlive()