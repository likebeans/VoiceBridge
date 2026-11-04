"""Real SIGTERM through the installed entry point, with only a local upstream."""

import asyncio
import json
import os
import signal
import socket
import sys
from contextlib import asynccontextmanager
from pathlib import Path

import httpx
import pytest
from conftest import AUTH_HEADERS, TEST_SERVICE_API_KEY
from websockets.asyncio.client import connect
from websockets.asyncio.server import serve
from websockets.exceptions import ConnectionClosed

from voice_bridge.providers.transport import CONNECT_OPTIONS

SOURCE = Path(__file__).resolve().parents[1] / "src"


@asynccontextmanager
async def service_process(tmp_path, upstream_port, grace):
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        port = listener.getsockname()[1]
    command = (
        "import sys; sys.path.insert(0, sys.argv[1]); from voice_bridge.main import run; run()"
    )
    with (tmp_path / "service.log").open("wb") as log:
        process = await asyncio.create_subprocess_exec(
            sys.executable,
            "-c",
            command,
            str(SOURCE),
            cwd=tmp_path,
            env={
                "PATH": os.defpath,
                "HOST": "127.0.0.1",
                "PORT": str(port),
                "SERVICE_API_KEY": TEST_SERVICE_API_KEY,
                "MAAS_BASE_URL": f"http://127.0.0.1:{upstream_port}",
                "MAAS_API_KEY": "local-test-key",
                "SHUTDOWN_GRACE_SECONDS": str(grace),
            },
            stdout=log,
            stderr=log,
        )
        try:
            async with httpx.AsyncClient(
                base_url=f"http://127.0.0.1:{port}", trust_env=False
            ) as http:
                async with asyncio.timeout(10):
                    while True:
                        assert process.returncode is None, (tmp_path / "service.log").read_text()
                        try:
                            if (await http.get("/healthz")).status_code == 200:
                                break
                        except httpx.TransportError:
                            pass
                        await asyncio.sleep(0.02)
                yield process, http, f"ws://127.0.0.1:{port}/v1/asr"
        finally:
            if process.returncode is None:
                process.terminate()
            try:
                async with asyncio.timeout(5):
                    await process.wait()
            except TimeoutError:
                process.kill()
                await process.wait()


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX signals")
async def test_sigterm_preserves_active_audio_and_rejects_new_sessions(tmp_path):
    received = bytearray()
    released = asyncio.Event()

    async def gateway(socket):
        await socket.recv()
        await socket.send(json.dumps({"header": {"event": "task-started"}}))
        try:
            async for raw in socket:
                if isinstance(raw, bytes):
                    received.extend(raw)
                else:
                    assert json.loads(raw)["header"]["action"] == "finish-task"
                    await socket.send(json.dumps({"header": {"event": "task-finished"}}))
        finally:
            released.set()

    async with serve(gateway, "127.0.0.1", 0) as upstream:
        async with service_process(tmp_path, upstream.sockets[0].getsockname()[1], 5) as (
            p,
            http,
            url,
        ):
            async with connect(url, additional_headers=AUTH_HEADERS, **CONNECT_OPTIONS) as ws:
                await ws.send(json.dumps({"type": "start", "provider": "funasr"}))
                assert json.loads(await ws.recv())["type"] == "ready"
                await ws.send(b"\x01\x00" * 160)
                p.send_signal(signal.SIGTERM)
                async with asyncio.timeout(3):
                    # A separate process has no shared Event; poll its public readiness.
                    while (await http.get("/readyz")).json()["status"] != "draining":  # noqa: ASYNC110
                        await asyncio.sleep(0.02)
                assert (await http.get("/readyz")).status_code == 503
                assert (await http.get("/healthz")).status_code == 200
                async with connect(
                    url, additional_headers=AUTH_HEADERS, **CONNECT_OPTIONS
                ) as refused:
                    assert json.loads(await refused.recv())["code"] == "shutting_down"
                    with pytest.raises(ConnectionClosed) as error:
                        await refused.recv()
                    assert error.value.rcvd.code == 1013
                await ws.send(b"\x02\x00" * 160)
                await ws.send(json.dumps({"type": "stop"}))
                done = json.loads(await ws.recv())
                assert done["type"] == "done" and done["audio_seconds"] == pytest.approx(0.02)
                await ws.wait_closed()
                assert ws.close_code == 1000
            async with asyncio.timeout(3):
                await released.wait()
                await p.wait()
            # Uvicorn re-raises SIGTERM after a clean shutdown.
            assert p.returncode in {0, -signal.SIGTERM}
    assert received == b"\x01\x00" * 160 + b"\x02\x00" * 160


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX signals")
async def test_sigterm_grace_expiry_bounds_a_stuck_session(tmp_path):
    async def gateway(socket):
        await socket.recv()
        await socket.send(json.dumps({"header": {"event": "task-started"}}))
        await socket.wait_closed()

    async with serve(gateway, "127.0.0.1", 0) as upstream:
        async with service_process(tmp_path, upstream.sockets[0].getsockname()[1], 0.1) as (
            p,
            _,
            url,
        ):
            async with connect(url, additional_headers=AUTH_HEADERS, **CONNECT_OPTIONS) as ws:
                await ws.send(json.dumps({"type": "start", "provider": "funasr"}))
                assert json.loads(await ws.recv())["type"] == "ready"
                p.send_signal(signal.SIGTERM)
                async with asyncio.timeout(3):
                    with pytest.raises(ConnectionClosed):
                        await ws.recv()
                    await p.wait()
                assert ws.close_code == 1012
