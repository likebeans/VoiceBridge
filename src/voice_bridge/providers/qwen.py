import asyncio
import base64
import json
import math
from collections.abc import AsyncIterator
from urllib.parse import urlsplit
from uuid import uuid4

from websockets.asyncio.client import ClientConnection
from websockets.exceptions import ConnectionClosed, InvalidStatus, WebSocketException

from voice_bridge.providers.base import (
    AudioFormat,
    Finished,
    ProviderError,
    ProviderEvent,
    SegmentOrder,
    Transcript,
)
from voice_bridge.providers.transport import (
    CONNECT_OPTIONS,
    WIRE_LOGGER,
    DirectConnect,
    handshake_error,
)


def _protocol_error() -> ProviderError:
    return ProviderError(
        "upstream_protocol_error", "Invalid response from the transcription service."
    )


def _string(event: dict, field: str, *, allow_empty: bool = False) -> str:
    value = event.get(field)
    if not isinstance(value, str) or (not allow_empty and not value.strip()):
        raise _protocol_error()
    return value


def _reject_json_constant(value: str) -> None:
    raise ValueError("Invalid JSON constant")


class QwenProvider:
    """One Qwen realtime ASR session using server-side voice activity detection."""

    def __init__(
        self,
        *,
        api_key: str,
        url: str,
        connect_timeout: float = 10,
        send_timeout: float = 10,
        silence_duration_ms: int = 400,
    ) -> None:
        if (
            not isinstance(api_key, str)
            or not api_key.strip()
            or "\r" in api_key
            or "\n" in api_key
        ):
            raise ValueError("api_key must be a nonempty single-line string")
        try:
            parsed = urlsplit(url) if isinstance(url, str) else None
            valid_url = parsed is not None and parsed.scheme in {"ws", "wss"} and parsed.hostname
        except ValueError:
            valid_url = False
        if not valid_url:
            raise ValueError("url must be a full ws:// or wss:// URL")
        for timeout in (connect_timeout, send_timeout):
            if (
                isinstance(timeout, bool)
                or not isinstance(timeout, (int, float))
                or not math.isfinite(timeout)
                or timeout <= 0
            ):
                raise ValueError("timeouts must be finite positive numbers")
        if (
            isinstance(silence_duration_ms, bool)
            or not isinstance(silence_duration_ms, int)
            or silence_duration_ms <= 0
        ):
            raise ValueError("silence_duration_ms must be a positive integer")
        self._api_key = api_key
        self._url = url
        self._connect_timeout = connect_timeout
        self._send_timeout = send_timeout
        self._silence_duration_ms = silence_duration_ms
        self._socket: ClientConnection | None = None
        self._send_lock = asyncio.Lock()
        self._started = False
        self._ready = False
        self._closed = False
        self._finish_sent = False
        self._finished = False

    async def open(self, *, language: str | None, audio: AudioFormat) -> None:
        if self._started or self._closed:
            raise ProviderError(
                "provider_state_error", "This transcription session cannot be opened."
            )
        if audio != AudioFormat():
            raise ProviderError("invalid_audio_format", "Audio must be PCM16LE, 16 kHz, mono.")
        if language is not None and (
            not isinstance(language, str)
            or not language.strip()
            or len(language) > 64
            or any(character.isspace() for character in language)
        ):
            raise ProviderError("invalid_language", "Language must be a nonempty language code.")
        self._started = True
        session = {
            "modalities": ["text"],
            "input_audio_format": "pcm",
            "sample_rate": 16000,
            "turn_detection": {
                "type": "server_vad",
                "threshold": 0.0,
                "silence_duration_ms": self._silence_duration_ms,
            },
        }
        if language is not None:
            session["input_audio_transcription"] = {"language": language}
        try:
            async with asyncio.timeout(self._connect_timeout):
                self._socket = await DirectConnect(
                    self._url,
                    additional_headers={
                        "Authorization": f"Bearer {self._api_key}",
                        "OpenAI-Beta": "realtime=v1",
                    },
                    open_timeout=self._connect_timeout,
                    close_timeout=1,
                    logger=WIRE_LOGGER,
                    **CONNECT_OPTIONS,
                )
                await self._send("session.update", session=session)
                while True:
                    event = await self._receive()
                    if event["type"] == "session.updated":
                        self._ready = True
                        return
                    if event["type"] == "session.finished":
                        raise _protocol_error()
        except TimeoutError:
            raise ProviderError(
                "upstream_timeout", "Timed out opening the transcription session."
            ) from None
        except InvalidStatus as exc:
            raise handshake_error(exc) from None
        except (OSError, WebSocketException, ValueError):
            raise ProviderError(
                "upstream_connection_error", "Could not connect to the transcription service."
            ) from None
        finally:
            if not self._ready:
                await self.close()

    async def send_audio(self, audio: bytes) -> None:
        self._require_ready()
        if self._finish_sent or self._finished:
            raise ProviderError("provider_state_error", "Audio input has already finished.")
        if not isinstance(audio, bytes) or not audio or len(audio) % 2:
            raise ProviderError("invalid_audio", "Audio must contain complete PCM16LE samples.")
        await self._send("input_audio_buffer.append", audio=base64.b64encode(audio).decode("ascii"))

    async def finish(self) -> None:
        if self._finish_sent:
            return
        self._require_ready()
        # Set before awaiting so concurrent callers send exactly one finish event.
        self._finish_sent = True
        await self._send("session.finish")

    async def events(self) -> AsyncIterator[ProviderEvent]:
        self._require_ready()
        while not self._finished:
            event = await self._receive()
            event_type = event["type"]
            if event_type == "input_audio_buffer.committed":
                item_id = _string(event, "item_id")
                if "previous_item_id" not in event:
                    raise _protocol_error()
                previous = event["previous_item_id"]
                if previous is not None:
                    previous = _string(event, "previous_item_id")
                yield SegmentOrder(item_id, previous)
            elif event_type == "conversation.item.input_audio_transcription.text":
                yield Transcript(
                    _string(event, "item_id"),
                    _string(event, "text", allow_empty=True)
                    + _string(event, "stash", allow_empty=True),
                    False,
                )
            elif event_type == "conversation.item.input_audio_transcription.completed":
                yield Transcript(
                    _string(event, "item_id"),
                    _string(event, "transcript", allow_empty=True),
                    True,
                )
            elif event_type == "session.finished":
                self._finished = True
                yield Finished()
                return

    def _require_ready(self) -> None:
        if not self._ready or self._closed:
            raise ProviderError("provider_state_error", "The transcription session is not ready.")

    async def close(self) -> None:
        self._closed = True
        self._ready = False
        socket, self._socket = self._socket, None
        if socket is None:
            return
        try:
            async with asyncio.timeout(1):
                await socket.close()
        except (TimeoutError, OSError, WebSocketException):
            pass
        finally:
            # A cancelled close must also release the underlying transport.
            socket.transport.abort()

    async def _send(self, event_type: str, **fields) -> None:
        try:
            async with asyncio.timeout(self._send_timeout):
                async with self._send_lock:
                    if self._socket is None or self._closed:
                        raise ProviderError(
                            "provider_state_error", "The transcription session is closed."
                        )
                    await self._socket.send(
                        json.dumps(
                            {"event_id": f"event_{uuid4().hex}", "type": event_type, **fields}
                        )
                    )
        except TimeoutError:
            raise ProviderError(
                "upstream_timeout", "Timed out sending data to the transcription service."
            ) from None
        except (ConnectionClosed, OSError):
            raise ProviderError(
                "upstream_closed", "The transcription service disconnected before finishing."
            ) from None

    async def _receive(self) -> dict:
        if self._socket is None or self._closed:
            raise ProviderError("provider_state_error", "The transcription session is closed.")
        try:
            raw = await self._socket.recv()
        except (ConnectionClosed, OSError):
            raise ProviderError(
                "upstream_closed", "The transcription service disconnected before finishing."
            ) from None
        if not isinstance(raw, str):
            raise _protocol_error()
        try:
            event = json.loads(raw, parse_constant=_reject_json_constant)
        except (ValueError, RecursionError):
            raise _protocol_error() from None
        if not isinstance(event, dict):
            raise _protocol_error()
        event_type = _string(event, "type")
        if "event_id" in event:
            _string(event, "event_id")
        if event_type in {"error", "conversation.item.input_audio_transcription.failed"}:
            raise ProviderError("upstream_error", "The transcription service reported an error.")
        return event
