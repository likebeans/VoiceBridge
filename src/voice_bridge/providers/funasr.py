import asyncio
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


def _reject_json_constant(value: str) -> None:
    raise ValueError("Invalid JSON constant")


# Only known identifiers enter logs. An identifier-shaped arbitrary string can still
# contain a credential or transcript, so a character regex alone isn't sufficient.
_ERROR_CODES = frozenset(
    {
        "InvalidApiKey",
        "InvalidParameter",
        "InvalidParameter.UnsupportedFormat",
        "InvalidParameter.UnsupportedSampleRate",
        "InvalidParameter.UnsupportedLanguage",
        "Throttling",
        "Throttling.User",
        "Throttling.RateQuota",
        "Throttling.AllocationQuota",
        "AccessDenied",
        "InternalError",
        "ServiceUnavailable",
        "ModelNotFound",
        "CLIENT_ERROR",
        "SERVER_ERROR",
        "UNKNOWN_ERROR",
        "TEST_FAILURE",
    }
)


def _safe_error_code(value: object) -> str | None:
    """Keep only recognized vendor error codes for logs.

    The accompanying error message is never read: it is free text that could echo input.
    """
    if value is None:
        return None
    if isinstance(value, str) and value in _ERROR_CODES:
        return value
    return "unrecognized"


class FunAsrProvider:
    """One Fun-ASR realtime session through the MaaS gateway (DashScope task protocol).

    The server segments speech into sentences. A sentence streams as full-text snapshots
    and is final once `sentence_end` is true, so sentences arrive strictly in speech order
    and no separate ordering events are needed.
    """

    def __init__(
        self,
        *,
        api_key: str,
        url: str,
        model: str = "fun-asr-realtime",
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
        if not isinstance(model, str) or not model.strip():
            raise ValueError("model must be a nonempty string")
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
        self._model = model
        self._connect_timeout = connect_timeout
        self._send_timeout = send_timeout
        self._silence_duration_ms = silence_duration_ms
        self._task_id = uuid4().hex
        self._socket: ClientConnection | None = None
        self._send_lock = asyncio.Lock()
        self._started = False
        self._ready = False
        self._closed = False
        self._finish_sent = False
        self._finished = False
        self._spoken: set[str] = set()  # sentences that have shown text

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
        parameters: dict = {
            "format": "pcm",
            "sample_rate": 16000,
            "max_sentence_silence": self._silence_duration_ms,
        }
        if language is not None:
            parameters["language_hints"] = [language]
        try:
            async with asyncio.timeout(self._connect_timeout):
                self._socket = await DirectConnect(
                    self._url,
                    additional_headers={"Authorization": f"Bearer {self._api_key}"},
                    open_timeout=self._connect_timeout,
                    close_timeout=1,
                    logger=WIRE_LOGGER,
                    **CONNECT_OPTIONS,
                )
                await self._send_json(
                    "run-task",
                    {
                        "task_group": "audio",
                        "task": "asr",
                        "function": "recognition",
                        "model": self._model,
                        "parameters": parameters,
                        "input": {},
                    },
                )
                while True:
                    event = await self._receive()
                    if event == "task-started":
                        self._ready = True
                        return
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
        await self._send(audio)

    async def finish(self) -> None:
        if self._finish_sent:
            return
        self._require_ready()
        # Set before awaiting so concurrent callers send exactly one finish-task.
        self._finish_sent = True
        await self._send_json("finish-task", {"input": {}})

    async def events(self) -> AsyncIterator[ProviderEvent]:
        self._require_ready()
        while not self._finished:
            message = await self._receive_message()
            event = message["header"]["event"]
            if event == "result-generated":
                transcript = self._transcript(message)
                if transcript is not None:
                    yield transcript
            elif event == "task-finished":
                self._finished = True
                yield Finished()
                return

    def _transcript(self, message: dict) -> Transcript | None:
        payload = message.get("payload")
        output = payload.get("output") if isinstance(payload, dict) else None
        sentence = output.get("sentence") if isinstance(output, dict) else None
        if sentence is None:
            return None  # Results without a sentence carry nothing to display.
        if not isinstance(sentence, dict):
            raise _protocol_error()
        text = sentence.get("text")
        sentence_id = sentence.get("sentence_id")
        is_final = sentence.get("sentence_end", False)
        if not isinstance(text, str) or not isinstance(is_final, bool):
            raise _protocol_error()
        if isinstance(sentence_id, bool) or not isinstance(sentence_id, (int, str)):
            raise _protocol_error()
        if isinstance(sentence_id, str) and not sentence_id.strip():
            raise _protocol_error()
        segment_id = str(sentence_id)
        if text:
            self._spoken.add(segment_id)
        elif not is_final and segment_id not in self._spoken:
            return None  # Silence keepalives must not create empty pending segments.
        # An empty snapshot after text is a real revision: snapshots replace, never append.
        return Transcript(segment_id, text, is_final)

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

    async def _send_json(self, action: str, payload: dict) -> None:
        await self._send(
            json.dumps(
                {
                    "header": {"action": action, "task_id": self._task_id, "streaming": "duplex"},
                    "payload": payload,
                }
            )
        )

    async def _send(self, data: str | bytes) -> None:
        try:
            async with asyncio.timeout(self._send_timeout):
                async with self._send_lock:
                    if self._socket is None or self._closed:
                        raise ProviderError(
                            "provider_state_error", "The transcription session is closed."
                        )
                    await self._socket.send(data)
        except TimeoutError:
            raise ProviderError(
                "upstream_timeout", "Timed out sending data to the transcription service."
            ) from None
        except (ConnectionClosed, OSError):
            raise ProviderError(
                "upstream_closed", "The transcription service disconnected before finishing."
            ) from None

    async def _receive(self) -> str:
        """Return the next event name; raise on task failure or malformed frames."""
        return (await self._receive_message())["header"]["event"]

    async def _receive_message(self) -> dict:
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
            message = json.loads(raw, parse_constant=_reject_json_constant)
        except (ValueError, RecursionError):
            raise _protocol_error() from None
        header = message.get("header") if isinstance(message, dict) else None
        event = header.get("event") if isinstance(header, dict) else None
        if not isinstance(event, str) or not event.strip():
            raise _protocol_error()
        if event == "task-failed":
            upstream_code = _safe_error_code(header.get("error_code"))
            code = "upstream_error"
            if upstream_code in {"ServiceUnavailable", "InternalError", "SERVER_ERROR"}:
                code = "upstream_unavailable"
            elif upstream_code in {"InvalidApiKey", "AccessDenied"}:
                code = "upstream_auth_error"
            elif upstream_code and upstream_code.startswith("Throttling"):
                code = "upstream_rate_limited"
            elif upstream_code == "InvalidParameter.UnsupportedLanguage":
                code = "invalid_language"
            raise ProviderError(
                code,
                "The transcription service reported an error.",
                upstream_code=upstream_code,
            )
        return message
