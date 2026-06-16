import os
import asyncio
from fastapi import WebSocket
from assemblyai.streaming.v3 import (
    StreamingClient,
    StreamingClientOptions,
    StreamingEvents,
    StreamingParameters,
    TurnEvent,
)

async def proxy_transcription(client_ws: WebSocket):
    """Bridges extension's mic stream to AssemblyAI via official SDK."""
    
    aai_client = StreamingClient(
        StreamingClientOptions(api_key=os.environ["ASSEMBLYAI_API_KEY"])
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
    
    aai_client.on(StreamingEvents.Turn, on_turn)
    
    try:
        aai_client.connect(StreamingParameters(
            sample_rate=16000,
            speech_model="u3-rt-pro"
        ))
        
        # forward audio chunks from extension to AssemblyAI
        while True:
            audio_chunk = await client_ws.receive_bytes()
            aai_client.stream(audio_chunk)
            
    except Exception as e:
        print(f"Transcription error: {e}")
    finally:
        # CRITICAL: always terminate or you keep getting billed up to 3hrs
        aai_client.disconnect(terminate=True)