# Inworld.ai text-to-speech. The extension can't hold the API key (anyone can
# read an extension's source), so the backend proxies synthesis: the extension
# POSTs text to /tts and gets MP3 bytes back. The key in .env is already
# base64-encoded the way Inworld issues it, so it drops straight into a
# Basic authorization header.
import base64
import logging
import os

import httpx

logger = logging.getLogger("agent.inworld")

TTS_URL = "https://api.inworld.ai/tts/v1/voice"
# A warm, clear default voice for an older, non-technical audience.
# Override in .env if another Inworld voice suits better.
VOICE_ID = os.getenv("INWORLD_VOICE_ID", "Sarah")
MODEL_ID = os.getenv("INWORLD_MODEL_ID", "inworld-tts-2")
# Slightly slower than natural pace so an older listener can follow each step.
SPEAKING_RATE = float(os.getenv("INWORLD_SPEAKING_RATE", "0.8"))
# BALANCED keeps delivery even and clear rather than expressive/dramatic.
DELIVERY_PRESET = os.getenv("INWORLD_DELIVERY_PRESET", "BALANCED")
# AUTO lets Inworld detect the language of each utterance.
LANGUAGE = os.getenv("INWORLD_LANGUAGE", "AUTO")


class TTSNotConfigured(RuntimeError):
    """INWORLD_API_KEY is missing — synthesis can't even be attempted."""


class TTSFailed(RuntimeError):
    """The Inworld API call was attempted but didn't yield audio."""


_client: httpx.AsyncClient | None = None


def _http() -> httpx.AsyncClient:
    # one shared client: keeps the TLS connection to Inworld warm across the
    # many short utterances a single task produces
    global _client
    if _client is None:
        _client = httpx.AsyncClient(timeout=15.0)
    return _client


async def synthesize(text: str) -> bytes:
    """Turn one utterance into MP3 bytes via Inworld's TTS API."""
    api_key = os.getenv("INWORLD_API_KEY")
    if not api_key:
        raise TTSNotConfigured("INWORLD_API_KEY not set in backend/.env")
    payload = {
        "text": text,
        "voiceId": VOICE_ID,
        "modelId": MODEL_ID,
        "language": LANGUAGE,
        "deliveryPreset": DELIVERY_PRESET,
        # MP3 keeps responses small and plays directly in an <audio> element;
        # the default LINEAR16 would be ~10x the bytes per utterance.
        # speakingRate < 1.0 slows delivery for an older audience.
        "audioConfig": {
            "audioEncoding": "MP3",
            "speakingRate": SPEAKING_RATE,
        },
    }
    try:
        resp = await _http().post(
            TTS_URL,
            json=payload,
            headers={"Authorization": f"Basic {api_key}"},
        )
        resp.raise_for_status()
        data = resp.json()
    except httpx.HTTPStatusError as e:
        # include the body — Inworld's error messages name the actual problem
        # (bad voice id, quota, malformed key) far better than the status code
        raise TTSFailed(f"Inworld TTS HTTP {e.response.status_code}: "
                        f"{e.response.text[:200]}") from e
    except httpx.HTTPError as e:
        raise TTSFailed(f"Inworld TTS request failed: {e}") from e
    # non-streaming responses carry audioContent at the top level; tolerate the
    # streaming-style {"result": {...}} envelope too
    audio_b64 = data.get("audioContent") or (data.get("result") or {}).get("audioContent")
    if not audio_b64:
        raise TTSFailed("Inworld TTS response had no audioContent")
    return base64.b64decode(audio_b64)
