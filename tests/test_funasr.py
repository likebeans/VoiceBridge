import asyncio
import json
import logging
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager

import pytest
from websockets.asyncio.server import ServerConnection, serve

from voice_bridge.config import Settings
from voice_bridge.providers.base import AudioFormat, Finished, ProviderError, Transcript
from voice_bridge.providers.funasr import FunAsrProvider


def started() -> str:
    return json.dumps({"header": {"task_id": "t", "event": "task-started"}, "payload": {}})


def result(text: str, *, sentence_id=1, end: bool = False, **extra) -> str:
    sentence = {"sentence_id": sentence_id, "text": text, "sentence_end": end, **extra}
    return json.dumps(
        {
            "header": {"task_id": "t", "event": "result-generated"},
            "payload": {"output": {"sentence": sentence}},
        }
    )


FINISHED = json.dumps({"header": {"task_id": "t", "event": "task-finished"}, "payload": {}})


@asynccontextmanager
async def local_provider(
    handler: Callable[[ServerConnection], Awaitable[None]], **options
) -> AsyncIterator[FunAsrProvider]:
    async with serve(handler, "127.0.0.1", 0, close_timeout=0.2, compression=None) as server:
        port = server.sockets[0].getsockname()[1]
        provider = FunAsrProvider(
            api_key="local-test-key",
            url=f"ws://127.0.0.1:{port}/v1/audio/asr-realtime?model=local-test-model",
            model="local-test-model",
            **options,
        )
        try:
            async with asyncio.timeout(3):
                yield provider
        finally:
            await provider.close()


@pytest.mark.parametrize("language,silence_ms", [("zh", 400), (None, 800)])
async def test_open_sends_run_task_and_waits_for_task_started(language, silence_ms):
    received = asyncio.Event()
    ready = asyncio.Event()
    captured = {}

    async def handler(socket):
        captured["path"] = socket.request.path
        captured["authorization"] = socket.request.headers["Authorization"]
        captured["run"] = json.loads(await socket.recv())
        received.set()
        await ready.wait()
        await socket.send(started())
        await socket.wait_closed()

    options = {} if silence_ms == 400 else {"silence_duration_ms": silence_ms}
    async with local_provider(handler, **options) as provider:
        opening = asyncio.create_task(provider.open(language=language, audio=AudioFormat()))
        try:
            await received.wait()
            assert not opening.done()
            ready.set()
            await opening
        finally:
            ready.set()
            if not opening.done():
                opening.cancel()
            await asyncio.gather(opening, return_exceptions=True)

    assert captured["path"] == "/v1/audio/asr-realtime?model=local-test-model"
    assert captured["authorization"] == "Bearer local-test-key"
    run = captured["run"]
    assert run["header"]["action"] == "run-task"
    assert run["header"]["streaming"] == "duplex"
    assert run["header"]["task_id"] and len(run["header"]["task_id"]) == 32
    parameters = {"format": "pcm", "sample_rate": 16000, "max_sentence_silence": silence_ms}
    if language is not None:
        parameters["language_hints"] = [language]
    assert run["payload"] == {
        "task_group": "audio",
        "task": "asr",
        "function": "recognition",
        "model": "local-test-model",
        "parameters": parameters,
        "input": {},
    }


async def test_audio_is_binary_snapshots_are_mapped_and_finish_is_idempotent():
    commands: list = []

    async def handler(socket):
        await socket.recv()
        await socket.send(started())
        commands.append(await socket.recv())
        for message in [
            result(""),  # keepalive before speech: must not produce a segment
            result("你好"),
            result("你好，我想查"),
            result("你好，我想查"),
            result("你好，我想查订单。", end=True),
            result("请帮我", sentence_id=2),
        ]:
            await socket.send(message)
        commands.append(await socket.recv())
        await socket.send(result("请帮我看看。", sentence_id=2, end=True))
        await socket.send(FINISHED)
        async for command in socket:
            commands.append(command)

    async with local_provider(handler) as provider:
        await provider.open(language="zh", audio=AudioFormat())
        await provider.send_audio(b"\x00\x00\xff\x7f\x00\x80")
        events = provider.events()
        assert await anext(events) == Transcript("1", "你好", False)
        assert await anext(events) == Transcript("1", "你好，我想查", False)
        assert await anext(events) == Transcript("1", "你好，我想查", False)
        assert await anext(events) == Transcript("1", "你好，我想查订单。", True)
        assert await anext(events) == Transcript("2", "请帮我", False)
        await asyncio.gather(provider.finish(), provider.finish())
        assert [event async for event in events] == [
            Transcript("2", "请帮我看看。", True),
            Finished(),
        ]
        await provider.finish()

    assert commands[0] == b"\x00\x00\xff\x7f\x00\x80"
    finish = json.loads(commands[1])
    assert finish["header"]["action"] == "finish-task"
    assert finish["header"]["streaming"] == "duplex"
    assert len(commands) == 2


