"""Behavioral checks for the loopback browser demo proxy."""

import asyncio
import importlib.util
import socket
from contextlib import asynccontextmanager
from pathlib import Path

import httpx
import pytest
import uvicorn
from conftest import TEST_SERVICE_API_KEY
from fastapi import FastAPI
from fastapi.responses import PlainTextResponse, RedirectResponse, Response
from test_end_to_end import running_app
from websockets.asyncio.client import connect
from websockets.asyncio.server import serve
from websockets.exceptions import ConnectionClosed, InvalidStatus

from voice_bridge.config import Settings
from voice_bridge.main import create_app
from voice_bridge.providers.transport import CONNECT_OPTIONS

_PROXY_SPEC = importlib.util.spec_from_file_location(
    "voice_bridge_demo_proxy",
    Path(__file__).resolve().parents[1] / "scripts" / "demo_proxy.py",
)
assert _PROXY_SPEC is not None and _PROXY_SPEC.loader is not None
demo_proxy = importlib.util.module_from_spec(_PROXY_SPEC)
_PROXY_SPEC.loader.exec_module(demo_proxy)

CLIENT_SUPPLIED_KEY = "client_supplied_key_0123456789ABCDEF"


def backend_settings() -> Settings:
    return Settings(
        _env_file=None,
        service_api_key=TEST_SERVICE_API_KEY,
        dashscope_api_key="fake-dashscope-key",
        maas_api_key="",
    )


def upstream_url(port: int) -> str:
    return f"http://127.0.0.1:{port}"


def websocket_url(port: int) -> str:
    return f"ws://127.0.0.1:{port}/v1/asr"


def origin_for(port: int) -> str:
    return f"http://127.0.0.1:{port}"


def bound_listener() -> tuple[socket.socket, int]:
    listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    listener.bind(("127.0.0.1", 0))
    listener.listen()
    return listener, listener.getsockname()[1]


@asynccontextmanager
async def serve_uvicorn(app: FastAPI, listener: socket.socket):
    server = uvicorn.Server(
        uvicorn.Config(
            app,
            log_level="error",
            ws="websockets",
            ws_max_size=demo_proxy.WS_MAX_MESSAGE_BYTES,
            ws_max_queue=demo_proxy.WS_MAX_QUEUE,
            ws_per_message_deflate=False,
            access_log=False,
        )
    )
    task = asyncio.create_task(server.serve(sockets=[listener]))
    try:
        async with asyncio.timeout(5):
            while not server.started:
                if task.done():
                    await task
                    raise RuntimeError("Application failed to start")
                await asyncio.sleep(0.01)
        yield
    finally:
        server.should_exit = True
        try:
            async with asyncio.timeout(5):
                await task
        finally:
            if not task.done():
                task.cancel()
            await asyncio.gather(task, return_exceptions=True)
            listener.close()


@asynccontextmanager
async def running_proxy(upstream: str):
    listener, port = bound_listener()
    try:
        app = demo_proxy.create_proxy_app(upstream, TEST_SERVICE_API_KEY, port=port)
    except BaseException:
        listener.close()
        raise
    async with serve_uvicorn(app, listener):
        yield port


async def test_http_entrypoints_inject_key_and_keep_fixed_routes():
    async with running_app(create_app(backend_settings())) as backend_port:
        async with running_proxy(upstream_url(backend_port)) as proxy_port:
            async with httpx.AsyncClient(
                base_url=upstream_url(proxy_port),
                trust_env=False,
                follow_redirects=False,
            ) as client:
                capabilities = await client.get(
                    "/v1/asr/capabilities",
                    headers={"X-API-Key": CLIENT_SUPPLIED_KEY},
                )
                assert capabilities.status_code == 200
                assert capabilities.json()["audio"]["sample_rate"] == 16000
                assert TEST_SERVICE_API_KEY not in capabilities.text
                assert "access-control-allow-origin" not in capabilities.headers

                entrypoints = (
                    ("/demo", "开始录音", "text/html"),
                    ("/guide", "full_text", "text/html"),
                    ("/demo/assets/demo.css", ":root", "text/css"),
                    ("/demo/assets/demo.js", "capabilities", "javascript"),
                    ("/demo/assets/recorder.js", "RealtimeTranscriber", "javascript"),
                    ("/demo/assets/pcm-worklet.js", "registerProcessor", "javascript"),
                    ("/demo/assets/pcm-resampler.js", "PCMResampler", "javascript"),
                )
                for path, marker, content_type in entrypoints:
                    response = await client.get(path)
                    assert response.status_code == 200, path
                    assert marker in response.text, path
                    assert content_type in response.headers["content-type"], path

                localhost = await client.get("/demo", headers={"Host": f"localhost:{proxy_port}"})
                assert localhost.status_code == 200

                root = await client.get("/")
                assert root.status_code == 307
                assert root.headers["location"] == "/demo"

                for path in (
                    "/metrics",
                    "/healthz",
                    "/v1/asr/unknown",
                    "/docs",
                    "/redoc",
                    "/openapi.json",
                    "/demo/",
                ):
                    assert (await client.get(path)).status_code == 404
                assert (await client.post("/demo")).status_code == 405


