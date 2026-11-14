"""Provider selection defaults shared by settings, capabilities, sessions, and clients."""

import asyncio
import importlib.util
import json
import threading
import wave
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest
from conftest import AUTH_HEADERS, TEST_SERVICE_API_KEY
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from voice_bridge.config import Settings
from voice_bridge.main import create_app
from voice_bridge.protocol import StartMessage
from voice_bridge.providers.base import Finished

STREAM_WAV = Path(__file__).resolve().parents[1] / "examples" / "stream_wav.py"


def _load_stream_wav():
    spec = importlib.util.spec_from_file_location("stream_wav_example", STREAM_WAV)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


stream_wav = _load_stream_wav()


def settings(**overrides):
    values = {
        "_env_file": None,
        "service_api_key": TEST_SERVICE_API_KEY,
        "dashscope_api_key": "",
        "maas_api_key": "",
        "fallback_provider": None,
    }
    values.update(overrides)
    return Settings(**values)


class FakeProvider:
    def __init__(self):
        self.closed = False
        self.queue = asyncio.Queue()

    async def open(self, *, language, audio):
        pass

    async def send_audio(self, audio):
        pass

    async def finish(self):
        await self.queue.put(Finished())

    async def events(self):
        event = await self.queue.get()
        if isinstance(event, Exception):
            raise event
        yield event

    async def close(self):
        self.closed = True


@contextmanager
def service(config, provider=None):
    provider = provider or FakeProvider()
    created = []

    def factory(name):
        created.append(name)
        return provider

    app = create_app(config, factory)
    with TestClient(app, headers=AUTH_HEADERS) as client:
        yield client, app, created
    assert app.state.connections == 0
    assert provider.closed


@pytest.mark.parametrize(
    ("dashscope", "maas", "expected"),
    [
        ("", "", None),
        ("fake-qwen-key", "", "qwen"),
        ("", "fake-funasr-key", "funasr"),
        ("fake-qwen-key", "fake-funasr-key", "qwen"),
        (" \t ", "fake-funasr-key", "funasr"),
    ],
)
def test_default_provider_follows_configured_keys(dashscope, maas, expected):
    config = settings(dashscope_api_key=dashscope, maas_api_key=maas)
    assert config.default_provider == expected


def test_start_message_provider_may_be_omitted_or_null():
    assert StartMessage.model_validate({"type": "start"}).provider is None
    assert StartMessage.model_validate({"type": "start", "provider": None}).provider is None
    explicit = StartMessage.model_validate({"type": "start", "provider": "funasr"})
    assert explicit.provider == "funasr"


@pytest.mark.parametrize(
    ("dashscope", "maas", "expected"),
    [
        ("fake-qwen-key", "", "qwen"),
        ("", "fake-funasr-key", "funasr"),
        ("fake-qwen-key", "fake-funasr-key", "qwen"),
    ],
)
def test_capabilities_advertises_the_same_default(dashscope, maas, expected):
    app = create_app(settings(dashscope_api_key=dashscope, maas_api_key=maas))
    with TestClient(app) as client:
        response = client.get("/v1/asr/capabilities", headers=AUTH_HEADERS)
    assert response.status_code == 200
    assert response.json()["default_provider"] == expected


@pytest.mark.parametrize(
    "payload",
    [
        pytest.param({"type": "start"}, id="omitted"),
        pytest.param({"type": "start", "provider": None}, id="null"),
    ],
)
def test_omitted_provider_uses_the_configured_funasr(payload):
    config = settings(maas_api_key="fake-funasr-key")
    with service(config) as (client, app, created):
        with client.websocket_connect("/v1/asr") as ws:
            ws.send_json(payload)
            ready = ws.receive_json()
            assert ready["provider"] == "funasr"
            assert "fallback_from" not in ready
            ws.send_json({"type": "stop"})
            assert ws.receive_json()["type"] == "done"
            with pytest.raises(WebSocketDisconnect) as closed:
                ws.receive_json()
            assert closed.value.code == 1000
    assert created == ["funasr"]
    assert app.state.metrics.sessions.value("funasr", "done") == 1
    assert app.state.metrics.fallbacks.value("qwen", "funasr") == 0


def test_omitted_provider_prefers_qwen_and_is_not_a_fallback():
    config = settings(
        dashscope_api_key="fake-qwen-key",
        maas_api_key="fake-funasr-key",
        fallback_provider="funasr",
    )
    with service(config) as (client, app, created):
        with client.websocket_connect("/v1/asr") as ws:
            ws.send_json({"type": "start"})
            ready = ws.receive_json()
            assert ready["provider"] == "qwen"
            assert "fallback_from" not in ready
            ws.send_json({"type": "stop"})
            assert ws.receive_json()["type"] == "done"
    assert created == ["qwen"]
    assert app.state.metrics.fallbacks.value("qwen", "funasr") == 0
    assert app.state.metrics.sessions.value("qwen", "done") == 1