async def test_empty_final_is_preserved_and_unknown_events_are_ignored():
    async def handler(socket):
        await socket.recv()
        await socket.send(started())
        for message in [
            json.dumps({"header": {"event": "future-event"}, "payload": {"new": True}}),
            json.dumps({"header": {"event": "result-generated"}, "payload": {"output": {}}}),
            result("", sentence_id=3, end=True),
            FINISHED,
        ]:
            await socket.send(message)
        await socket.wait_closed()

    async with local_provider(handler) as provider:
        await provider.open(language=None, audio=AudioFormat())
        assert [event async for event in provider.events()] == [
            Transcript("3", "", True),
            Finished(),
        ]


async def test_sentence_id_may_be_a_string():
    async def handler(socket):
        await socket.recv()
        await socket.send(started())
        await socket.send(result("你好。", sentence_id="s-1", end=True))
        await socket.send(FINISHED)
        await socket.wait_closed()

    async with local_provider(handler) as provider:
        await provider.open(language=None, audio=AudioFormat())
        assert [event async for event in provider.events()] == [
            Transcript("s-1", "你好。", True),
            Finished(),
        ]


@pytest.mark.parametrize("before_ready", [False, True])
async def test_task_failed_is_safe_to_display(before_ready):
    async def handler(socket):
        await socket.recv()
        if not before_ready:
            await socket.send(started())
        await socket.send(
            json.dumps(
                {
                    "header": {
                        "event": "task-failed",
                        "error_code": "SECRET_CODE",
                        "error_message": "secret upstream text local-test-key",
                    },
                    "payload": {},
                }
            )
        )
        await socket.wait_closed()

    async with local_provider(handler) as provider:
        with pytest.raises(ProviderError) as caught:
            await provider.open(language=None, audio=AudioFormat())
            await anext(provider.events())
        assert caught.value.code == "upstream_error"
        assert "secret upstream text" not in str(caught.value)
        assert "SECRET_CODE" not in str(caught.value)
        assert "local-test-key" not in str(caught.value)


@pytest.mark.parametrize("close_code", [1000, 1011])
@pytest.mark.parametrize("before_ready", [False, True])
async def test_early_close_is_an_error_even_with_a_normal_close_code(close_code, before_ready):
    async def handler(socket):
        await socket.recv()
        if not before_ready:
            await socket.send(started())
        await socket.close(code=close_code, reason="private upstream reason local-test-key")

    async with local_provider(handler) as provider:
        with pytest.raises(ProviderError) as caught:
            await provider.open(language=None, audio=AudioFormat())
            await anext(provider.events())
        assert caught.value.code == "upstream_closed"
        assert "local-test-key" not in str(caught.value)
        assert "private upstream reason" not in str(caught.value)


@pytest.mark.parametrize(
    "raw",
    [
        "not JSON: private upstream data",
        "[]",
        "null",
        '{"header": {"event": NaN}}',
        '{"header": {"event": 42}}',
        '{"header": {"event": ""}}',
        '{"header": "result-generated"}',
        '{"payload": {}}',
        b'{"header": {"event": "task-finished"}}',
        '{"header": {"event": "result-generated"},'
        ' "payload": {"output": {"sentence": "private upstream data"}}}',
        '{"header": {"event": "result-generated"},'
        ' "payload": {"output": {"sentence": {"sentence_id": 1, "text": 17}}}}',
        '{"header": {"event": "result-generated"},'
        ' "payload": {"output": {"sentence": {"sentence_id": 1, "text": "hi",'
        ' "sentence_end": "yes"}}}}',
        '{"header": {"event": "result-generated"},'
        ' "payload": {"output": {"sentence": {"text": "hi"}}}}',
        '{"header": {"event": "result-generated"},'
        ' "payload": {"output": {"sentence": {"sentence_id": true, "text": "hi"}}}}',
        '{"header": {"event": "result-generated"},'
        ' "payload": {"output": {"sentence": {"sentence_id": " ", "text": "hi"}}}}',
    ],
)
async def test_malformed_frames_and_fields_are_protocol_errors(raw):
    async def handler(socket):
        await socket.recv()
        await socket.send(started())
        await socket.send(raw)
        await socket.wait_closed()

    async with local_provider(handler) as provider:
        await provider.open(language=None, audio=AudioFormat())
        with pytest.raises(ProviderError) as caught:
            await anext(provider.events())
        assert caught.value.code == "upstream_protocol_error"
        assert "private upstream data" not in str(caught.value)


