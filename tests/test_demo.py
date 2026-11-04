import pytest
from conftest import AUTH_HEADERS, TEST_SERVICE_API_KEY
from fastapi.testclient import TestClient

from voice_bridge.config import Settings
from voice_bridge.main import create_app


@pytest.fixture
def client():
    settings = Settings(
        _env_file=None,
        service_api_key=TEST_SERVICE_API_KEY,
        dashscope_api_key="",
        maas_api_key="private-upstream-test-key",
    )
    with TestClient(create_app(settings)) as test_client:
        yield test_client


def test_capabilities_selects_configured_provider_without_leaking_key(client):
    response = client.get("/v1/asr/capabilities", headers=AUTH_HEADERS)
    assert response.status_code == 200
    data = response.json()
    assert data["default_provider"] == "funasr"
    assert {provider["id"]: provider["configured"] for provider in data["providers"]} == {
        "qwen": False,
        "funasr": True,
    }
    assert data["audio"] == {"encoding": "pcm_s16le", "sample_rate": 16000, "channels": 1}
    expected_codes = {
        "funasr": (
            "zh en ja ko vi th id ms fil hi ar fr de es pt ru it nl sv da fi no el "
            "pl cs hu ro bg hr sk"
        ),
        "qwen": (
            "zh yue en ja de ko ru fr pt ar it es hi id th tr uk vi cs da fil fi is ms no pl sv"
        ),
    }
    expected_models = {"funasr": "fun-asr-realtime", "qwen": "qwen3-asr-flash-realtime"}
    for provider in data["providers"]:
        assert provider["model"] == expected_models[provider["id"]]
        languages = provider["languages"]
        expected = set(expected_codes[provider["id"]].split())
        assert {language["code"] for language in languages} == expected
        assert len(languages) == len(expected)
        assert {"code": "fil", "name": "菲律宾语"} in languages
        assert all(language["name"] for language in languages)
        assert provider["auto_detect"] is True
        assert provider["language_support_known"] is True
    assert TEST_SERVICE_API_KEY not in response.text
    assert "private-upstream-test-key" not in response.text
    assert "maas_base_url" not in response.text


@pytest.mark.parametrize(
    "provider,model,expected",
    [
        ("funasr", "fun-asr-realtime-2026-02-28", {"zh", "en", "ja"}),
        ("funasr", "fun-asr-realtime-2026-02-27", set()),
        ("funasr", "fun-asr-realtime-latest", set()),
        ("qwen", "qwen3-asr-flash-realtime-2026-02-28", set()),
        ("qwen", "custom-asr", set()),
        ("qwen", "fun-asr-realtime", set()),
    ],
)
def test_capabilities_match_exact_configured_model(provider, model, expected):
    settings = Settings(
        _env_file=None,
        service_api_key=TEST_SERVICE_API_KEY,
        dashscope_api_key="fake-dashscope-key",
        maas_api_key="private-upstream-test-key",
        **{f"{provider}_model": model},
    )
    with TestClient(create_app(settings)) as client:
        response = client.get("/v1/asr/capabilities", headers=AUTH_HEADERS)
    assert response.status_code == 200
    advertised = next(item for item in response.json()["providers"] if item["id"] == provider)
    assert advertised["model"] == model
    assert advertised["configured"] is True
    assert {language["code"] for language in advertised["languages"]} == expected
    assert len(advertised["languages"]) == len(expected)
    assert advertised["auto_detect"] is (True if expected else None)
    assert advertised["language_support_known"] is bool(expected)
    assert "fake-dashscope-key" not in response.text
    assert TEST_SERVICE_API_KEY not in response.text
    assert "private-upstream-test-key" not in response.text


@pytest.mark.parametrize(
    "path, marker",
    [
        ("/demo", "开始录音"),
        ("/demo/assets/recorder.js", "RealtimeTranscriber"),
        ("/demo/assets/pcm-worklet.js", "registerProcessor"),
        ("/demo/assets/pcm-resampler.js", "export"),
        ("/guide", "full_text"),
    ],
)
def test_browser_entrypoints_are_served(client, path, marker):
    response = client.get(path)
    assert response.status_code == 200
    assert marker in response.text
    if path.endswith(".js"):
        assert "javascript" in response.headers["content-type"]
    else:
        assert "text/html" in response.headers["content-type"]
    assert TEST_SERVICE_API_KEY not in response.text
    assert "private-upstream-test-key" not in response.text


def test_demo_assets_cannot_serve_environment_file(client):
    assert client.get("/demo/assets/%2e%2e/%2e%2e/.env").status_code == 404
