import re
from typing import Literal
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from pydantic import Field, SecretStr, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        allow_inf_nan=False,
        hide_input_in_errors=True,
    )

    service_api_key: SecretStr = SecretStr("")
    dashscope_api_key: SecretStr = SecretStr("")
    qwen_base_url: str = "wss://dashscope.aliyuncs.com/api-ws/v1/realtime"
    qwen_model: str = "qwen3-asr-flash-realtime"
    maas_api_key: SecretStr = SecretStr("")
    maas_base_url: str = "https://maas.marketingforce.com"
    funasr_model: str = "fun-asr-realtime"
    max_session_seconds: float = Field(default=120, ge=1, le=600)
    max_audio_seconds: float = Field(default=120, ge=1, le=600)
    max_connections: int = Field(default=100, ge=1)
    start_timeout_seconds: float = Field(default=5, gt=0)
    # Per upstream attempt. The whole open phase (retries and fallback included) is bounded
    # separately by open_timeout_seconds, so a hung primary still leaves time for a fallback.
    connect_timeout_seconds: float = Field(default=5, gt=0)
    open_timeout_seconds: float = Field(default=12, gt=0)
    # Audio the client may send before `ready`; it is held and forwarded once upstream is open.
    pre_ready_buffer_seconds: float = Field(default=3, gt=0, le=10)
    # Provider to switch to, before the session starts, if the requested one cannot open.
    fallback_provider: Literal["qwen", "funasr"] | None = None
    # On SIGTERM, how long to let running sessions finish before connections are closed.
    # Include start/open/send timeouts as well as recording and final-result drain.
    shutdown_grace_seconds: float = Field(default=180, ge=0)
    idle_timeout_seconds: float = Field(default=15, gt=0)
    finish_timeout_seconds: float = Field(default=15, gt=0)
    send_timeout_seconds: float = Field(default=5, gt=0)
    max_audio_chunk_bytes: int = Field(default=16384, ge=2, le=32768)
    max_control_message_bytes: int = Field(default=8192, ge=512, le=32768)
    vad_silence_duration_ms: int = Field(default=400, ge=200, le=2000)
    host: str = "0.0.0.0"
    port: int = Field(default=8000, ge=1, le=65535)

    @field_validator("service_api_key")
    @classmethod
    def validate_service_key(cls, value: SecretStr) -> SecretStr:
        key = value.get_secret_value().strip()
        if key and re.fullmatch(r"[A-Za-z0-9_-]{32,256}", key) is None:
            raise ValueError("SERVICE_API_KEY must contain 32–256 ASCII letters, digits, _ or -")
        return SecretStr(key)

    def validate_startup(self) -> None:
        if not self.service_api_key.get_secret_value():
            raise ValueError("SERVICE_API_KEY is required")
        self.validate_provider_config()

    @field_validator("qwen_base_url")
    @classmethod
    def validate_url(cls, value: str) -> str:
        parsed = urlsplit(value)
        if (
            parsed.scheme != "wss"
            or not parsed.hostname
            or parsed.fragment
            or parsed.username is not None
            or parsed.password is not None
        ):
            raise ValueError("QWEN_BASE_URL must be a wss URL without credentials or a fragment")
        return value

    @field_validator("maas_base_url")
    @classmethod
    def validate_maas_url(cls, value: str) -> str:
        parsed = urlsplit(value)
        if (
            parsed.scheme not in {"http", "https"}
            or not parsed.hostname
            or parsed.query
            or parsed.fragment
            or parsed.username is not None  # also rejects an empty user in "://:pw@host"
            or parsed.password is not None
        ):
            raise ValueError(
                "MAAS_BASE_URL must be an http(s) URL without credentials, query or fragment"
            )
        return value

    @field_validator("dashscope_api_key", "maas_api_key")
    @classmethod
    def validate_api_key(cls, value: SecretStr) -> SecretStr:
        key = value.get_secret_value().strip()
        if "\r" in key or "\n" in key:
            raise ValueError("Upstream API keys must be single-line values")
        return SecretStr(key)  # a blank key counts as not configured

    @field_validator("fallback_provider", mode="before")
    @classmethod
    def blank_fallback_means_none(cls, value: object) -> object:
        return None if isinstance(value, str) and not value.strip() else value

    def api_key_for(self, provider: str) -> str:
        keys = {"funasr": self.maas_api_key, "qwen": self.dashscope_api_key}
        return keys[provider].get_secret_value()

    @property
    def default_provider(self) -> Literal["qwen", "funasr"] | None:
        """The provider a client gets when it does not request one.

        Both endpoints and the capabilities response read this, so the advertised
        recommendation and the session's automatic choice can never disagree.
        """
        if self.dashscope_api_key.get_secret_value():
            return "qwen"
        if self.maas_api_key.get_secret_value():
            return "funasr"
        return None

    def validate_provider_config(self) -> None:
        if not (self.dashscope_api_key.get_secret_value() or self.maas_api_key.get_secret_value()):
            raise ValueError("DASHSCOPE_API_KEY or MAAS_API_KEY is required")
        if self.fallback_provider and not self.api_key_for(self.fallback_provider):
            raise ValueError("FALLBACK_PROVIDER is set but that provider has no API key")

    @property
    def funasr_url(self) -> str:
        parts = urlsplit(self.maas_base_url)
        scheme = "wss" if parts.scheme == "https" else "ws"
        path = parts.path.rstrip("/") + "/v1/audio/asr-realtime"
        return urlunsplit((scheme, parts.netloc, path, urlencode({"model": self.funasr_model}), ""))

    @property
    def qwen_url(self) -> str:
        parts = urlsplit(self.qwen_base_url)
        query = dict(parse_qsl(parts.query))
        query["model"] = self.qwen_model
        return urlunsplit(parts._replace(query=urlencode(query)))