async def test_open_timeout_covers_waiting_for_task_started_and_closes_socket():
    disconnected = asyncio.Event()

    async def handler(socket):
        await socket.recv()
        await socket.wait_closed()
        disconnected.set()

    async with local_provider(handler, connect_timeout=0.1) as provider:
        with pytest.raises(ProviderError) as caught:
            await provider.open(language=None, audio=AudioFormat())
        assert caught.value.code == "upstream_timeout"
        await disconnected.wait()
        await provider.close()


async def test_cancelled_open_releases_connection_and_close_is_repeatable():
    received = asyncio.Event()
    disconnected = asyncio.Event()

    async def handler(socket):
        await socket.recv()
        received.set()
        await socket.wait_closed()
        disconnected.set()

    async with local_provider(handler) as provider:
        opening = asyncio.create_task(provider.open(language=None, audio=AudioFormat()))
        try:
            await received.wait()
            opening.cancel()
            with pytest.raises(asyncio.CancelledError):
                await opening
        finally:
            if not opening.done():
                opening.cancel()
            await asyncio.gather(opening, return_exceptions=True)
        await disconnected.wait()
        await provider.close()
        await provider.close()


async def test_open_timeout_also_covers_websocket_handshake():
    handshake_started = asyncio.Event()
    release_handshake = asyncio.Event()

    async def process_request(socket, request):
        handshake_started.set()
        await release_handshake.wait()
        return socket.respond(503, "local test service unavailable")

    async def handler(socket):
        await socket.wait_closed()

    async with serve(handler, "127.0.0.1", 0, process_request=process_request) as server:
        port = server.sockets[0].getsockname()[1]
        provider = FunAsrProvider(
            api_key="local-test-key",
            url=f"ws://127.0.0.1:{port}/v1/audio/asr-realtime",
            connect_timeout=0.1,
        )
        try:
            async with asyncio.timeout(3):
                with pytest.raises(ProviderError) as caught:
                    await provider.open(language=None, audio=AudioFormat())
                assert handshake_started.is_set()
                assert caught.value.code == "upstream_timeout"
        finally:
            release_handshake.set()
            await provider.close()


async def test_state_and_input_validation():
    async def handler(socket):
        await socket.recv()
        await socket.send(started())
        await socket.wait_closed()

    async with local_provider(handler) as provider:
        with pytest.raises(ProviderError) as caught:
            await provider.send_audio(b"\x00\x00")
        assert caught.value.code == "provider_state_error"
        with pytest.raises(ProviderError) as caught:
            await provider.open(language="zh zh", audio=AudioFormat())
        assert caught.value.code == "invalid_language"
        with pytest.raises(ProviderError) as caught:
            await provider.open(language=None, audio=AudioFormat(sample_rate=8000))
        assert caught.value.code == "invalid_audio_format"
        await provider.open(language=None, audio=AudioFormat())
        for bad in (b"", b"\x00", "text"):
            with pytest.raises(ProviderError) as caught:
                await provider.send_audio(bad)
            assert caught.value.code == "invalid_audio"
        await provider.finish()
        with pytest.raises(ProviderError) as caught:
            await provider.send_audio(b"\x00\x00")
        assert caught.value.code == "provider_state_error"
        with pytest.raises(ProviderError) as caught:
            await provider.open(language=None, audio=AudioFormat())
        assert caught.value.code == "provider_state_error"


@pytest.mark.parametrize(
    "kwargs",
    [
        {"api_key": " "},
        {"api_key": "a\nb"},
        {"url": "https://example.com/x"},
        {"url": "not a url"},
        {"model": ""},
        {"connect_timeout": 0},
        {"send_timeout": float("inf")},
        {"silence_duration_ms": 0},
        {"silence_duration_ms": True},
    ],
)
def test_constructor_rejects_invalid_configuration(kwargs):
    options = {"api_key": "k", "url": "wss://example.com/v1/audio/asr-realtime", **kwargs}
    with pytest.raises(ValueError):
        FunAsrProvider(**options)


