// Runs inside the extension's offscreen document. One job: command
// dictation — getUserMedia -> 16kHz PCM (AudioWorklet) -> the backend's
// /transcribe WebSocket. getUserMedia works here with the extension's
// one-time mic grant (that's the documented USER_MEDIA offscreen pattern).
// NOTE: wake-word listening does NOT live here — Chrome refuses to run
// SpeechRecognition in offscreen documents regardless of permission, so the
// wake listener lives in an extension iframe per tab (wake.js), elected by
// the background so only the active tab's frame listens.

let dictation = null  // { ws, ctx, stream, source, node } while recording

function send(type, extra = {}) {
    chrome.runtime.sendMessage({ target: 'background', type, ...extra }).catch(() => {})
}

chrome.runtime.onMessage.addListener((msg) => {
    if (msg.target !== 'offscreen') return
    if (msg.type === 'offscreen_dictate_start') startDictation()
    else if (msg.type === 'offscreen_dictate_stop') stopDictation()
})

async function startDictation() {
    if (dictation) return
    let stream
    try {
        stream = await navigator.mediaDevices.getUserMedia({ audio: true })
    } catch {
        send('mic_permission_needed', { source: 'dictation' })
        return
    }

    const ws = new WebSocket('ws://localhost:8000/transcribe')
    ws.onmessage = (e) => {
        let m
        try { m = JSON.parse(e.data) } catch { return }
        if (m.type === 'transcript') {
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
        }
    }
    source.connect(node)
    node.connect(ctx.destination)  // worklet emits no output samples — silent

    dictation = { ws, ctx, stream, source, node }
    send('dictation_started')
}

function stopDictation() {
    if (!dictation) return
    const { ws, ctx, stream, source, node } = dictation
    dictation = null
    try { node.disconnect() } catch {}
    try { source.disconnect() } catch {}
    stream.getTracks().forEach(t => t.stop())
    ctx.close().catch(() => {})
    try { ws.close() } catch {}
    send('dictation_ended')
}
