// The Voice Hub (voice.html): the single wake-word listener, running in a
// pinned top-level extension tab. Both getUserMedia and SpeechRecognition
// here answer to the EXTENSION's permission — no site Permissions-Policy,
// no per-site prompts, no contention. The background opens/closes this tab
// from the user's toggle and pauses listening when Chrome loses focus.
// The one-time mic grant button also lives here: if permission is missing,
// the hub shows it inline instead of popping up setup tabs.

const WAKE_RE = /\bhey,?\s*helper\b/i
const STOP_RE = /\b(?:thank\s*you|thanks),?\s*helper\b/i

let wanted = false          // background's last listen/pause command
let blocked = false         // mic permission missing — grant button shown
let rec = null              // active SpeechRecognition instance
let micStream = null        // held while listening; keeps the capture alive
let starting = false        // start() is async — guard reentry
let restartTimer = null
let restartDelay = 400      // grows to 3s after a speech-service network error
let lastPhraseAt = 0        // debounce: interim results repeat the same phrase

const dot = document.getElementById('dot')
const stateText = document.getElementById('stateText')
const detail = document.getElementById('detail')
const enableBtn = document.getElementById('enable')

function setUI(state) {
    dot.classList.toggle('live', state === 'listening')
    enableBtn.style.display = state === 'needs-grant' ? 'inline-block' : 'none'
    if (state === 'listening') {
        stateText.textContent = 'Listening for "Hey Helper"'
        detail.textContent = 'Say "Hey Helper", then tell your helper what you need. Say "Thank you, Helper" to stop it.'
    } else if (state === 'paused') {
        stateText.textContent = 'Paused'
        detail.textContent = 'Listening resumes when you come back to Chrome.'
    } else if (state === 'needs-grant') {
        stateText.textContent = 'Voice needs one permission'
        detail.textContent = 'Click the button, then choose Allow. You only do this once — it works on every website after.'
    } else {
        stateText.textContent = 'Voice is off'
        detail.textContent = 'Turn on "Hey Helper" from the 🤖 helper on any page.'
    }
}

function send(type, extra = {}) {
    chrome.runtime.sendMessage({ target: 'background', type, ...extra }).catch(() => {})
}

chrome.runtime.onMessage.addListener((msg) => {
    if (msg.target !== 'voice-hub') return
    if (msg.type === 'wake_listen') {
        wanted = !!msg.on
        if (wanted) start()
        else { stop(); setUI(msg.off ? 'off' : 'paused') }
    } else if (msg.type === 'hub_notice') {
        // the background couldn't act on the user's command (e.g. the active
        // tab is a chrome:// page) — this tab is the one place left to say so
        detail.textContent = msg.text
    }
})

enableBtn.addEventListener('click', async () => {
    try {
        const s = await navigator.mediaDevices.getUserMedia({ audio: true })
        s.getTracks().forEach(t => t.stop())
        blocked = false
        send('mic_permission_granted')
        if (wanted) start()
        else setUI('off')
    } catch {
        detail.textContent = 'The microphone was blocked. Click the mic icon in the address bar, choose Allow, then try again.'
    }
})

async function start() {
    if (!wanted || blocked || rec || starting) return
    const SR = self.SpeechRecognition || self.webkitSpeechRecognition
    if (!SR) {
        stateText.textContent = 'Voice is not supported in this browser'
        return
    }
    // hold the mic while listening — proves the grant and keeps this pinned
    // tab exempt from background-tab freezing (Chrome never freezes a
    // capturing tab), so the listener survives being out of sight
    if (!micStream) {
        starting = true
        try {
            micStream = await navigator.mediaDevices.getUserMedia({ audio: true })
        } catch {
            blocked = true
            setUI('needs-grant')
            return
        } finally {
            starting = false
        }
        if (!wanted || rec) { releaseMic(); return }  // world changed while awaiting
    }
    const r = new SR()
    rec = r
    r.continuous = true
    r.interimResults = true
    r.lang = 'en-US'

    r.onstart = () => {
        console.debug('a11y-agent hub: listening')
        setUI('listening')
    }
    r.onresult = (e) => {
        const now = Date.now()
        if (now - lastPhraseAt < 2000) return
        for (let i = e.resultIndex; i < e.results.length; i++) {
            const heard = e.results[i][0].transcript
            console.debug('a11y-agent hub: heard', JSON.stringify(heard))
            if (STOP_RE.test(heard)) {
                lastPhraseAt = now
                send('stop_phrase_heard')
                return
            }
            if (WAKE_RE.test(heard)) {
                lastPhraseAt = now
                stop()               // hand the mic to the dictation pipeline
                setUI('paused')
                send('wake_heard')
                return
            }
        }
    }
    r.onerror = (e) => {
        console.debug('a11y-agent hub: error', e.error)
        if (e.error === 'not-allowed' || e.error === 'service-not-allowed') {
            blocked = true
            setUI('needs-grant')
        } else if (e.error === 'network') {
            restartDelay = 3000  // speech service rate-limits rapid restarts
        }
    }
    r.onend = () => {
        if (rec === r) rec = null
        // Chrome ends continuous recognition on silence gaps — restart quietly
        if (wanted && !blocked) {
            clearTimeout(restartTimer)
            restartTimer = setTimeout(start, restartDelay)
            restartDelay = 400
        }
    }
    try {
        r.start()
    } catch {
        rec = null
        clearTimeout(restartTimer)
        restartTimer = setTimeout(start, 600)
    }
}

function releaseMic() {
    if (micStream) {
        micStream.getTracks().forEach(t => t.stop())
        micStream = null
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
    releaseMic()  // free the mic for dictation
}

setUI('off')
// announce so the background can command the fresh hub
send('voice_hub_ready')
