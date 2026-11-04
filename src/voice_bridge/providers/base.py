from collections.abc import AsyncIterator
from dataclasses import dataclass
from typing import Protocol


class ProviderError(Exception):
    """A safe-to-display error; never include upstream responses or credentials.

    `upstream_code` is the vendor's own error identifier, already sanitized by the adapter.
    It is for server-side logs only and is never sent to clients.
    """

    def __init__(self, code: str, message: str, *, upstream_code: str | None = None):
        super().__init__(message)
        self.code = code
        self.message = message
        self.upstream_code = upstream_code


@dataclass(frozen=True)
class AudioFormat:
    encoding: str = "pcm_s16le"
    sample_rate: int = 16000
    channels: int = 1


@dataclass(frozen=True)
class SegmentOrder:
    segment_id: str
    previous_segment_id: str | None


@dataclass(frozen=True)
class Transcript:
    segment_id: str
    text: str
    is_final: bool


@dataclass(frozen=True)
class Finished:
    pass


ProviderEvent = SegmentOrder | Transcript | Finished


class ASRProvider(Protocol):
    """Providers normalize their wire events into transcript snapshots.

    The shared input format is PCM16LE / 16kHz / mono. A future provider
    requiring a different sample rate must convert it inside its adapter.
    finish() ends input; events() must yield Finished only after all finals.
    close() must be safe even if open() was cancelled or failed.
    """

    async def open(self, *, language: str | None, audio: AudioFormat) -> None: ...

    async def send_audio(self, audio: bytes) -> None: ...

    async def finish(self) -> None: ...

    def events(self) -> AsyncIterator[ProviderEvent]: ...

    async def close(self) -> None: ...
