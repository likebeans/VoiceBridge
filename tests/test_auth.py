"""Fixed service-key auth at settings, startup, HTTP, and WebSocket boundaries."""

import asyncio
import hmac
import json
import logging

import httpx
import pytest
import uvicorn
from conftest import AUTH_HEADERS, TEST_SERVICE_API_KEY
from fastapi.testclient import TestClient
from pydantic import SecretStr, ValidationError
from starlette.testclient import WebSocketDenialResponse
from starlette.websockets import WebSocketDisconnect
from test_end_to_end import running_app
from test_production import Factory, Fake
from websockets.asyncio.client import connect
from websockets.asyncio.server import serve
from websockets.exceptions import InvalidStatus

from voice_bridge.config import Settings
from voice_bridge.main import create_app, run
from voice_bridge.providers.transport import CONNECT_OPTIONS

DENIAL = {"detail": "Invalid or missing API key"}
WRONG_KEY = "wrong_service_key_0123456789ABCDEF_abcd"


def auth_settings(**overrides):
    return Settings(
        _env_file=None,
        **{
            "service_api_key": TEST_SERVICE_API_KEY,
            "dashscope_api_key": "fake-upstream-key",
            "maas_api_key": "",
            "fallback_provider": None,
            **overrides,
        },
    )


@pytest.fixture
def client():
    factory = Factory(qwen=[Fake()])
    with TestClient(create_app(auth_settings(), factory)) as test_client:
        yield test_client
    assert factory.created == []


def assert_ws_denied(client, *, url="/v1/asr", headers=None):
    with pytest.raises(WebSocketDenialResponse) as denied:
        with client.websocket_connect(url, headers=httpx.Headers(headers or {})):
            pytest.fail("Unauthenticated WebSocket was accepted")
    assert denied.value.status_code == 401
    assert denied.value.json() == DENIAL
    assert denied.value.headers["content-type"].startswith("application/json")


def test_service_key_defaults_empty_and_provider_validation_stays_independent(monkeypatch):
    monkeypatch.delenv("SERVICE_API_KEY")
    settings = Settings(
        _env_file=None,
        dashscope_api_key="fake-upstream-key",
        maas_api_key="",
        fallback_provider=None,
    )
    assert isinstance(settings.service_api_key, SecretStr)
    assert settings.service_api_key.get_secret_value() == ""
    settings.validate_provider_config()
    with pytest.raises(ValueError, match="SERVICE_API_KEY"):
        settings.validate_startup()


def test_service_key_trims_configuration_and_masks_diagnostics():
    settings = auth_settings(service_api_key=f" \t{TEST_SERVICE_API_KEY}\n")
    assert isinstance(settings.service_api_key, SecretStr)
    assert settings.service_api_key.get_secret_value() == TEST_SERVICE_API_KEY
    settings.validate_startup()
    for diagnostic in (str(settings), repr(settings), settings.model_dump_json()):
        assert TEST_SERVICE_API_KEY not in diagnostic
        assert "fake-upstream-key" not in diagnostic
    for key in ("a" * 31 + "_", "Z9_-" * 64):
        assert auth_settings(service_api_key=key).service_api_key.get_secret_value() == key
    blank = auth_settings(service_api_key=" \t ")
    assert blank.service_api_key.get_secret_value() == ""
    with pytest.raises(ValueError, match="SERVICE_API_KEY"):
        blank.validate_startup()


@pytest.mark.parametrize(
    "key",
    [
        pytest.param("a" * 31, id="too-short"),
        pytest.param("a" * 257, id="too-long"),
        pytest.param("a" * 32 + "+", id="non-urlsafe"),
        pytest.param("a" * 32 + "é", id="non-ascii"),
        pytest.param("a" * 16 + " " + "b" * 16, id="embedded-space"),
        pytest.param("a" * 16 + "\nb" * 16, id="embedded-newline"),
    ],
)
def test_invalid_service_keys_fail_without_echoing_input(key):
    with pytest.raises(ValidationError, match="SERVICE_API_KEY") as error:
        auth_settings(service_api_key=key)
    assert key not in str(error.value)
    assert repr(key) not in str(error.value)


