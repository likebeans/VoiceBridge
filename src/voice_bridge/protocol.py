from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

from voice_bridge.languages import normalize_language


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class AudioConfig(StrictModel):
    encoding: Literal["pcm_s16le"] = "pcm_s16le"
    sample_rate: Literal[16000] = 16000
    channels: Literal[1] = 1


class StartMessage(StrictModel):
    type: Literal["start"]
    # Omitted or null means "use the service's configured default". The session
    # resolves it before any provider or language check.
    provider: Literal["qwen", "funasr"] | None = None
    audio: AudioConfig = Field(default_factory=AudioConfig)
    language: str | None = Field(default=None, min_length=2, max_length=16)

    @field_validator("language", mode="before")
    @classmethod
    def valid_language(cls, value: object) -> str | None:
        return normalize_language(value)


class StopMessage(StrictModel):
    type: Literal["stop"]
