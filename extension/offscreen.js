// Runs inside the extension's offscreen document. Two jobs:
// 1. Command dictation — getUserMedia -> 16kHz PCM (AudioWorklet) -> the
//    backend's /transcribe WebSocket. getUserMedia works here with the
//    extension's one-time mic grant (the documented USER_MEDIA pattern).
// 2. Spoken narration — fetch each utterance's audio from the backend's
//    /tts proxy (Inworld.ai neural TTS) and play it; service workers can't
//    play audio, so this document is where the agent's voice lives.
// NOTE: wake-word listening does NOT live here — Chrome refuses to run
// SpeechRecognition in offscreen documents regardless of permission, so the
// wake listener lives in the pinned Voice Hub tab (voice.html/voice.js),
// the one context where both mic APIs answer to the extension alone.

let dictation = null  // { ws, ctx, stream, source, node } while recording

function send(type, extra = {}) {
    chrome.runtime.sendMessage({ target: 'background', type, ...extra }).catch(() => {})
}

chrome.runtime.onMessage.addListener((msg) => {
    if (msg.target !== 'offscreen') return
    if (msg.type === 'offscreen_dictate_start') startDictation()
    else if (msg.type === 'offscreen_dictate_stop') stopDictation()
    else if (msg.type === 'offscreen_speak') speak(msg.text)
    else if (msg.type === 'offscreen_speak_stop') stopSpeaking()
})

// --- narration playback ----------------------------------------------------
// One utterance at a time: a new speak (or a stop) supersedes whatever is
// fetching or playing, so the voice never lags behind the page. The
// generation counter makes a superseded fetch's late arrival a no-op.

let speakGen = 0        // bumped by every speak/stop; stale work checks it
let speakAudio = null   // the <audio> currently playing
let speakAbort = null   // aborts the in-flight /tts fetch

function stopSpeaking() {
    speakGen++
    if (speakAbort) { speakAbort.abort(); speakAbort = null }
    if (speakAudio) {
        try { speakAudio.pause() } catch {}
        speakAudio = null
    }
}

async function speak(text) {
    if (!text) return
    stopSpeaking()
    const gen = speakGen
    const ctrl = new AbortController()
    speakAbort = ctrl
    try {
        const resp = await fetch('https://accessiblity-agent-production.up.railway.app/tts', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ text }),
            signal: ctrl.signal,
        })
        if (!resp.ok) throw new Error(`tts http ${resp.status}`)
        const buf = await resp.arrayBuffer()
        if (gen !== speakGen) return  // superseded while fetching
        const url = URL.createObjectURL(new Blob([buf], { type: 'audio/mpeg' }))
        const audio = new Audio(url)
        speakAudio = audio
        audio.onended = audio.onerror = () => {
            URL.revokeObjectURL(url)
            if (speakAudio === audio) speakAudio = null
        }
        await audio.play()
    } catch {
        if (gen !== speakGen) return  // deliberately interrupted — not a failure
        // backend/Inworld unavailable: hand the text back so the background
        // can speak it with chrome.tts instead of dropping it silently
        send('tts_fallback', { text })
    }
}

async function startDictation() {
    if (dictation) return
    let stream
    try {
        stream = await navigator.mediaDevices.getUserMedia({ audio: true })
    } catch {
        send('mic_permission_needed', { source: 'dictation' })
        return
    }

    const ws = new WebSocket('wss://accessiblity-agent-production.up.railway.app/transcribe')
    // audio captured while the socket is still connecting — flushed on open so
    // the user's first words aren't dropped during the connection handshake
    let preOpenQueue = []
    ws.onopen = () => {
        for (const b of preOpenQueue) ws.send(b)
        preOpenQueue = null
    }
    ws.onmessage = (e) => {
        let m
        try { m = JSON.parse(e.data) } catch { return }
        if (m.type === 'transcript') {
            if (dictation && dictation.noSpeechTimer) {
                clearTimeout(dictation.noSpeechTimer)
                dictation.noSpeechTimer = null
            }
            send('dictation_transcript', { text: m.text, is_final: m.is_final })
        } else if (m.type === 'transcribe_error') {
            send('dictation_error', { error: m.error })
            stopDictation()
        }
    }
    ws.onerror = () => {
        send('dictation_error', { error: 'Transcription connection failed — is the backend running?' })
        stopDictation()
    }
    ws.onclose = () => {
        if (dictation && dictation.ws === ws) stopDictation()
    }

    // 16kHz mono is what the backend's AssemblyAI session expects
    const ctx = new AudioContext({ sampleRate: 16000 })
    const source = ctx.createMediaStreamSource(stream)
    // extension-origin document: the worklet module always loads (no page CSP)
    await ctx.audioWorklet.addModule('audio-processor.js')
    const node = new AudioWorkletNode(ctx, 'pcm-processor')

    // batch ~100ms of audio per send (the worklet emits 8ms blocks)
    let pending = [], pendingSamples = 0
    node.port.onmessage = (e) => {
        const int16 = new Int16Array(e.data)
        pending.push(int16)
        pendingSamples += int16.length
        if (pendingSamples >= 1600) {  // 1600 samples @16kHz = 100ms
            const all = new Int16Array(pendingSamples)
            let off = 0
            for (const c of pending) { all.set(c, off); off += c.length }
            pending = []; pendingSamples = 0
            if (ws.readyState === WebSocket.OPEN) ws.send(all.buffer)
            else if (ws.readyState === WebSocket.CONNECTING && preOpenQueue) preOpenQueue.push(all.buffer)
        }
    }
    source.connect(node)
    node.connect(ctx.destination)  // worklet emits no output samples — silent

    // Without this, an unheard command leaves the mic recording forever with
    // no feedback — the exact "it isn't listening to me" dead end. If the
    // service produces no transcript at all in 12s, say so and reset.
    const noSpeechTimer = setTimeout(() => {
        send('dictation_error', { error: 'I didn\'t catch anything — say "Hey Helper" and then your request' })
        stopDictation()
    }, 12000)

    dictation = { ws, ctx, stream, source, node, noSpeechTimer }
    send('dictation_started')
}

function stopDictation() {
    if (!dictation) return
    const { ws, ctx, stream, source, node, noSpeechTimer } = dictation
    if (noSpeechTimer) clearTimeout(noSpeechTimer)
    dictation = null
    try { node.disconnect() } catch {}
    try { source.disconnect() } catch {}
    stream.getTracks().forEach(t => t.stop())
    ctx.close().catch(() => {})
    try { ws.close() } catch {}
    send('dictation_ended')
}
