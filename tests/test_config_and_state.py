import pytest
from pydantic import ValidationError

from voice_bridge.config import Settings
from voice_bridge.providers.base import ProviderError, SegmentOrder, Transcript
from voice_bridge.transcript import TranscriptState


def test_provider_configuration_requires_upstream_key_and_wss():
    Settings(
        _env_file=None, service_api_key="", dashscope_api_key="fake-key", maas_api_key=""
    ).validate_provider_config()
    with pytest.raises(ValueError, match="DASHSCOPE_API_KEY"):
        Settings(
            _env_file=None, service_api_key="", dashscope_api_key="", maas_api_key=""
        ).validate_provider_config()
    with pytest.raises(ValidationError):
        Settings(_env_file=None, qwen_base_url="ws://example.com/realtime")
    assert (
        not {
            "session_signing_key",
            "session_token_ttl_seconds",
            "allowed_origins",
        }
        & Settings.model_fields.keys()
    )


@pytest.mark.parametrize("key_name", ["dashscope_api_key", "maas_api_key"])
def test_upstream_keys_trim_whitespace_and_reject_embedded_newlines(key_name):
    values = {"dashscope_api_key": "", "maas_api_key": "", key_name: " \t "}
    blank = Settings(_env_file=None, **values)
    with pytest.raises(ValueError, match="DASHSCOPE_API_KEY"):
        blank.validate_provider_config()
    values[key_name] = "  fake-key  "
    padded = Settings(_env_file=None, **values)
    assert getattr(padded, key_name).get_secret_value() == "fake-key"
    padded.validate_provider_config()
    for key in ["fake\nkey", "fake\rkey"]:
        values[key_name] = key
        with pytest.raises(ValidationError, match="single-line"):
            Settings(_env_file=None, **values)


@pytest.mark.parametrize("authority", ["user:password@", ":password@", "@"])
def test_qwen_endpoint_rejects_credentials_even_with_an_empty_username(authority):
    with pytest.raises(ValidationError, match="without credentials"):
        Settings(_env_file=None, qwen_base_url=f"wss://{authority}example.com/realtime")


def test_endpoint_uses_configured_model_once():
    settings = Settings(
        _env_file=None,
        qwen_base_url="wss://example.com/api-ws/v1/realtime?model=old&other=value",
        qwen_model="qwen3-asr-flash-realtime",
    )
    assert settings.qwen_url.count("model=") == 1
    assert "model=qwen3-asr-flash-realtime" in settings.qwen_url
    assert "other=value" in settings.qwen_url


def test_late_order_corrects_completion_arrival_order():
    state = TranscriptState()
    state.apply(Transcript("b", "第二句。", True))
    state.apply(Transcript("a", "第一句。", True))
    assert state.text() == "第二句。第一句。"
    assert state.order(SegmentOrder("b", "a"))
    assert state.text() == "第一句。第二句。"


def test_conflicting_finals_and_order_cycles_are_errors():
    state = TranscriptState()
    state.apply(Transcript("a", "原文", True))
    with pytest.raises(ProviderError):
        state.apply(Transcript("a", "修改", True))
    state.order(SegmentOrder("b", "a"))
    with pytest.raises(ProviderError):
        state.order(SegmentOrder("a", "b"))
    assert state.text(final_only=True) == "原文"