def test_settings_build_gateway_url_and_validate_keys(monkeypatch):
    for name in ("DASHSCOPE_API_KEY", "MAAS_API_KEY", "MAAS_BASE_URL", "FUNASR_MODEL"):
        monkeypatch.delenv(name, raising=False)
    settings = Settings(
        _env_file=None, maas_base_url="https://gateway.example.com/", maas_api_key="k"
    )
    assert settings.funasr_url == (
        "wss://gateway.example.com/v1/audio/asr-realtime?model=fun-asr-realtime"
    )
    settings.validate_provider_config()
    prefixed = Settings(_env_file=None, maas_base_url="http://localhost:9000/proxy")
    assert (
        prefixed.funasr_url
        == "ws://localhost:9000/proxy/v1/audio/asr-realtime?model=fun-asr-realtime"
    )
    with pytest.raises(ValueError):
        prefixed.validate_provider_config()
    for bad in (
        "wss://example.com",
        "https://u:p@example.com",
        "https://:pw@example.com",  # empty user still carries userinfo
        "https://u@example.com",
        "https://example.com/?x=1",
    ):
        with pytest.raises(ValueError):
            Settings(_env_file=None, maas_base_url=bad)


def test_blank_maas_key_is_not_configured_and_multiline_key_is_rejected(monkeypatch):
    for name in ("DASHSCOPE_API_KEY", "MAAS_API_KEY"):
        monkeypatch.delenv(name, raising=False)
    blank = Settings(_env_file=None, maas_api_key="  \t ")
    assert blank.maas_api_key.get_secret_value() == ""
    with pytest.raises(ValueError):
        blank.validate_provider_config()
    padded = Settings(_env_file=None, maas_api_key=" k ")
    assert padded.maas_api_key.get_secret_value() == "k"
    with pytest.raises(ValueError):
        Settings(_env_file=None, maas_api_key="a\nb")


async def test_empty_snapshot_after_text_clears_the_partial_but_silence_stays_quiet():
    async def handler(socket):
        await socket.recv()
        await socket.send(started())
        for message in [
            result(""),  # silence before speech: nothing to show
            result("temporary"),
            result(""),  # full-text snapshot that retracts the partial
            result("", sentence_id=2),  # another sentence that never spoke
            result("", sentence_id=2, end=True),
            FINISHED,
        ]:
            await socket.send(message)
        await socket.wait_closed()

    async with local_provider(handler) as provider:
        await provider.open(language=None, audio=AudioFormat())
        assert [event async for event in provider.events()] == [
            Transcript("1", "temporary", False),
            Transcript("1", "", False),
            Transcript("2", "", True),
            Finished(),
        ]


async def test_redirects_are_not_followed_so_the_key_stays_on_the_configured_origin():
    leaked = asyncio.Event()

    async def target(socket):
        leaked.set()
        await socket.wait_closed()

    async with serve(target, "127.0.0.1", 0) as other:
        other_port = other.sockets[0].getsockname()[1]

        async def redirect(socket, request):
            response = socket.respond(302, "moved")
            response.headers["Location"] = f"ws://localhost:{other_port}/steal"
            return response

        async def handler(socket):
            await socket.wait_closed()

        async with serve(handler, "127.0.0.1", 0, process_request=redirect) as origin:
            port = origin.sockets[0].getsockname()[1]
            provider = FunAsrProvider(
                api_key="local-test-key", url=f"ws://127.0.0.1:{port}/v1/audio/asr-realtime"
            )
            try:
                async with asyncio.timeout(3):
                    with pytest.raises(ProviderError) as caught:
                        await provider.open(language=None, audio=AudioFormat())
                assert caught.value.code == "upstream_rejected"
                assert not leaked.is_set()
            finally:
                await provider.close()


async def test_root_debug_logging_never_records_the_key_or_upstream_text(caplog):
    async def handler(socket):
        await socket.recv()
        await socket.send(started())
        await socket.send(result("private spoken words", end=True))
        await socket.send(FINISHED)
        await socket.wait_closed()

    with caplog.at_level(logging.DEBUG):
        async with local_provider(handler) as provider:
            await provider.open(language=None, audio=AudioFormat())
            assert [event async for event in provider.events()][-1] == Finished()
    # The in-process fake gateway is a server and logs what it receives; only the client matters.
    client_records = [r for r in caplog.records if r.name != "websockets.server"]
    logged = "\n".join(record.getMessage() for record in client_records)
    assert "local-test-key" not in logged
    assert "private spoken words" not in logged
