// create the floating bubble
const bubble = document.createElement('div')
bubble.id = 'a11y-agent-bubble'
bubble.innerHTML = `
  <div id="a11y-agent-icon">🤖</div>
  <div id="a11y-agent-panel" style="display:none">
    <input id="a11y-agent-input" placeholder="What do you need help with?" />
    <div id="a11y-agent-checklist" style="display:none"></div>
    <div id="a11y-agent-status"></div>
  </div>
`
document.body.appendChild(bubble)

const icon = document.getElementById('a11y-agent-icon')
const panel = document.getElementById('a11y-agent-panel')
const input = document.getElementById('a11y-agent-input')
const checklist = document.getElementById('a11y-agent-checklist')
const status = document.getElementById('a11y-agent-status')

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
    panel.style.display = panel.style.display === 'none' ? 'block' : 'none'
    if (panel.style.display === 'block') input.focus()
})

input.addEventListener('keydown', async (e) => {
    if (e.key !== 'Enter') return
    const task = input.value.trim()
    if (!task) return
    
    status.textContent = 'Thinking...'
    renderChecklist('')  // clear any previous task's plan
    sendToBackground({
        type: 'start_task',
        task,
        url: window.location.href,
        title: document.title,
        dom_tree: extractAccessibilityTree()
    })
})

function generateSelector(el) {
    if (el.id) return `[id="${el.id.replace(/"/g, '\\"')}"]`
    if (el.getAttribute('data-testid')) return `[data-testid="${el.getAttribute('data-testid')}"]`
    
    // use text content for unique anchors/buttons
    const text = (el.innerText || '').trim().slice(0, 30)
    if (text && (el.tagName === 'A' || el.tagName === 'BUTTON')) {
        return `${el.tagName.toLowerCase()}:has-text("${text}")`
    }
    
    // fallback: tag + nth-of-type
    const parent = el.parentElement
    if (parent) {
        const siblings = [...parent.children].filter(c => c.tagName === el.tagName)
        const i = siblings.indexOf(el)
        return `${el.tagName.toLowerCase()}:nth-of-type(${i + 1})`
    }
    return el.tagName.toLowerCase()
}

function extractAccessibilityTree() {
    const selectors = 'button, a, input, select, textarea, [role], h1, h2, h3, label'
    return [...document.querySelectorAll(selectors)]
        .filter(el => {
            const r = el.getBoundingClientRect()
            return r.width > 0 && r.height > 0
        })
        .map(el => ({
            tag: el.tagName.toLowerCase(),
            text: (el.innerText || '').slice(0, 100),
            label: el.ariaLabel || el.placeholder || el.title || null,
            role: el.getAttribute('role'),
            selector: generateSelector(el),
            visible: true,
            // current input value so the backend can see typing took effect
            value: typeof el.value === 'string' ? el.value.slice(0, 100) : null
        }))
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
    if (msg.type === 'execute_action') {
        handleExecuteAction(msg)
    } else if (msg.type === 'agent_update') {
        showPanel()
        if (msg.description) status.textContent = msg.description
        if (msg.checklist !== undefined) renderChecklist(msg.checklist)
    } else if (msg.type === 'collect_context') {
        // background needs the current page state (e.g. after a failed
        // browser action) — answer synchronously
        sendResponse({
            url: window.location.href,
            title: document.title,
            dom_tree: extractAccessibilityTree()
        })
    }
})

// Tracks whether this page has begun unloading (a navigation). Set once and
// read by settleOrNavigate so an action that navigates stays silent.
let pageIsUnloading = false
window.addEventListener('pagehide', () => { pageIsUnloading = true })
window.addEventListener('beforeunload', () => { pageIsUnloading = true })

// Resolves true if the page starts navigating away within `timeout` ms — in
// which case the caller must stay silent and let the new page's content_ready
// report the outcome. Resolves false if the page stayed put, meaning the
// action ran in place and its result should be reported normally.
function settleOrNavigate(timeout) {
    if (pageIsUnloading) return Promise.resolve(true)
    return new Promise((resolve) => {
        const onUnload = () => finish(true)
        function finish(navigated) {
            clearTimeout(timer)
            window.removeEventListener('pagehide', onUnload)
            window.removeEventListener('beforeunload', onUnload)
            resolve(navigated)
        }
        const timer = setTimeout(() => finish(false), timeout)
        window.addEventListener('pagehide', onUnload)
        window.addEventListener('beforeunload', onUnload)
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

    // Give the action a beat to settle. If it triggered a navigation (e.g. a
    // clicked hyperlink), this page is about to unload — reporting from a dying
    // page would ship a stale snapshot that races the new page's content_ready
    // and strands the agent. So stay silent and let content_ready report, the
    // same reliable channel the navigate/new_tab browser actions already use.
    const navigated = await settleOrNavigate(1500)
    if (navigated) return

    sendToBackground({
        type: 'action_executed',
        // null = executed; string = why it did NOT execute. The backend
        // feeds this to the agent so it can't believe phantom actions.
        action_result: result.success ? null : result.error,
        url: window.location.href,
        title: document.title,
        dom_tree: extractAccessibilityTree()
    })
}

function showPanel() {
    if (panel.style.display === 'none') {
        panel.style.display = 'block'
    }
}

// announce this page to the background worker — if a task is mid-flight in
// this tab, this is how it resumes after a navigation
chrome.runtime.sendMessage({
    type: 'content_ready',
    url: window.location.href,
    title: document.title,
    dom_tree: extractAccessibilityTree()
}).catch(() => {})

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
        // highlight + bring into view
        el.style.outline = '3px solid #3B82F6'
        el.style.outlineOffset = '2px'
        el.scrollIntoView({ behavior: 'smooth', block: 'center' })
        await sleep(800)
    }

    switch (action.type) {
        case 'click':
            el.click()
            break
        case 'type': {
            const value = action.value ?? ''
            el.focus()
            el.value = value
            el.dispatchEvent(new Event('input', { bubbles: true }))
            el.dispatchEvent(new Event('change', { bubbles: true }))
            // readback check: did the value actually stick? (catches
            // non-input elements and controlled inputs that reset)
            if (el.value !== value) {
                return { success: false, error: `typed text did not stick in ${action.selector}` }
            }
            break
        }
        case 'press_enter': {
            // press Enter like a real user — on the given element, or
            // whatever currently has focus (e.g. the input just typed in)
            let target = document.activeElement
            if (action.selector) {
                target = findElement(action.selector)
                if (!target) {
                    return { success: false, error: `element not found for selector: ${action.selector}` }
                }
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