def test_explicit_unconfigured_provider_is_not_silently_replaced():
    app = create_app(settings(maas_api_key="fake-funasr-key"))
    with TestClient(app, headers=AUTH_HEADERS) as client:
        with client.websocket_connect("/v1/asr") as ws:
            ws.send_json({"type": "start", "provider": "qwen"})
            error = ws.receive_json()
            assert error["type"] == "error"
            assert error["code"] == "provider_unavailable"
            with pytest.raises(WebSocketDisconnect) as closed:
                ws.receive_json()
            assert closed.value.code == 1011
        assert app.state.connections == 0


@pytest.mark.parametrize(
    ("base", "expected"),
    [
        ("http://127.0.0.1:8000", "http://127.0.0.1:8000/v1/asr/capabilities"),
        ("https://voice.example/prefix", "https://voice.example/prefix/v1/asr/capabilities"),
        ("ws://127.0.0.1:8000/v1/asr", "http://127.0.0.1:8000/v1/asr/capabilities"),
    ],
)
def test_capabilities_url_pairs_with_the_transcription_endpoint(base, expected):
    assert stream_wav.capabilities_url(base) == expected


@contextmanager
def http_endpoint(handler):
    server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_address[1]}"
    finally:
        server.shutdown()
        server.server_close()
        thread.join()


@contextmanager
def capabilities_server(provider):
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            body = json.dumps({"default_provider": provider}).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, format, *args):
            pass

    with http_endpoint(Handler) as base_url:
        yield base_url


def test_wav_client_reads_protected_capabilities_and_uses_its_default():
    seen = []

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            seen.append({"path": self.path, "key": self.headers.get("X-API-Key")})
            body = json.dumps({"default_provider": "funasr"}).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, format, *args):
            pass

    with http_endpoint(Handler) as base_url:
        provider = stream_wav.default_provider(base_url, api_key="client-key")
    assert provider == "funasr"
    assert seen == [{"path": "/v1/asr/capabilities", "key": "client-key"}]


async def test_wav_stream_sends_the_service_default_explicitly(tmp_path, monkeypatch):
    class Socket:
        def __init__(self):
            self.sent = []
            self.events = [json.dumps({"type": "done"})]

        async def send(self, data):
            self.sent.append(data)

        async def recv(self):
            return json.dumps({"type": "ready", "max_audio_chunk_bytes": 16384})

        def __aiter__(self):
            return self

        async def __anext__(self):
            if not self.events:
                raise StopAsyncIteration
            return self.events.pop(0)

    class Connection:
        def __init__(self):
            self.ws = Socket()

        async def __aenter__(self):
            return self.ws

        async def __aexit__(self, *exc):
            return False

    path = tmp_path / "sample.wav"
    with wave.open(str(path), "wb") as audio:
        audio.setparams((1, 2, 16000, 0, "NONE", "not compressed"))
        audio.writeframes(b"\x00\x01" * 160)
    connection = Connection()
    monkeypatch.setattr(stream_wav, "DirectConnect", lambda *args, **kwargs: connection)
    with capabilities_server("funasr") as base_url:
        await stream_wav.stream(path, base_url, api_key="client-key")
    assert json.loads(connection.ws.sent[0]) == {
        "type": "start",
        "provider": "funasr",
        "language": None,
    }


def test_wav_client_does_not_invent_a_provider_when_none_is_configured():
    with capabilities_server(None) as base_url:
        with pytest.raises(RuntimeError, match="no configured speech provider"):
            stream_wav.default_provider(base_url, api_key="client-key")


def test_wav_capabilities_requests_refuse_redirects():
    target_hits = []

    class Target(BaseHTTPRequestHandler):
        def do_GET(self):
            target_hits.append(dict(self.headers))
            self.send_response(200)
            self.send_header("Content-Length", "0")
            self.end_headers()

        def log_message(self, format, *args):
            pass

    with http_endpoint(Target) as target_url:

        class Redirect(BaseHTTPRequestHandler):
            def do_GET(self):
                self.send_response(302)
                self.send_header("Location", f"{target_url}/leak")
                self.send_header("Content-Length", "0")
                self.end_headers()

            def log_message(self, format, *args):
                pass

        with http_endpoint(Redirect) as base_url:
            with pytest.raises(RuntimeError, match="HTTP 302"):
                stream_wav.default_provider(base_url, api_key="client-key")
    assert target_hits == []
