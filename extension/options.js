// Loads the saved profile into the form and writes edits back to
// chrome.storage.local. Field ids here match the UserProfile keys the backend
// expects (see backend/agent/schemas.py).

const FIELDS = ['fullName', 'email', 'phone', 'street', 'city', 'state', 'zip', 'country', 'notes']
const statusEl = document.getElementById('status')
const emailHint = document.getElementById('emailHint')
const useProfileEl = document.getElementById('useProfile')

// Populate the form from storage on open.
chrome.storage.local.get(['userProfile', 'useProfile']).then(({ userProfile, useProfile }) => {
    const p = userProfile || {}
    for (const id of FIELDS) {
        const el = document.getElementById(id)
        if (el) el.value = p[id] || ''
    }
    // default the toggle ON when it has never been set
    useProfileEl.checked = useProfile !== false
}).catch(() => {})

// Non-blocking email hint — never prevents saving.
function updateEmailHint() {
    const v = document.getElementById('email').value.trim()
    emailHint.textContent = (v && !/^[^@\s]+@[^@\s]+\.[^@\s]+$/.test(v))
        ? "That doesn't look like an email — saved anyway."
        : ''
}
document.getElementById('email').addEventListener('input', updateEmailHint)

document.getElementById('save').addEventListener('click', () => {
    const profile = {}
    for (const id of FIELDS) {
        const el = document.getElementById(id)
        const v = (el?.value || '').trim()
        if (v) profile[id] = v   // store only non-empty fields
    }
    updateEmailHint()
    chrome.storage.local.set({ userProfile: profile, useProfile: useProfileEl.checked })
        .then(() => {
            statusEl.textContent = 'Saved ✓'
            setTimeout(() => { statusEl.textContent = '' }, 2000)
        })
        .catch(() => { statusEl.textContent = 'Could not save' })
})
