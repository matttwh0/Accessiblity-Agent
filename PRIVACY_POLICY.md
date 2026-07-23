# Privacy Policy — Accessibility Agent

**Last updated:** July 23, 2026

Accessibility Agent ("the extension") is a Chrome extension that helps you complete tasks on websites using voice or text instructions. This policy explains what data the extension collects, how it's used, and who it's shared with.

## What data we collect

**Page content.** When you give the extension a task, it reads the current page's URL, title, and an accessibility-tree representation of the visible elements (buttons, links, form fields, and similar). This lets the agent understand what's on the page and decide what to click, type, or navigate to.

**Voice audio.** If you use voice input ("Hey Helper" or the microphone button), your microphone audio is streamed to our backend in real time for transcription. Audio is not saved — it's discarded as soon as transcription completes.

**Your task instructions.** Whatever you type or speak as a request is sent to our backend so the agent can act on it.

**Profile info (optional).** If you fill out "My info" (name, email, phone, address, notes), it's stored locally in your browser (`chrome.storage.local`) and is only sent to the backend while an active task is running, so the agent can autofill matching form fields on your behalf. You can turn this off at any time with the "Let my helper use this info" toggle, or clear the fields entirely. This data never leaves your device unless a task is in progress, and our backend never writes profile field values to its logs.

**Narration text.** When the agent speaks its progress aloud, the text of that narration is sent to our text-to-speech provider to generate audio.

## How your data is used

Your page content, task instructions, and (if enabled) profile info are sent to our backend server, which forwards them to the following third-party AI providers to carry out your request:

- **Anthropic (Claude)** — decides what actions to take based on the page and your task.
- **AssemblyAI** — transcribes your voice audio to text.
- **Inworld AI** — converts the agent's narration text to spoken audio.

Each provider processes this data under its own privacy policy:
- Anthropic: https://www.anthropic.com/privacy
- AssemblyAI: https://www.assemblyai.com/legal/privacy-policy
- Inworld AI: https://inworld.ai/privacy

Our backend is hosted on Railway (https://railway.com/legal/privacy).

## Data retention

Our backend does not use a database. Page content, task text, and audio exist only in memory for the duration of the WebSocket connection handling your task, and are discarded when the task ends or the connection closes. We do not sell your data or use it for advertising.

## Permissions we request, and why

- **Host permissions (all sites)** — needed so the extension can read page content and perform actions on any site you ask it to help with.
- **Microphone** — needed for voice input and dictation; only active when you start a voice interaction.
- **Storage** — used to save your optional profile info and preferences locally in your browser.
- **Tabs / scripting / offscreen / text-to-speech** — used to inject the assistant UI, run tasks in the correct tab, and play spoken narration.

## Your controls

- You can edit or delete your saved profile info at any time from the extension's "My info" page.
- You can disable profile autofill with the "Let my helper use this info" toggle without deleting the data.
- Uninstalling the extension removes all locally stored data (profile, preferences).

## Children's privacy

This extension is not directed at children under 13, and we do not knowingly collect data from them.

## Changes to this policy

If this policy changes, we'll update the "Last updated" date above.

## Contact

Questions about this policy: matttran2004@gmail.com