async def test_http_rejects_foreign_host_and_wrong_port():
    async with running_proxy(upstream_url(9)) as proxy_port:
        async with httpx.AsyncClient(base_url=upstream_url(proxy_port), trust_env=False) as client:
            foreign = await client.get("/demo", headers={"Host": f"evil.example:{proxy_port}"})
            assert foreign.status_code == 400
            assert foreign.text == "Invalid Host header"
            wrong_port = 1 if proxy_port != 1 else 2
            mismatched = await client.get("/demo", headers={"Host": f"127.0.0.1:{wrong_port}"})
            assert mismatched.status_code == 400


async def test_http_does_not_follow_upstream_redirects():
    target_hits: list[bool] = []
    target = FastAPI()

    @target.get("/leak")
    def leak() -> PlainTextResponse:
        target_hits.append(True)
        return PlainTextResponse("upstream-secret-body")

    async with running_app(target) as target_port:
        redirect = FastAPI()

        @redirect.get("/demo")
        def moved() -> RedirectResponse:
            return RedirectResponse(f"{upstream_url(target_port)}/leak", status_code=302)

        async with running_app(redirect) as redirect_port:
            async with running_proxy(upstream_url(redirect_port)) as proxy_port:
                async with httpx.AsyncClient(
                    base_url=upstream_url(proxy_port), trust_env=False
                ) as client:
                    response = await client.get("/demo")
                assert response.status_code == 502
                assert response.text == "Upstream request failed"
                assert "upstream-secret-body" not in response.text
    assert target_hits == []


async def test_http_rejects_oversized_upstream_response():
    oversized = FastAPI()

    @oversized.get("/v1/asr/capabilities")
    def huge() -> Response:
        return Response(
            b"x" * (demo_proxy.MAX_HTTP_RESPONSE_BYTES + 1),
            media_type="application/json",
        )

    async with running_app(oversized) as upstream_port:
        async with running_proxy(upstream_url(upstream_port)) as proxy_port:
            async with httpx.AsyncClient(
                base_url=upstream_url(proxy_port), trust_env=False
            ) as client:
                response = await client.get("/v1/asr/capabilities")
            assert response.status_code == 502
            assert response.text == "Upstream request failed"


@pytest.mark.parametrize(
    "value",
    [
        "https://127.0.0.1:8000",
        "http://user:pass@127.0.0.1:8000",
        "http://127.0.0.1:8000?token=secret",
        "http://127.0.0.1:8000#fragment",
        "http://192.0.2.10:8000",
        "http://127.0.0.1.evil.example:8000",
        "http://127.0.0.1:8000/api",
        "http://127.0.0.1:0",
    ],
)
def test_upstream_must_be_loopback_http_without_suffix(value):
    with pytest.raises(ValueError):
        demo_proxy.validate_upstream(value)


@pytest.mark.parametrize(
    "value",
    [
        "http://127.0.0.1:8000",
        "http://127.0.0.1:8000/",
        "http://localhost:8000",
        "http://[::1]:8000",
    ],
)
def test_loopback_upstreams_are_accepted(value):
    assert demo_proxy.validate_upstream(value).startswith("http://")


def test_cli_defaults_match_the_documented_command():
    args = demo_proxy.build_parser().parse_args([])
    assert args.upstream == "http://127.0.0.1:8000"
    assert args.port == 8080


