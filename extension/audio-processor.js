class PCMProcessor extends AudioWorkletProcessor {
    process(inputs) {
        const input = inputs[0][0]
        if (!input) return true
        
        // convert Float32 [-1, 1] to Int16
        const pcm16 = new Int16Array(input.length)
        for (let i = 0; i < input.length; i++) {
            const s = Math.max(-1, Math.min(1, input[i]))
            pcm16[i] = s < 0 ? s * 0x8000 : s * 0x7FFF
        }
        
        this.port.postMessage(pcm16.buffer)
        return true
    }
}
registerProcessor('pcm-processor', PCMProcessor)