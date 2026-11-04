"""Exercise the real HTTP/WS server, session layer, provider adapters, and WAV CLI."""

import asyncio
import base64
import json
import os
import socket
import sys
import wave
from contextlib import asynccontextmanager
from pathlib import Path

import httpx
import pytest
import uvicorn
from conftest import AUTH_HEADERS, TEST_SERVICE_API_KEY
from websockets.asyncio.client import connect
from websockets.asyncio.server import serve

from voice_bridge.config import Settings
from voice_bridge.main import create_app
from voice_bridge.providers.funasr import FunAsrProvider
from voice_bridge.providers.qwen import QwenProvider
from voice_bridge.providers.transport import CONNECT_OPTIONS

WAV_SCRIPT = Path(__file__).resolve().parents[1] / "examples" / "stream_wav.py"


@asynccontextmanager
async def running_app(app):
    listener = socket.socket()
    listener.bind(("127.0.0.1", 0))
    listener.listen()
    port = listener.getsockname()[1]
    server = uvicorn.Server(
        uvicorn.Config(
            app,
            log_level="error",
            ws="websockets",
            ws_max_size=32768,
            ws_max_queue=8,
            ws_per_message_deflate=False,
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
        yield port
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


async def test_real_websocket_session_forwards_audio_and_drains_tail():
    upstream_messages = []
    received_audio = bytearray()

    async def qwen(ws):
        await ws.send(json.dumps({"type": "session.created", "session": {"id": "mock"}}))
        update = json.loads(await ws.recv())
        upstream_messages.append(update["type"])
        assert update["session"]["sample_rate"] == 16000
        await ws.send(json.dumps({"type": "session.updated", "session": {"id": "mock"}}))
        async for raw in ws:
            message = json.loads(raw)
            upstream_messages.append(message["type"])
            if message["type"] == "input_audio_buffer.append":
                received_audio.extend(base64.b64decode(message["audio"]))
                await ws.send(
                    json.dumps(
                        {
                            "type": "conversation.item.input_audio_transcription.text",
                            "item_id": "a",
                            "text": "你好",
                            "stash": "，世",
                        }
                    )
                )
            elif message["type"] == "session.finish":
                await asyncio.sleep(0.02)
                for event in [
                    {
                        "type": "input_audio_buffer.committed",
                        "item_id": "a",
                        "previous_item_id": None,
                    },
                    {
                        "type": "conversation.item.input_audio_transcription.completed",
                        "item_id": "a",
                        "transcript": "你好，世界。",
                    },
                    {"type": "session.finished"},
                ]:
                    await ws.send(json.dumps(event))
                return

    async with serve(qwen, "127.0.0.1", 0) as upstream:
        upstream_port = upstream.sockets[0].getsockname()[1]
        settings = Settings(
            _env_file=None,
            service_api_key=TEST_SERVICE_API_KEY,
            dashscope_api_key="fake-key",
        )
        app = create_app(
            settings,
            lambda _: QwenProvider(
                api_key="fake-key",
                url=f"ws://127.0.0.1:{upstream_port}/?model=fake",
            ),
        )
        async with running_app(app) as port:
            async with httpx.AsyncClient(base_url=f"http://127.0.0.1:{port}") as http:
                assert (await http.get("/healthz")).json() == {"status": "ok"}
            async with connect(
                f"ws://127.0.0.1:{port}/v1/asr",
                origin="http://localhost:3000",
                additional_headers=AUTH_HEADERS,
                **CONNECT_OPTIONS,
            ) as ws:
                async with asyncio.timeout(5):
                    await ws.send(json.dumps({"type": "start", "language": "zh"}))
                    assert json.loads(await ws.recv())["type"] == "ready"
                    audio = b"\x00\x01" * 1600
                    await ws.send(audio)
                    partial = json.loads(await ws.recv())
                    assert partial["full_text"] == "你好，世"
                    assert partial["is_final"] is False
                    await ws.send(json.dumps({"type": "stop"}))
                    results = [json.loads(message) async for message in ws]
                    assert results[-2]["is_final"] is True
                    assert results[-1]["type"] == "done"
                    assert results[-1]["full_text"] == "你好，世界。"
                    assert ws.close_code == 1000
    assert bytes(received_audio) == audio
    assert upstream_messages == ["session.update", "input_audio_buffer.append", "session.finish"]


@pytest.mark.parametrize("provider", ["funasr", "qwen"])
@pytest.mark.parametrize(
    "start_fields,funasr_hint,qwen_hint",
    [
        pytest.param({}, None, None, id="omitted"),
        pytest.param({"language": None}, None, None, id="null"),
        pytest.param({"language": " AUTO "}, None, None, id="auto"),
        pytest.param({"language": " JA "}, "ja", "ja", id="normalized"),
        pytest.param({"language": "fil"}, "tl", "fil", id="filipino"),
        pytest.param({"language": " TL-PH "}, "tl", "fil", id="filipino-locale"),
    ],
)
async def test_real_websocket_language_hints_use_provider_wire_codes(
    provider, start_fields, funasr_hint, qwen_hint
):
    requests = []
    finishes = []

    async def gateway(ws):
        requests.append(json.loads(await ws.recv()))
        started = (
            {"header": {"event": "task-started"}}
            if provider == "funasr"
            else {"type": "session.updated"}
        )
        await ws.send(json.dumps(started))
        finishes.append(json.loads(await ws.recv()))
        finished = (
            {"header": {"event": "task-finished"}}
            if provider == "funasr"
            else {"type": "session.finished"}
        )
        await ws.send(json.dumps(finished))
        await ws.wait_closed()

    async with serve(gateway, "127.0.0.1", 0) as upstream:
        upstream_port = upstream.sockets[0].getsockname()[1]
        settings = Settings(
            _env_file=None,
            service_api_key=TEST_SERVICE_API_KEY,
            dashscope_api_key="fake-key",
            fallback_provider=None,
        )

        def factory(name):
            assert name == provider
            if name == "funasr":
                return FunAsrProvider(
                    api_key="fake-key",
                    url=f"ws://127.0.0.1:{upstream_port}/v1/audio/asr-realtime",
                    model=settings.funasr_model,
                )
            return QwenProvider(api_key="fake-key", url=f"ws://127.0.0.1:{upstream_port}")

        async with running_app(create_app(settings, factory)) as port:
            async with connect(
                f"ws://127.0.0.1:{port}/v1/asr",
                additional_headers=AUTH_HEADERS,
                **CONNECT_OPTIONS,
            ) as ws:
                async with asyncio.timeout(3):
                    await ws.send(
                        json.dumps({"type": "start", "provider": provider, **start_fields})
                    )
                    ready = json.loads(await ws.recv())
                    assert ready["type"] == "ready"
                    assert ready["provider"] == provider
                    await ws.send(json.dumps({"type": "stop"}))
                    done = json.loads(await ws.recv())
                    assert done["type"] == "done"
                    await ws.wait_closed()
                    assert ws.close_code == 1000

    assert len(requests) == len(finishes) == 1
    request = requests[0]
    if provider == "funasr":
        assert request["header"]["action"] == "run-task"
        assert request["payload"]["model"] == "fun-asr-realtime"
        parameters = request["payload"]["parameters"]
        if funasr_hint is None:
            assert "language_hints" not in parameters
        else:
            assert parameters["language_hints"] == [funasr_hint]
        assert finishes[0]["header"]["action"] == "finish-task"
    else:
        assert request["type"] == "session.update"
        session = request["session"]
        if qwen_hint is None:
            assert "input_audio_transcription" not in session
        else:
            assert session["input_audio_transcription"] == {"language": qwen_hint}
        assert finishes[0]["type"] == "session.finish"


@pytest.mark.parametrize(
    "language_args,expected_language", [((), None), (("--language", "JA"), "ja")]
)
async def test_wav_cli_respects_small_server_chunk_limit(
    tmp_path, language_args, expected_language
):
    chunk_sizes = []
    updates = []

    async def qwen(ws):
        updates.append(json.loads(await ws.recv()))
        await ws.send(json.dumps({"type": "session.updated"}))
        async for raw in ws:
            event = json.loads(raw)
            if event["type"] == "input_audio_buffer.append":
                chunk_sizes.append(len(base64.b64decode(event["audio"])))
            if event["type"] == "session.finish":
                await ws.send(json.dumps({"type": "session.finished"}))
                return

    path = tmp_path / "sample.wav"
    with wave.open(str(path), "wb") as audio:
        audio.setparams((1, 2, 16000, 0, "NONE", "not compressed"))
        audio.writeframes(b"\x00\x01" * 2000)
    async with serve(qwen, "127.0.0.1", 0) as upstream:
        upstream_port = upstream.sockets[0].getsockname()[1]
        config = Settings(
            _env_file=None,
            service_api_key=TEST_SERVICE_API_KEY,
            dashscope_api_key="fake-key",
            max_audio_chunk_bytes=1024,
        )
        app = create_app(
            config,
            lambda _: QwenProvider(
                api_key="fake-key",
                url=f"ws://127.0.0.1:{upstream_port}/?model=fake",
            ),
        )
        async with running_app(app) as port:
            script = WAV_SCRIPT
            process = await asyncio.create_subprocess_exec(
                sys.executable,
                str(script),
                str(path),
                "--url",
                f"http://127.0.0.1:{port}",
                *language_args,
                env={**os.environ, "SERVICE_API_KEY": TEST_SERVICE_API_KEY},
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            try:
                async with asyncio.timeout(10):
                    stdout, stderr = await process.communicate()
            finally:
                if process.returncode is None:
                    process.kill()
                    await process.wait()
            assert process.returncode == 0, stderr.decode()
            assert json.loads(stdout.splitlines()[-1])["type"] == "done"
            assert TEST_SERVICE_API_KEY.encode() not in stdout + stderr
    assert chunk_sizes == [1024, 1024, 1024, 928]
    assert len(updates) == 1
    assert updates[0]["type"] == "session.update"
    session = updates[0]["session"]
    if expected_language is None:
        assert "input_audio_transcription" not in session
    else:
        assert session["input_audio_transcription"] == {"language": expected_language}


@pytest.mark.parametrize(
    "upstream_code,expected",
    [
        ("ServiceUnavailable", "done"),
        ("Throttling.User", "upstream_rate_limited"),
        ("InvalidApiKey", "upstream_auth_error"),
        ("InvalidParameter.UnsupportedLanguage", "invalid_language"),
    ],
)
async def test_funasr_startup_task_failure_retry_policy(upstream_code, expected):
    attempts = 0
    audio_by_attempt = []

    async def gateway(ws):
        nonlocal attempts
        attempts += 1
        received = bytearray()
        audio_by_attempt.append(received)
        await ws.recv()  # run-task
        if attempts == 1:
            await ws.send(
                json.dumps(
                    {
                        "header": {
                            "event": "task-failed",
                            "error_code": upstream_code,
                            "error_message": "private error body",
                        }
                    }
                )
            )
            await ws.wait_closed()
            return
        await ws.send(json.dumps({"header": {"event": "task-started"}}))
        async for raw in ws:
            if isinstance(raw, bytes):
                received.extend(raw)
            else:
                assert json.loads(raw)["header"]["action"] == "finish-task"
                await ws.send(json.dumps({"header": {"event": "task-finished"}}))

    async with serve(gateway, "127.0.0.1", 0) as upstream:
        port = upstream.sockets[0].getsockname()[1]
        app = create_app(
            Settings(
                _env_file=None,
                service_api_key=TEST_SERVICE_API_KEY,
                maas_api_key="test-key",
                maas_base_url=f"http://127.0.0.1:{port}",
                fallback_provider=None,
            )
        )
        async with running_app(app) as service_port:
            async with connect(
                f"ws://127.0.0.1:{service_port}/v1/asr",
                additional_headers=AUTH_HEADERS,
                **CONNECT_OPTIONS,
            ) as ws:
                async with asyncio.timeout(3):
                    await ws.send(json.dumps({"type": "start", "provider": "funasr"}))
                    await ws.send(b"\x01\x00" * 160)
                    await ws.send(json.dumps({"type": "stop"}))
                    if expected == "done":
                        events = [json.loads(raw) async for raw in ws]
                        assert [e["type"] for e in events] == ["ready", "done"]
                        assert events[-1]["audio_seconds"] == pytest.approx(0.01)
                    else:
                        error = json.loads(await ws.recv())
                        assert error["type"] == "error" and error["code"] == expected
                        await ws.wait_closed()
                        assert ws.close_code == 1011
    assert attempts == (2 if expected == "done" else 1)
    assert audio_by_attempt[0] == b""
    if expected == "done":
        assert audio_by_attempt[1] == b"\x01\x00" * 160
