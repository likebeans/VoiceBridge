"""Stream a local PCM16/16kHz/mono WAV to a running service without a browser."""

import argparse
import asyncio
import json
import os
import sys
import wave
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit

from websockets.exceptions import WebSocketException

from voice_bridge.providers.transport import CONNECT_OPTIONS, WIRE_LOGGER, DirectConnect


def websocket_url(base_url: str) -> str:
    parts = urlsplit(base_url)
    schemes = {"http": "ws", "https": "wss", "ws": "ws", "wss": "wss"}
    if parts.scheme not in schemes or not parts.hostname or parts.fragment:
        raise ValueError("Expected an http, https, ws or wss URL with a host and no fragment")
    path = parts.path.rstrip("/")
    if not path.endswith("/v1/asr"):
        path += "/v1/asr"
    return urlunsplit(parts._replace(scheme=schemes[parts.scheme], path=path))


async def stream(
    path: Path,
    base_url: str,
    provider: str = "qwen",
    language: str | None = None,
    *,
    api_key: str,
) -> None:
    if not api_key.strip():
        raise ValueError("Set SERVICE_API_KEY in the client environment")
    with wave.open(str(path), "rb") as audio:
        if (audio.getnchannels(), audio.getsampwidth(), audio.getframerate()) != (1, 2, 16000):
            raise ValueError("Expected a mono, 16-bit, 16000 Hz PCM WAV")
        async with DirectConnect(
            websocket_url(base_url),
            additional_headers={"X-API-Key": api_key.strip()},
            logger=WIRE_LOGGER,
            **CONNECT_OPTIONS,
        ) as ws:
            await ws.send(json.dumps({"type": "start", "provider": provider, "language": language}))
            first = json.loads(await ws.recv())
            print(json.dumps(first, ensure_ascii=False))
            if first["type"] != "ready":
                raise RuntimeError("The session did not become ready")

            frames_per_chunk = min(1600, first["max_audio_chunk_bytes"] // 2)
            if frames_per_chunk < 1:
                raise ValueError("Server advertised an invalid audio chunk limit")

            async def send_audio() -> None:
                while chunk := audio.readframes(frames_per_chunk):
                    await ws.send(chunk)
                    await asyncio.sleep(len(chunk) / 32000)
                await ws.send(json.dumps({"type": "stop"}))

            sender = asyncio.create_task(send_audio())
            completed = False
            try:
                async for raw in ws:
                    event = json.loads(raw)
                    print(json.dumps(event, ensure_ascii=False))
                    if event["type"] == "error":
                        raise RuntimeError(f"Transcription failed: {event['code']}")
                    if event["type"] == "done":
                        completed = True
                        break
                if not completed:
                    raise RuntimeError("Connection closed without done")
                await sender
            finally:
                if not sender.done():
                    sender.cancel()
                await asyncio.gather(sender, return_exceptions=True)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("wav", type=Path)
    parser.add_argument(
        "--url",
        default="http://127.0.0.1:8000",
        help="HTTP(S) base URL or WS(S) endpoint (default: http://127.0.0.1:8000)",
    )
    parser.add_argument(
        "--provider",
        choices=("qwen", "funasr"),
        default="qwen",
        help="ASR provider (default: qwen)",
    )
    parser.add_argument(
        "--language",
        default=None,
        help="Language hint such as ja, fr, ar; omit for automatic detection",
    )
    args = parser.parse_args()
    try:
        asyncio.run(
            stream(
                args.wav,
                args.url,
                args.provider,
                args.language,
                api_key=os.environ.get("SERVICE_API_KEY", ""),
            )
        )
    except (OSError, ValueError, RuntimeError, wave.Error, EOFError, WebSocketException) as exc:
        print(str(exc), file=sys.stderr)
        raise SystemExit(1) from None


if __name__ == "__main__":
    main()