async def test_websocket_requires_same_origin_page():
    reached = 0

    async def handler(ws):
        nonlocal reached
        reached += 1
        await ws.send("ok")
        await ws.close(code=1000)

    async with serve(handler, "127.0.0.1", 0) as server:
        upstream_port = server.sockets[0].getsockname()[1]
        async with running_proxy(upstream_url(upstream_port)) as proxy_port:
            url = websocket_url(proxy_port)

            async def expect_denied(origin: str | None) -> None:
                try:
                    connection = await connect(url, origin=origin, **CONNECT_OPTIONS)
                except InvalidStatus as error:
                    assert error.response.status_code == 403
                else:
                    await connection.close()
                    pytest.fail(f"WebSocket handshake accepted origin {origin!r}")

            async with asyncio.timeout(10):
                await expect_denied(None)
                await expect_denied("http://evil.example")
                await expect_denied(origin_for(proxy_port + 1))
            assert reached == 0

            async with asyncio.timeout(5):
                async with connect(url, origin=origin_for(proxy_port), **CONNECT_OPTIONS) as client:
                    assert await client.recv() == "ok"
            assert reached == 1


async def test_websocket_upstream_handshake_error_does_not_leak_headers():
    async def reject(socket, request):
        response = socket.respond(401, "private upstream body")
        response.headers["X-Upstream-Secret"] = "private-upstream-header"
        return response

    async def handler(socket):
        pytest.fail("A rejected upstream handshake must not reach the handler")

    async with serve(handler, "127.0.0.1", 0, process_request=reject) as server:
        upstream_port = server.sockets[0].getsockname()[1]
        async with running_proxy(upstream_url(upstream_port)) as proxy_port:
            async with asyncio.timeout(5):
                with pytest.raises(InvalidStatus) as error:
                    await connect(
                        websocket_url(proxy_port),
                        origin=origin_for(proxy_port),
                        **CONNECT_OPTIONS,
                    )
            assert error.value.response.status_code == 403
            assert "private-upstream-header" not in error.value.response.headers
            assert b"private upstream body" not in error.value.response.body


async def test_websocket_relay_injects_key_and_preserves_frames_and_close_code():
    received: list[object] = []
    captured: dict[str, str | None] = {}

    async def handler(ws):
        captured["api_key"] = ws.request.headers.get("X-API-Key")
        captured["client_secret"] = ws.request.headers.get("X-Client-Secret")
        received.append(await ws.recv())
        await ws.send('{"type":"error","code":"upstream_error"}')
        await ws.send(b"\x00\x01\x02")
        received.append(await ws.recv())
        await ws.send('{"type":"done"}')
        await ws.close(code=4321, reason="upstream done")

    async with serve(handler, "127.0.0.1", 0) as server:
        upstream_port = server.sockets[0].getsockname()[1]
        async with running_proxy(upstream_url(upstream_port)) as proxy_port:
            async with asyncio.timeout(5):
                async with connect(
                    websocket_url(proxy_port),
                    origin=origin_for(proxy_port),
                    additional_headers={
                        "X-API-Key": CLIENT_SUPPLIED_KEY,
                        "X-Client-Secret": "must-not-be-forwarded",
                    },
                    **CONNECT_OPTIONS,
                ) as client:
                    await client.send("start")
                    assert await client.recv() == '{"type":"error","code":"upstream_error"}'
                    assert await client.recv() == b"\x00\x01\x02"
                    await client.send(b"audio")
                    assert await client.recv() == '{"type":"done"}'
                    with pytest.raises(ConnectionClosed) as closed:
                        await client.recv()
                    assert closed.value.code == 4321

    assert received == ["start", b"audio"]
    assert captured["api_key"] == TEST_SERVICE_API_KEY
    assert captured["client_secret"] is None


async def test_client_disconnect_closes_upstream_promptly():
    received = asyncio.Event()
    upstream_closed = asyncio.Event()
    upstream_close_codes: list[int | None] = []

    async def handler(ws):
        try:
            if await ws.recv() == "start":
                received.set()
            await ws.wait_closed()
        finally:
            upstream_close_codes.append(ws.close_code)
            upstream_closed.set()

    async with serve(handler, "127.0.0.1", 0) as server:
        upstream_port = server.sockets[0].getsockname()[1]
        async with running_proxy(upstream_url(upstream_port)) as proxy_port:
            async with asyncio.timeout(5):
                client = await connect(
                    websocket_url(proxy_port),
                    origin=origin_for(proxy_port),
                    **CONNECT_OPTIONS,
                )
                await client.send("start")
                await received.wait()
                await client.close(code=1001, reason="client leaving")
                await upstream_closed.wait()
            assert upstream_close_codes == [1001]
