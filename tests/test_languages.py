"""Public language normalization, exact model profiles, and local start validation."""

import pytest
from starlette.websockets import WebSocketDisconnect
from test_production import Factory, Fake, service

from voice_bridge.languages import language_metadata, profile_for
from voice_bridge.protocol import StartMessage


@pytest.mark.parametrize(
    "provider,model,codes",
    [
        (
            "funasr",
            "fun-asr-realtime",
            "zh en ja ko vi th id ms fil hi ar fr de es pt ru it nl sv da fi no el "
            "pl cs hu ro bg hr sk",
        ),
        ("funasr", "fun-asr-realtime-2026-02-28", "zh en ja"),
        (
            "qwen",
            "qwen3-asr-flash-realtime",
            "zh yue en ja de ko ru fr pt ar it es hi id th tr uk vi cs da fil fi is ms no pl sv",
        ),
    ],
)
def test_exact_model_profiles_advertise_documented_canonical_codes(provider, model, codes):
    expected = set(codes.split())
    profile = profile_for(provider, model)
    assert profile is not None
    assert set(profile.codes) == expected
    assert len(profile.codes) == len(expected)
    metadata = language_metadata(provider, model)
    assert {language["code"] for language in metadata["languages"]} == expected
    assert len(metadata["languages"]) == len(expected)
    assert all(language["name"] for language in metadata["languages"])
    assert metadata["auto_detect"] is True
    assert metadata["language_support_known"] is True


@pytest.mark.parametrize(
    "provider,model",
    [
        ("funasr", "fun-asr-realtime-2026-02-27"),
        ("funasr", "fun-asr-realtime-latest"),
        ("funasr", "FUN-ASR-REALTIME"),
        ("qwen", "qwen3-asr-flash-realtime-2026-02-28"),
        ("qwen", "custom-asr"),
        ("qwen", "fun-asr-realtime"),
        ("funasr", "qwen3-asr-flash-realtime"),
    ],
)
def test_unknown_models_do_not_inherit_language_profiles(provider, model):
    assert profile_for(provider, model) is None
    assert language_metadata(provider, model) == {
        "languages": [],
        "auto_detect": None,
        "language_support_known": False,
    }


def test_omitting_language_defaults_to_auto_detection():
    assert StartMessage(type="start").language is None


@pytest.mark.parametrize(
    "value,expected",
    [
        (None, None),
        ("auto", None),
        (" \tAuTo\n", None),
        (" JA ", "ja"),
        ("\tFR\n", "fr"),
        ("FIL", "fil"),
        (" TL ", "fil"),
        ("zh-CN", "zh"),
        ("zh-TW", "zh"),
        ("zh-Hans", "zh"),
        ("zh-Hant", "zh"),
        ("en-US", "en"),
        ("en-GB", "en"),
        ("en-AU", "en"),
        ("ja-JP", "ja"),
        ("ko-KR", "ko"),
        ("fr-FR", "fr"),
        ("fr-CA", "fr"),
        ("de-DE", "de"),
        ("es-ES", "es"),
        ("es-MX", "es"),
        ("pt-BR", "pt"),
        ("pt-PT", "pt"),
        ("ru-RU", "ru"),
        ("vi-VN", "vi"),
        ("th-TH", "th"),
        ("id-ID", "id"),
        ("hi-IN", "hi"),
        ("fil-PH", "fil"),
        ("tl-PH", "fil"),
        (" EN-CA ", "en-ca"),
        ("yue-Hant", "yue-hant"),
        ("zz", "zz"),
    ],
)
def test_start_normalizes_only_deliberate_language_aliases(value, expected):
    assert StartMessage(type="start", language=value).language == expected


@pytest.mark.parametrize(
    "language",
    [
        "",
        "   ",
        "e",
        "english",
        "zh_CN",
        "ja--jp",
        "-en",
        "en-",
        "en-1",
        "zh en",
        "en\nus",
        "en/gb",
        "zh-hans-cn-extra-long",
        True,
        123,
        [],
        {},
    ],
)
def test_malformed_language_fails_invalid_start_before_provider_factory(language):
    factory = Factory()
    with service(factory) as (client, _):
        with client.websocket_connect("/v1/asr") as ws:
            ws.send_json({"type": "start", "language": language})
            error = ws.receive_json()
            assert error["type"] == "error"
            assert error["code"] == "invalid_start"
            assert error["confirmed_text"] == ""
            with pytest.raises(WebSocketDisconnect) as closed:
                ws.receive_json()
            assert closed.value.code == 1008
    assert factory.created == []


@pytest.mark.parametrize(
    "provider,model,language",
    [
        ("qwen", "qwen3-asr-flash-realtime", "nl"),
        ("funasr", "fun-asr-realtime", "yue"),
        ("funasr", "fun-asr-realtime-2026-02-28", "ko"),
        ("funasr", "fun-asr-realtime-2026-02-28", "fil"),
        ("qwen", "qwen3-asr-flash-realtime", "en-CA"),
        ("funasr", "fun-asr-realtime", "zz"),
    ],
)
def test_unsupported_hint_fails_invalid_language_before_provider_factory(provider, model, language):
    factory = Factory()
    fallback = "qwen" if provider == "funasr" else "funasr"
    with service(
        factory,
        maas_api_key="fake-maas-key",
        fallback_provider=fallback,
        **{f"{provider}_model": model},
    ) as (client, metrics):
        with client.websocket_connect("/v1/asr") as ws:
            ws.send_json({"type": "start", "provider": provider, "language": language})
            error = ws.receive_json()
            assert error["type"] == "error"
            assert error["code"] == "invalid_language"
            assert error["confirmed_text"] == ""
            with pytest.raises(WebSocketDisconnect) as closed:
                ws.receive_json()
            assert closed.value.code == 1008
    assert factory.created == []
    assert metrics.upstream_retries.value(provider) == 0
    assert metrics.fallbacks.value(provider, fallback) == 0


@pytest.mark.parametrize("provider", ["funasr", "qwen"])
@pytest.mark.parametrize("language,expected", [("zz", "zz"), (" EN-CA ", "en-ca")])
def test_unknown_models_accept_syntactically_valid_direct_hints(provider, language, expected):
    upstream = Fake()
    factory = Factory(**{provider: [upstream]})
    with service(factory, **{f"{provider}_model": "custom-asr"}) as (client, _):
        with client.websocket_connect("/v1/asr") as ws:
            ws.send_json({"type": "start", "provider": provider, "language": language})
            ready = ws.receive_json()
            assert ready["type"] == "ready"
            assert ready["provider"] == provider
            ws.send_json({"type": "stop"})
            assert ws.receive_json()["type"] == "done"
    assert factory.created == [provider]
    assert upstream.calls[0][1] == expected
