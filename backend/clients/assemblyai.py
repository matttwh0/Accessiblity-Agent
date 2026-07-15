import os
import asyncio
from fastapi import WebSocket, WebSocketDisconnect
from assemblyai.streaming.v3 import (
    StreamingClient,
    StreamingClientOptions,
    StreamingEvents,
    StreamingParameters,
    TurnEvent,
)

async def proxy_transcription(client_ws: WebSocket):
    """Bridges extension's mic stream to AssemblyAI via official SDK."""

    api_key = os.environ.get("ASSEMBLYAI_API_KEY")
    if not api_key:
        # tell the user why voice doesn't work instead of crashing the endpoint
        await client_ws.send_json({
            "type": "transcribe_error",
            "error": "voice is not configured — set ASSEMBLYAI_API_KEY in backend/.env"
        })
        await client_ws.close()
        return

    aai_client = StreamingClient(
        StreamingClientOptions(api_key=api_key)
    )

    loop = asyncio.get_event_loop()

    def on_turn(_, event: TurnEvent):
        asyncio.run_coroutine_threadsafe(
            client_ws.send_json({
                "type": "transcript",
                "text": event.transcript,
                "is_final": event.end_of_turn
            }),
            loop
        )

    def on_error(_, error):
        # bad key, quota, dropped session — surface it to the bubble UI
        asyncio.run_coroutine_threadsafe(
            client_ws.send_json({"type": "transcribe_error", "error": str(error)}),
            loop
        )

    aai_client.on(StreamingEvents.Turn, on_turn)
    aai_client.on(StreamingEvents.Error, on_error)

    try:
        aai_client.connect(StreamingParameters(
            sample_rate=16000,
            speech_model="u3-rt-pro"
        ))

        # forward audio chunks from extension to AssemblyAI
        while True:
            audio_chunk = await client_ws.receive_bytes()
            aai_client.stream(audio_chunk)

    except WebSocketDisconnect:
        pass  # the extension hung up (recording stopped) — a normal end
    except Exception as e:
        print(f"Transcription error: {e}")
    finally:
        # CRITICAL: always terminate or you keep getting billed up to 3hrs
        aai_client.disconnect(terminate=True)