def test_startup_requires_service_key_before_preserved_provider_validation():
    with pytest.raises(ValueError, match="SERVICE_API_KEY"):
        auth_settings(service_api_key="", dashscope_api_key="").validate_startup()
    with pytest.raises(ValueError, match="DASHSCOPE_API_KEY or MAAS_API_KEY"):
        auth_settings(dashscope_api_key="").validate_startup()
    with pytest.raises(ValueError, match="FALLBACK_PROVIDER"):
        auth_settings(fallback_provider="funasr").validate_startup()
    auth_settings(dashscope_api_key="", maas_api_key="fake-maas-key").validate_startup()


def test_lifespan_fails_without_service_key_even_with_configured_provider():
    app = create_app(auth_settings(service_api_key=""), Factory())
    with pytest.raises(ValueError, match="SERVICE_API_KEY"):
        with TestClient(app):
            pytest.fail("Application started without a service key")


def test_entrypoint_validates_before_starting_server(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("SERVICE_API_KEY", "")
    monkeypatch.setenv("DASHSCOPE_API_KEY", "fake-upstream-key")
    monkeypatch.setenv("MAAS_API_KEY", "")
    monkeypatch.setenv("FALLBACK_PROVIDER", "")
    monkeypatch.setattr(uvicorn.Server, "run", lambda _: pytest.fail("Server must not start"))
    with pytest.raises(ValueError, match="SERVICE_API_KEY"):
        run()


@pytest.mark.parametrize("path", ["/v1/asr/capabilities", "/metrics"])
def test_http_requires_one_exact_service_key_and_accepts_case_insensitive_name(client, path):
    for headers in (
        {},
        {"X-API-Key": WRONG_KEY},
        {"X-API-Key": TEST_SERVICE_API_KEY.swapcase()},
        {"X-API-Key": f" {TEST_SERVICE_API_KEY} "},
        [("X-API-Key", TEST_SERVICE_API_KEY), ("x-api-key", TEST_SERVICE_API_KEY)],
        [("X-API-Key", WRONG_KEY), ("x-api-key", TEST_SERVICE_API_KEY)],
    ):
        response = client.get(path, headers=headers)
        assert response.status_code == 401
        assert response.json() == DENIAL
    assert client.get(path, headers={"x-aPi-kEy": TEST_SERVICE_API_KEY}).status_code == 200


def test_http_does_not_accept_query_json_cookie_or_authorization_key(client):
    response = client.request(
        "GET",
        "/v1/asr/capabilities",
        params={"api_key": TEST_SERVICE_API_KEY, "token": TEST_SERVICE_API_KEY},
        json={"api_key": TEST_SERVICE_API_KEY},
        headers={
            "Authorization": f"Bearer {TEST_SERVICE_API_KEY}",
            "Cookie": f"api_key={TEST_SERVICE_API_KEY}; X-API-Key={TEST_SERVICE_API_KEY}",
        },
    )
    assert response.status_code == 401
    assert response.json() == DENIAL


def test_websocket_requires_single_exact_header_before_accept_or_provider(client):
    for headers in (
        {},
        {"X-API-Key": WRONG_KEY},
        {"X-API-Key": TEST_SERVICE_API_KEY.swapcase()},
        {"X-API-Key": f" {TEST_SERVICE_API_KEY} "},
        [("X-API-Key", TEST_SERVICE_API_KEY), ("x-api-key", TEST_SERVICE_API_KEY)],
        [("X-API-Key", TEST_SERVICE_API_KEY), ("x-api-key", WRONG_KEY)],
    ):
        assert_ws_denied(client, headers=headers)
        assert client.get("/readyz").json()["active_sessions"] == 0
    assert client.app.state.metrics.active_sessions.value() == 0
    assert client.app.state.idle.is_set()


def test_websocket_does_not_accept_url_cookie_or_authorization_key(client):
    assert_ws_denied(
        client,
        url=f"/v1/asr?api_key={TEST_SERVICE_API_KEY}&token={TEST_SERVICE_API_KEY}",
        headers={
            "Authorization": f"Bearer {TEST_SERVICE_API_KEY}",
            "Cookie": f"api_key={TEST_SERVICE_API_KEY}; X-API-Key={TEST_SERVICE_API_KEY}",
        },
    )


async def test_websocket_cannot_authenticate_using_start_json():
    factory = Factory()
    app = create_app(auth_settings(), factory)
    messages = iter(
        [
            {"type": "websocket.connect"},
            {
                "type": "websocket.receive",
                "text": json.dumps({"type": "start", "api_key": TEST_SERVICE_API_KEY}),
            },
        ]
    )
    sent = []

    async def receive():
        return next(messages)

    async def send(message):
        sent.append(message)

    async with asyncio.timeout(1):
        await app(
            {
                "type": "websocket",
                "asgi": {"version": "3.0"},
                "scheme": "ws",
                "path": "/v1/asr",
                "query_string": b"",
                "root_path": "",
                "headers": [],
                "client": ("127.0.0.1", 12345),
                "server": ("127.0.0.1", 8000),
                "subprotocols": [],
                "extensions": {"websocket.http.response": {}},
            },
            receive,
            send,
        )
    assert sent[0]["type"] == "websocket.http.response.start"
    assert sent[0]["status"] == 401
    assert json.loads(sent[1]["body"]) == DENIAL
    assert all(message["type"] != "websocket.accept" for message in sent)
    assert factory.created == []


def test_authentication_precedes_capacity_but_valid_key_preserves_busy_protocol():
    factory = Factory(qwen=[Fake()])
    app = create_app(auth_settings(max_connections=1), factory)
    with TestClient(app) as client:
        with client.websocket_connect("/v1/asr", headers=AUTH_HEADERS.copy()) as active:
            active.send_json({"type": "start"})
            assert active.receive_json()["type"] == "ready"
            assert_ws_denied(client)
            assert client.get("/readyz").json()["active_sessions"] == 1
            assert app.state.metrics.rejected.value("server_busy") == 0
            with client.websocket_connect("/v1/asr", headers=AUTH_HEADERS.copy()) as busy:
                assert busy.receive_json()["code"] == "server_busy"
                with pytest.raises(WebSocketDisconnect) as closed:
                    busy.receive_json()
                assert closed.value.code == 1013
            active.send_json({"type": "stop"})
            assert active.receive_json()["type"] == "done"
        assert client.get("/readyz").json()["active_sessions"] == 0
    assert factory.created == ["qwen"]


def test_authentication_precedes_drain_but_valid_key_preserves_shutdown_protocol(client):
    client.app.state.draining = True
    assert_ws_denied(client)
    assert client.app.state.metrics.rejected.value("shutting_down") == 0
    with client.websocket_connect("/v1/asr", headers=AUTH_HEADERS.copy()) as ws:
        assert ws.receive_json()["code"] == "shutting_down"
        with pytest.raises(WebSocketDisconnect) as closed:
            ws.receive_json()
        assert closed.value.code == 1013
    assert client.get("/readyz").status_code == 503
    assert client.get("/healthz").status_code == 200


def test_header_verification_uses_constant_time_comparison(client, monkeypatch):
    compared = []
    original = hmac.compare_digest

    def compare(left, right):
        compared.append((left, right))
        return original(left, right)

    monkeypatch.setattr(hmac, "compare_digest", compare)
    assert client.get("/metrics", headers=AUTH_HEADERS).status_code == 200
    assert_ws_denied(client, headers={"X-API-Key": WRONG_KEY})
    normalized = [
        tuple(value.decode() if isinstance(value, bytes) else value for value in pair)
        for pair in compared
    ]
    assert (TEST_SERVICE_API_KEY, TEST_SERVICE_API_KEY) in normalized
    assert any(set(pair) == {TEST_SERVICE_API_KEY, WRONG_KEY} for pair in normalized)


def test_health_and_browser_paths_are_anonymous_and_never_embed_key(client):
    for path in (
        "/healthz",
        "/readyz",
        "/demo",
        "/guide",
        "/demo/assets/demo.js",
        "/demo/assets/recorder.js",
    ):
        response = client.get(path)
        assert response.status_code == 200
        assert TEST_SERVICE_API_KEY not in response.text
        assert "fake-upstream-key" not in response.text


def test_openapi_declares_header_key_only_on_protected_http_route(client):
    schema = client.get("/openapi.json").json()
    schemes = schema["components"]["securitySchemes"]
    name = next(
        name
        for name, scheme in schemes.items()
        if scheme == {"type": "apiKey", "in": "header", "name": "X-API-Key"}
    )
    assert {name: []} in schema["paths"]["/v1/asr/capabilities"]["get"]["security"]
    for path in ("/healthz", "/readyz"):
        assert not schema["paths"][path]["get"].get("security")
    assert TEST_SERVICE_API_KEY not in json.dumps(schema)


def test_authentication_responses_and_logs_do_not_echo_keys(client, caplog):
    with caplog.at_level(logging.INFO):
        denied = client.get("/metrics", headers={"X-API-Key": WRONG_KEY})
        assert_ws_denied(client, headers={"X-API-Key": WRONG_KEY})
        capabilities = client.get("/v1/asr/capabilities", headers=AUTH_HEADERS)
        metrics = client.get("/metrics", headers=AUTH_HEADERS)
    assert denied.status_code == 401
    assert capabilities.status_code == metrics.status_code == 200
    diagnostics = caplog.text + denied.text + capabilities.text + metrics.text
    for key in (TEST_SERVICE_API_KEY, WRONG_KEY, "fake-upstream-key"):
        assert key not in diagnostics


async def test_tcp_handshake_is_http_401_and_valid_key_streams_without_upstream_leak():
    upstream_headers = []
    upstream_messages = []
    received_audio = bytearray()

    async def gateway(ws):
        upstream_headers.append(list(ws.request.headers.raw_items()))
        upstream_messages.append(ws.request.path)
        upstream_messages.append(await ws.recv())
        await ws.send(json.dumps({"header": {"event": "task-started"}}))
        async for message in ws:
            if isinstance(message, bytes):
                received_audio.extend(message)
            else:
                upstream_messages.append(message)
                assert json.loads(message)["header"]["action"] == "finish-task"
                await ws.send(json.dumps({"header": {"event": "task-finished"}}))
                return

    async with serve(gateway, "127.0.0.1", 0) as upstream:
        upstream_port = upstream.sockets[0].getsockname()[1]
        app = create_app(
            auth_settings(
                dashscope_api_key="",
                maas_api_key="fake-maas-key",
                maas_base_url=f"http://127.0.0.1:{upstream_port}",
            )
        )
        async with running_app(app) as port:
            url = f"ws://127.0.0.1:{port}/v1/asr"
            for headers in (
                {},
                {"X-API-Key": WRONG_KEY},
                [("X-API-Key", TEST_SERVICE_API_KEY), ("x-api-key", TEST_SERVICE_API_KEY)],
            ):
                with pytest.raises(InvalidStatus) as denied:
                    async with connect(url, additional_headers=headers, **CONNECT_OPTIONS):
                        pytest.fail("Unauthenticated TCP WebSocket was accepted")
                assert denied.value.response.status_code == 401
                assert json.loads(denied.value.response.body) == DENIAL
            assert upstream_headers == []
            assert app.state.connections == 0
            async with connect(
                url,
                additional_headers={"x-aPi-kEy": TEST_SERVICE_API_KEY},
                origin="https://arbitrary.example",
                **CONNECT_OPTIONS,
            ) as ws:
                async with asyncio.timeout(3):
                    await ws.send(json.dumps({"type": "start", "provider": "funasr"}))
                    assert json.loads(await ws.recv())["type"] == "ready"
                    await ws.send(b"\x01\x00" * 160)
                    await ws.send(json.dumps({"type": "stop"}))
                    done = json.loads(await ws.recv())
                    assert done["type"] == "done"
                    assert done["audio_seconds"] == pytest.approx(0.01)
                    await ws.wait_closed()
                    assert ws.close_code == 1000
    assert len(upstream_headers) == 1
    assert dict(upstream_headers[0])["Authorization"] == "Bearer fake-maas-key"
    assert all(name.lower() != "x-api-key" for name, _ in upstream_headers[0])
    assert TEST_SERVICE_API_KEY not in json.dumps(upstream_headers + upstream_messages)
    assert received_audio == b"\x01\x00" * 160
