import asyncio
import json
import logging
import random
import time
from collections.abc import Callable
from contextlib import suppress
from dataclasses import dataclass, field
from uuid import uuid4

from fastapi import WebSocket, WebSocketDisconnect
from pydantic import ValidationError

from voice_bridge.config import Settings
from voice_bridge.languages import can_fallback, profile_for, wire_language
from voice_bridge.metrics import Metrics
from voice_bridge.protocol import StartMessage, StopMessage
from voice_bridge.providers.base import (
    ASRProvider,
    AudioFormat,
    Finished,
    ProviderError,
    SegmentOrder,
    Transcript,
)
from voice_bridge.transcript import Segment, TranscriptState

logger = logging.getLogger(__name__)
ProviderFactory = Callable[[str], ASRProvider]

PCM_BYTES_PER_SECOND = 32000  # 16 kHz, 16-bit, mono

# A fast transient failure is worth one more try on the same provider. A timeout is not:
# it already spent the attempt's whole budget, which the fallback needs more.
_RETRYABLE = frozenset({"upstream_connection_error", "upstream_closed", "upstream_unavailable"})
# Failures that say "this provider is unhealthy right now", so another provider may work.
# Client mistakes (bad language, bad audio format) would fail the same way elsewhere.
_FALLBACK_ELIGIBLE = _RETRYABLE | {
    "upstream_timeout",
    "upstream_error",
    "upstream_protocol_error",
    "provider_unavailable",
    "upstream_auth_error",
    "upstream_rate_limited",
    "upstream_rejected",
}
_RETRY_JITTER_SECONDS = (0.05, 0.15)  # spread retries so a flaky upstream is not hit in lockstep


class SessionError(Exception):
    def __init__(self, code: str, message: str, close_code: int = 1008):
        super().__init__(message)
        self.code = code
        self.message = message
        self.close_code = close_code


@dataclass
class _EarlyInput:
    """What the client sent while the upstream connection was still opening."""

    audio: bytearray = field(default_factory=bytearray)
    stop: bool = False


