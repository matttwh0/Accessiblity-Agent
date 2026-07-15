const result = document.getElementById('result')

document.getElementById('enable').addEventListener('click', async () => {
    try {
        const stream = await navigator.mediaDevices.getUserMedia({ audio: true })
        // permission is what we wanted — release the mic immediately
        stream.getTracks().forEach(t => t.stop())
        result.textContent = '✅ Voice is ready! This tab will close itself.'
        chrome.runtime.sendMessage({ target: 'background', type: 'mic_permission_granted' }).catch(() => {})
        setTimeout(() => window.close(), 1500)
    } catch {
        result.textContent =
            '❌ The microphone was blocked. Click the camera/mic icon in the ' +
            'address bar, choose Allow, then press the button again.'
    }
})
