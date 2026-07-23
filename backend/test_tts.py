# Tests for the Inworld TTS proxy: the /tts endpoint's status contract (the
# extension falls back to chrome.tts on any non-2xx) and the Inworld client's
# request/response handling.
import asyncio
import base64

import pytest
from fastapi.testclient import TestClient

import clients.inworld as inworld
from clients.inworld import TTSFailed, TTSNotConfigured, synthesize
import main
from main import app

client = TestClient(app)

FAKE_MP3 = b"ID3fake-mp3-bytes"


# --- /tts endpoint ---------------------------------------------------------

def test_tts_returns_audio(monkeypatch):
    async def fake_synthesize(text):
        assert text == "Hello there"
        return FAKE_MP3
    monkeypatch.setattr(main, "synthesize", fake_synthesize)
    resp = client.post("/tts", json={"text": "Hello there"})
    assert resp.status_code == 200
    assert resp.headers["content-type"].startswith("audio/mpeg")
    assert resp.content == FAKE_MP3


def test_tts_empty_text_is_400():
    resp = client.post("/tts", json={"text": "   "})
    assert resp.status_code == 400


def test_tts_unconfigured_is_503(monkeypatch):
    async def fake_synthesize(text):
        raise TTSNotConfigured("INWORLD_API_KEY not set")
    monkeypatch.setattr(main, "synthesize", fake_synthesize)
    resp = client.post("/tts", json={"text": "hi"})
    assert resp.status_code == 503


def test_tts_upstream_failure_is_502(monkeypatch):
    async def fake_synthesize(text):
        raise TTSFailed("Inworld TTS HTTP 500")
    monkeypatch.setattr(main, "synthesize", fake_synthesize)
    resp = client.post("/tts", json={"text": "hi"})
    assert resp.status_code == 502


# --- Inworld client --------------------------------------------------------

class FakeResponse:
    def __init__(self, data):
        self._data = data

    def raise_for_status(self):
        pass

    def json(self):
        return self._data


class FakeClient:
    def __init__(self, data):
        self._data = data
        self.calls = []

    async def post(self, url, json=None, headers=None):
        self.calls.append({"url": url, "json": json, "headers": headers})
        return FakeResponse(self._data)


def test_synthesize_builds_basic_auth_request(monkeypatch):
    monkeypatch.setenv("INWORLD_API_KEY", "dGVzdC1rZXk=")
    fake = FakeClient({"audioContent": base64.b64encode(FAKE_MP3).decode()})
    monkeypatch.setattr(inworld, "_http", lambda: fake)

    audio = asyncio.run(synthesize("Hello"))
    assert audio == FAKE_MP3

    call = fake.calls[0]
    assert call["url"] == inworld.TTS_URL
    assert call["headers"]["Authorization"] == "Basic dGVzdC1rZXk="
    assert call["json"]["text"] == "Hello"
    assert call["json"]["voiceId"] == inworld.VOICE_ID
    assert call["json"]["modelId"] == inworld.MODEL_ID
    assert call["json"]["audioConfig"]["audioEncoding"] == "MP3"


def test_synthesize_accepts_result_envelope(monkeypatch):
    monkeypatch.setenv("INWORLD_API_KEY", "k")
    fake = FakeClient({"result": {"audioContent": base64.b64encode(FAKE_MP3).decode()}})
    monkeypatch.setattr(inworld, "_http", lambda: fake)
    assert asyncio.run(synthesize("Hello")) == FAKE_MP3


def test_synthesize_without_key_raises(monkeypatch):
    monkeypatch.delenv("INWORLD_API_KEY", raising=False)
    with pytest.raises(TTSNotConfigured):
        asyncio.run(synthesize("Hello"))


def test_synthesize_empty_response_raises(monkeypatch):
    monkeypatch.setenv("INWORLD_API_KEY", "k")
    fake = FakeClient({})
    monkeypatch.setattr(inworld, "_http", lambda: fake)
    with pytest.raises(TTSFailed):
        asyncio.run(synthesize("Hello"))
