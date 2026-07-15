// Runs inside the invisible extension iframe (wake.html) that content.js
// embeds in every page. Extension origin -> covered by the extension's
// one-time mic grant, no per-site prompts. SpeechRecognition cannot run in
// the offscreen document (Chrome refuses regardless of permission), so THIS
// frame is where wake-word listening lives. The background elects exactly
// one frame — the active tab's — to listen at a time, so recognizers never
// fight over Chrome's single recognition session.

const WAKE_RE = /\bhey,?\s*helper\b/i
const STOP_RE = /\b(?:thank\s*you|thanks),?\s*helper\b/i

let wanted = false          // background's last listen/pause command
let blocked = false         // mic permission missing — don't retry-loop
let rec = null              // active SpeechRecognition instance
let restartTimer = null
let restartDelay = 400      // grows to 3s after a speech-service network error
let lastPhraseAt = 0        // debounce: interim results repeat the same phrase

function send(type, extra = {}) {
    chrome.runtime.sendMessage({ target: 'background', type, ...extra }).catch(() => {})
}

chrome.runtime.onMessage.addListener((msg) => {
    if (msg.target !== 'wake-frame') return
    if (msg.type === 'wake_listen') {
        wanted = !!msg.on
        if (wanted) {
            // an explicit listen-on retries even after a permission failure —
            // the background's session latch stops setup-tab spam if it fails
            blocked = false
            start()
        } else {
            stop()
        }
    }
})

function start() {
    if (!wanted || blocked || rec) return
    if (document.hidden) return  // stale command for a backgrounded tab
    const SR = self.SpeechRecognition || self.webkitSpeechRecognition
    if (!SR) return
    const r = new SR()
    rec = r
    r.continuous = true
    r.interimResults = true
    r.lang = 'en-US'

    r.onstart = () => console.debug('a11y-agent wake-frame: listening')
    r.onresult = (e) => {
        const now = Date.now()
        if (now - lastPhraseAt < 2000) return
        for (let i = e.resultIndex; i < e.results.length; i++) {
            const heard = e.results[i][0].transcript
            console.debug('a11y-agent wake-frame: heard', JSON.stringify(heard))
            if (STOP_RE.test(heard)) {
                lastPhraseAt = now
                send('stop_phrase_heard')
                return
            }
            if (WAKE_RE.test(heard)) {
                lastPhraseAt = now
                stop()               // hand the mic to the dictation pipeline
                send('wake_heard')
                return
            }
        }
    }
    r.onerror = (e) => {
        console.debug('a11y-agent wake-frame: error', e.error)
        if (e.error === 'not-allowed' || e.error === 'service-not-allowed') {
            blocked = true
            send('mic_permission_needed', { source: 'wake' })
        } else if (e.error === 'network') {
            restartDelay = 3000  // speech service rate-limits rapid restarts
        }
    }
    r.onend = () => {
        if (rec === r) rec = null
        // Chrome ends continuous recognition on silence gaps — restart quietly
        if (wanted && !blocked && !document.hidden) {
            clearTimeout(restartTimer)
            restartTimer = setTimeout(start, restartDelay)
            restartDelay = 400
        }
    }
    try {
        r.start()
    } catch {
        // previous instance still releasing — retry, never die silently
        rec = null
        clearTimeout(restartTimer)
        restartTimer = setTimeout(start, 600)
    }
}

function stop() {
    clearTimeout(restartTimer)
    const r = rec
    rec = null
    if (r) {
        r.onend = null    // deliberate stop — no auto-restart
        r.onerror = null  // …and no spurious 'aborted' noise
        try { r.stop() } catch {}
    }
}

// a hidden tab must never hold the mic; the election re-arms this frame
// when its tab becomes active again
document.addEventListener('visibilitychange', () => {
    if (document.hidden) stop()
    else if (wanted) start()
})

// announce so the background can include this frame in the election
send('wake_frame_ready')