class TranscriptionSession:
    def __init__(
        self,
        ws: WebSocket,
        settings: Settings,
        factory: ProviderFactory,
        metrics: Metrics | None = None,
    ):
        self.ws = ws
        self.settings = settings
        self.factory = factory
        self.metrics = metrics or Metrics()
        self.id = uuid4().hex
        self.provider: ASRProvider | None = None
        self.requested_provider: str | None = None
        self.provider_name: str | None = None  # the provider actually serving the session
        self.transcript = TranscriptState()
        self.stopping = asyncio.Event()
        self.audio_bytes = 0
        self.sequence = 0
        self._ready_sent = False
        self._first_audio_at: float | None = None
        self._first_transcript_seen = False
        self._stop_at: float | None = None

    async def run(self) -> None:
        close_code = 1000
        outcome = "interrupted"  # what a cancelled session is recorded as
        began = time.monotonic()
        try:
            start = await self._start()
            self.requested_provider = start.provider
            opening_began = time.monotonic()
            early = await self._open(start)
            assert self.provider_name is not None
            ready = {
                "type": "ready",
                "session_id": self.id,
                "provider": self.provider_name,
                "audio": start.audio.model_dump(),
                "max_session_seconds": self.settings.max_session_seconds,
                "max_audio_seconds": self.settings.max_audio_seconds,
                "max_audio_chunk_bytes": self.settings.max_audio_chunk_bytes,
            }
            if self.provider_name != start.provider:
                ready["fallback_from"] = start.provider
            await self._send(ready)
            self.metrics.ready_seconds.observe(time.monotonic() - opening_began, self.provider_name)
            self._ready_sent = True
            await self._stream(early)
            await self._send(
                {
                    "type": "done",
                    "session_id": self.id,
                    "full_text": self.transcript.text(final_only=True),
                    "audio_seconds": self.audio_bytes / PCM_BYTES_PER_SECOND,
                }
            )
            outcome = "done"
            if self._stop_at is not None:
                self.metrics.finish_seconds.observe(
                    time.monotonic() - self._stop_at, self.provider_name
                )
        except asyncio.CancelledError:
            close_code = 1001
            raise
        except WebSocketDisconnect as exc:
            outcome, close_code = "client_disconnected", exc.code
        except SessionError as exc:
            outcome, close_code = exc.code, exc.close_code
            await self._report(exc.code, exc.message)
        except ProviderError as exc:
            outcome, close_code = exc.code, 1011
            if self._ready_sent:  # Failures while opening were already logged per attempt.
                self._log_provider_failure(self.provider_name or "unknown", exc)
            await self._report(exc.code, exc.message)
        except Exception as exc:
            outcome, close_code = "internal_error", 1011
            logger.error("Session %s failed (%s)", self.id, type(exc).__name__)
            await self._report("internal_error", "The transcription session could not continue")
        finally:
            # Record first: the awaits below can be cancelled (server shutdown, client gone).
            self._record(outcome, close_code, time.monotonic() - began)
            if self.provider is not None:
                await self._close_quietly(self.provider)
            with suppress(Exception):
                async with asyncio.timeout(self.settings.send_timeout_seconds):
                    await self.ws.close(code=close_code)

    def _record(self, outcome: str, close_code: int, duration: float) -> None:
        provider = self.provider_name or self.requested_provider or "unknown"
        self.metrics.sessions.inc(provider, outcome)
        self.metrics.session_seconds.observe(duration, provider)
        # Private application codes are grouped to keep the label set bounded.
        code = (
            str(close_code) if close_code in {1000, 1001, 1006, 1008, 1009, 1011, 1013} else "other"
        )
        self.metrics.close_codes.inc(provider, code)

    def _log_provider_failure(self, provider: str, exc: ProviderError) -> None:
        # The vendor's code is sanitized by the adapter; its message text is never logged.
        logger.warning(
            "Session %s: provider %s failed (%s, upstream_code=%s)",
            self.id,
            provider,
            exc.code,
            exc.upstream_code,
        )

    @staticmethod
    async def _close_quietly(provider: ASRProvider) -> None:
        with suppress(Exception):
            async with asyncio.timeout(3):
                await provider.close()

    async def _send(self, payload: dict) -> None:
        try:
            async with asyncio.timeout(self.settings.send_timeout_seconds):
                await self.ws.send_json(payload)
        except TimeoutError as exc:
            raise SessionError("slow_client", "Client is not receiving results", 1013) from exc

    async def _report(self, code: str, message: str) -> None:
        with suppress(Exception):
            # Only finalized text is retained after an interrupted session.
            await self._send(
                {
                    "type": "error",
                    "session_id": self.id,
                    "code": code,
                    "message": message,
                    "confirmed_text": self.transcript.text(final_only=True),
                }
            )

    async def _receive(self) -> dict:
        message = await self.ws.receive()
        if message["type"] == "websocket.disconnect":
            raise WebSocketDisconnect(message.get("code", 1000))
        return message

    def _control(self, message: dict) -> dict:
        text = message.get("text")
        if text is None:
            raise SessionError("invalid_message", "Expected a JSON control message")
        if len(text.encode("utf-8")) > self.settings.max_control_message_bytes:
            raise SessionError("message_too_large", "Control message exceeds the size limit", 1009)
        try:
            value = json.loads(text)
        except (ValueError, RecursionError) as exc:
            raise SessionError("invalid_message", "Invalid JSON control message") from exc
        if not isinstance(value, dict):
            raise SessionError("invalid_message", "Control message must be an object")
        return value

    def _parse_stop(self, message: dict) -> None:
        try:
            StopMessage.model_validate(self._control(message))
        except ValidationError as exc:
            raise SessionError(
                "invalid_message", "Only audio or stop is accepted after start"
            ) from exc
        if self._stop_at is None:
            self._stop_at = time.monotonic()

    def _check_audio(self, audio: bytes, accepted: int) -> None:
        """Validate one client audio chunk; `accepted` is the audio already taken."""
        if not audio or len(audio) % 2:
            raise SessionError("invalid_audio", "PCM audio must have a nonzero even byte count")
        if len(audio) > self.settings.max_audio_chunk_bytes:
            raise SessionError("message_too_large", "Audio chunk exceeds the size limit", 1009)
        if accepted + len(audio) > self.settings.max_audio_seconds * PCM_BYTES_PER_SECOND:
            raise SessionError("audio_limit", "Audio duration limit reached")
        if self._first_audio_at is None:
            self._first_audio_at = time.monotonic()

    async def _start(self) -> StartMessage:
        try:
            async with asyncio.timeout(self.settings.start_timeout_seconds):
                message = await self._receive()
        except TimeoutError as exc:
            raise SessionError("start_timeout", "Send start immediately after connecting") from exc
        try:
            return StartMessage.model_validate(self._control(message))
        except ValidationError as exc:
            raise SessionError(
                "invalid_start",
                "Expected start with provider qwen or funasr and PCM16LE / 16000 Hz / mono audio",
            ) from exc

    async def _open(self, start: StartMessage) -> _EarlyInput:
        """Open the upstream session while holding back whatever the client sends meanwhile.

        Clients that start capturing right after `start` would otherwise lose their first
        words (or fail) because audio arrives before `ready`. Up to pre_ready_buffer_seconds
        of audio is kept and replayed in order once the upstream is open.
        """
        early = _EarlyInput()
        opening = asyncio.create_task(self._open_provider(start))
        incoming: asyncio.Task[dict] | None = None
        try:
            async with asyncio.timeout(self.settings.open_timeout_seconds):
                while True:
                    if incoming is None:
                        incoming = asyncio.create_task(self._receive())
                    done, _ = await asyncio.wait(
                        {opening, incoming}, return_when=asyncio.FIRST_COMPLETED
                    )
                    # Handle a message that arrived together with the open before deciding,
                    # so a disconnect or a bad message is never silently dropped.
                    if incoming.done():
                        message, incoming = incoming.result(), None
                        self._hold_early(message, early)
                    if opening in done:
                        await opening  # raises the open failure, if any
                        return early
        except TimeoutError as exc:
            raise SessionError(
                "upstream_timeout", "Timed out connecting to speech recognition", 1011
            ) from exc
        finally:
            pending = [task for task in (opening, incoming) if task is not None]
            for task in pending:
                if not task.done():
                    task.cancel()
            await asyncio.gather(*pending, return_exceptions=True)

    def _hold_early(self, message: dict, early: _EarlyInput) -> None:
        audio = message.get("bytes")
        if audio is None:
            self._parse_stop(message)
            early.stop = True
            return
        if early.stop:
            raise SessionError("audio_after_stop", "Audio is not accepted after stop")
        self._check_audio(audio, len(early.audio))
        limit = int(self.settings.pre_ready_buffer_seconds * PCM_BYTES_PER_SECOND) // 2 * 2
        if len(early.audio) + len(audio) > limit:
            raise SessionError(
                "pre_ready_overflow", "Too much audio was sent before ready; wait for ready"
            )
        early.audio += audio

    async def _open_provider(self, start: StartMessage) -> None:
        """Open the requested provider, or the configured fallback if it is unhealthy."""
        models = {"qwen": self.settings.qwen_model, "funasr": self.settings.funasr_model}
        primary = profile_for(start.provider, models[start.provider])
        if (
            primary is not None
            and start.language is not None
            and start.language not in primary.codes
        ):
            raise SessionError(
                "invalid_language", "The selected model does not support this language."
            )
        candidates = [start.provider]
        fallback = self.settings.fallback_provider
        if (
            fallback
            and fallback != start.provider
            and can_fallback(primary, profile_for(fallback, models[fallback]), start.language)
        ):
            candidates.append(fallback)
        first_error: ProviderError | None = None
        for name in candidates:
            try:
                await self._open_with_retry(name, start)
            except ProviderError as exc:
                if exc.code not in _FALLBACK_ELIGIBLE:
                    raise
                first_error = first_error or exc
                continue
            if name != start.provider:
                self.metrics.fallbacks.inc(start.provider, name)
            return
        assert first_error is not None
        raise first_error

    async def _open_with_retry(self, name: str, start: StartMessage) -> None:
        for attempt in range(2):
            try:
                began = time.monotonic()
                try:
                    await self._open_once(name, start)
                finally:
                    self.metrics.upstream_open_seconds.observe(time.monotonic() - began, name)
            except ProviderError as exc:
                self.metrics.upstream_open.inc(name, exc.code)
                self._log_provider_failure(name, exc)
                if attempt == 0 and exc.code in _RETRYABLE:
                    self.metrics.upstream_retries.inc(name)
                    await asyncio.sleep(random.uniform(*_RETRY_JITTER_SECONDS))
                    continue
                raise
            self.metrics.upstream_open.inc(name, "ok")
            self.provider_name = name
            return

    async def _open_once(self, name: str, start: StartMessage) -> None:
        provider = self.factory(name)
        self.provider = provider  # so a cancelled open is still cleaned up by run()
        try:
            async with asyncio.timeout(self.settings.connect_timeout_seconds):
                await provider.open(
                    language=wire_language(name, start.language), audio=AudioFormat()
                )
        except TimeoutError:
            await self._close_quietly(provider)
            self.provider = None
            raise ProviderError(
                "upstream_timeout", "Timed out opening the transcription session."
            ) from None
        except Exception:
            # Keep ownership until cleanup finishes, including if cleanup is cancelled.
            await self._close_quietly(provider)
            self.provider = None
            raise

    async def _stream(self, early: _EarlyInput) -> None:
        tasks = [
            asyncio.create_task(self._input(early)),
            asyncio.create_task(self._output()),
            asyncio.create_task(self._finish_deadline()),
            asyncio.create_task(self._recording_deadline()),
        ]
        try:
            await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
            # Don't report success if a concurrent input failure is already known.
            for task in tasks:
                if task.done():
                    task.result()
        finally:
            for task in tasks:
                if not task.done():
                    task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)

    async def _forward(self, audio: bytes) -> None:
        assert self.provider is not None
        # Serial sends preserve audio/stop order. No unbounded application queue.
        await self.provider.send_audio(audio)
        self.audio_bytes += len(audio)
        assert self.provider_name is not None
        self.metrics.audio_seconds.inc(self.provider_name, amount=len(audio) / PCM_BYTES_PER_SECOND)

    async def _flush_early(self, early: _EarlyInput) -> None:
        # Re-slice to the normal chunk size; step is even so no sample is ever split.
        step = max(2, self.settings.max_audio_chunk_bytes // 2 * 2)
        audio, early.audio = bytes(early.audio), bytearray()
        for offset in range(0, len(audio), step):
            await self._forward(audio[offset : offset + step])

    async def _finish_input(self) -> None:
        assert self.provider is not None
        if not self.stopping.is_set():
            self.stopping.set()
            await self.provider.finish()
        # Duplicate stop is harmless; the caller keeps observing client disconnects.

    async def _input(self, early: _EarlyInput) -> None:
        await self._flush_early(early)
        if early.stop:
            await self._finish_input()
        while True:
            try:
                timeout = None if self.stopping.is_set() else self.settings.idle_timeout_seconds
                async with asyncio.timeout(timeout):
                    message = await self._receive()
            except TimeoutError as exc:
                raise SessionError(
                    "idle_timeout", "No audio or stop received before idle timeout"
                ) from exc
            audio = message.get("bytes")
            if audio is None:
                self._parse_stop(message)
                await self._finish_input()
                continue
            if self.stopping.is_set():
                raise SessionError("audio_after_stop", "Audio is not accepted after stop")
            self._check_audio(audio, self.audio_bytes)
            await self._forward(audio)

    async def _output(self) -> None:
        assert self.provider is not None
        async for event in self.provider.events():
            if isinstance(event, SegmentOrder):
                if self.transcript.order(event) and self.transcript.text():
                    await self._publish(self.transcript.segments[event.segment_id])
            elif isinstance(event, Transcript):
                segment = self.transcript.apply(event)
                if segment is not None:
                    await self._publish(segment)
            elif isinstance(event, Finished):
                if not self.stopping.is_set():
                    raise ProviderError("upstream_closed", "Speech recognition ended before stop")
                if self.transcript.has_pending:
                    raise ProviderError(
                        "incomplete_transcript", "Speech recognition did not finalize all text"
                    )
                return
        raise ProviderError("upstream_closed", "Speech recognition disconnected before completion")

    async def _publish(self, segment: Segment) -> None:
        self.sequence += 1
        await self._send(
            {
                "type": "transcript",
                "session_id": self.id,
                "sequence": self.sequence,
                "segment_id": segment.id,
                "segment_index": self.transcript.segment_ids().index(segment.id),
                "revision": segment.revision,
                "text": segment.text,
                "is_final": segment.is_final,
                "full_text": self.transcript.text(),
                "confirmed_text": self.transcript.text(final_only=True),
            }
        )
        if segment.text and not self._first_transcript_seen:
            self._first_transcript_seen = True
            if self._first_audio_at is not None and self.provider_name:
                self.metrics.first_transcript_seconds.observe(
                    time.monotonic() - self._first_audio_at, self.provider_name
                )

    async def _recording_deadline(self) -> None:
        try:
            async with asyncio.timeout(self.settings.max_session_seconds):
                await self.stopping.wait()
        except TimeoutError as exc:
            raise SessionError(
                "session_limit", "Recording time limit reached; stop earlier"
            ) from exc
        # The separate finish deadline allows the last sentence to drain even
        # when stop arrives just before the recording limit.
        await asyncio.Future()

    async def _finish_deadline(self) -> None:
        await self.stopping.wait()
        try:
            async with asyncio.timeout(self.settings.finish_timeout_seconds):
                await asyncio.Future()
        except TimeoutError as exc:
            raise ProviderError(
                "finish_timeout", "Timed out waiting for final transcription"
            ) from exc
