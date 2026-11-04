import asyncio
import json
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager

import pytest
from websockets.asyncio.server import ServerConnection, serve

from voice_bridge.providers.base import (
    AudioFormat,
    Finished,
    ProviderError,
    SegmentOrder,
    Transcript,
)
from voice_bridge.providers.qwen import QwenProvider


@asynccontextmanager
async def local_provider(
    handler: Callable[[ServerConnection], Awaitable[None]], **options
) -> AsyncIterator[QwenProvider]:
    async with serve(handler, "127.0.0.1", 0, close_timeout=0.2, compression=None) as server:
        port = server.sockets[0].getsockname()[1]
        provider = QwenProvider(
            api_key="local-test-key",
            url=f"ws://127.0.0.1:{port}/realtime?model=local-test-model",
            **options,
        )
        try:
            async with asyncio.timeout(3):
                yield provider
        finally:
            await provider.close()


@pytest.mark.parametrize("language,silence_ms", [("zh", 400), (None, 800)])
async def test_open_sends_configuration_and_waits_for_session_updated(language, silence_ms):
    received = asyncio.Event()
    ready = asyncio.Event()
    captured = {}

    async def handler(socket):
        captured["path"] = socket.request.path
        captured["authorization"] = socket.request.headers["Authorization"]
        captured["beta"] = socket.request.headers["OpenAI-Beta"]
        captured["update"] = json.loads(await socket.recv())
        await socket.send(json.dumps({"type": "session.created"}))
        received.set()
        await ready.wait()
        await socket.send(json.dumps({"type": "session.updated"}))
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

    assert captured["path"] == "/realtime?model=local-test-model"
    assert captured["authorization"] == "Bearer local-test-key"
    assert captured["beta"] == "realtime=v1"
    update = captured["update"]
    assert update["type"] == "session.update"
    assert isinstance(update["event_id"], str) and update["event_id"]
    expected = {
        "modalities": ["text"],
        "input_audio_format": "pcm",
        "sample_rate": 16000,
        "turn_detection": {
            "type": "server_vad",
            "threshold": 0.0,
            "silence_duration_ms": silence_ms,
        },
    }
    if language is not None:
        expected["input_audio_transcription"] = {"language": language}
    assert update["session"] == expected


async def test_audio_snapshots_order_and_final_after_idempotent_finish():
    commands = []

    async def handler(socket):
        await socket.recv()
        await socket.send(json.dumps({"type": "session.updated"}))
        commands.append(json.loads(await socket.recv()))
        for event in [
            {"type": "input_audio_buffer.speech_started", "audio_start_ms": 0},
            {
                "type": "input_audio_buffer.committed",
                "item_id": "first",
                "previous_item_id": None,
            },
            {
                "type": "conversation.item.input_audio_transcription.text",
                "item_id": "first",
                "text": "你",
                "stash": "号",
            },
            {
                "type": "conversation.item.input_audio_transcription.text",
                "item_id": "first",
                "text": "你好",
                "stash": "",
            },
            {
                "type": "input_audio_buffer.committed",
                "item_id": "second",
                "previous_item_id": "first",
            },
        ]:
            await socket.send(json.dumps(event))
        commands.append(json.loads(await socket.recv()))
        for event in [
            {
                "type": "conversation.item.input_audio_transcription.completed",
                "item_id": "first",
                "transcript": "你好。",
            },
            {
                "type": "conversation.item.input_audio_transcription.completed",
                "item_id": "second",
                "transcript": "再见。",
            },
            {"type": "session.finished"},
        ]:
            await socket.send(json.dumps(event))
        async for command in socket:
            commands.append(json.loads(command))

    async with local_provider(handler) as provider:
        await provider.open(language="zh", audio=AudioFormat())
        await provider.send_audio(b"\x00\x00\xff\x7f\x00\x80")
        events = provider.events()
        assert await anext(events) == SegmentOrder("first", None)
        assert await anext(events) == Transcript("first", "你号", False)
        assert await anext(events) == Transcript("first", "你好", False)
        assert await anext(events) == SegmentOrder("second", "first")
        await asyncio.gather(provider.finish(), provider.finish())
        assert [event async for event in events] == [
            Transcript("first", "你好。", True),
            Transcript("second", "再见。", True),
            Finished(),
        ]
        await provider.finish()

    assert [command["type"] for command in commands] == [
        "input_audio_buffer.append",
        "session.finish",
    ]
    assert commands[0]["audio"] == "AAD/fwCA"
    assert all(isinstance(command["event_id"], str) and command["event_id"] for command in commands)
    assert len({command["event_id"] for command in commands}) == len(commands)


@pytest.mark.parametrize("close_code", [1000, 1011])
@pytest.mark.parametrize("before_ready", [False, True])
async def test_early_close_is_an_error_even_with_a_normal_close_code(close_code, before_ready):
    async def handler(socket):
        await socket.recv()
        if not before_ready:
            await socket.send(json.dumps({"type": "session.updated"}))
        await socket.close(code=close_code, reason="private upstream reason local-test-key")

    async with local_provider(handler) as provider:
        with pytest.raises(ProviderError) as caught:
            await provider.open(language=None, audio=AudioFormat())
            await anext(provider.events())
        assert caught.value.code == "upstream_closed"
        assert "local-test-key" not in str(caught.value)
        assert "private upstream reason" not in str(caught.value)


@pytest.mark.parametrize(
    "event_type", ["error", "conversation.item.input_audio_transcription.failed"]
)
@pytest.mark.parametrize("before_ready", [False, True])
async def test_upstream_error_is_safe_to_display(event_type, before_ready):
    async def handler(socket):
        await socket.recv()
        if not before_ready:
            await socket.send(json.dumps({"type": "session.updated"}))
        await socket.send(
            json.dumps(
                {
                    "type": event_type,
                    "error": {"message": "secret upstream text local-test-key"},
                    "item_id": "first",
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
        assert "local-test-key" not in str(caught.value)


@pytest.mark.parametrize(
    "raw",
    [
        "not JSON: private upstream data",
        "[]",
        "null",
        '{"type": NaN}',
        '{"type": 42}',
        '{"type": ""}',
        '{"type": "session.finished", "event_id": null}',
        '{"type": "conversation.item.input_audio_transcription.text",'
        ' "item_id": "first", "text": "hi", "stash": null}',
        '{"type": "conversation.item.input_audio_transcription.completed",'
        ' "item_id": "first", "transcript": 17}',
        '{"type": "conversation.item.input_audio_transcription.completed",'
        ' "item_id": " ", "transcript": "hi"}',
        '{"type": "input_audio_buffer.committed", "item_id": "first"}',
        '{"type": "input_audio_buffer.committed", "item_id": "first", "previous_item_id": []}',
        b'{"type": "session.finished"}',
    ],
)
async def test_malformed_frames_and_fields_are_protocol_errors(raw):
    async def handler(socket):
        await socket.recv()
        await socket.send(json.dumps({"type": "session.updated"}))
        await socket.send(raw)
        await socket.wait_closed()

    async with local_provider(handler) as provider:
        await provider.open(language=None, audio=AudioFormat())
        with pytest.raises(ProviderError) as caught:
            await anext(provider.events())
        assert caught.value.code == "upstream_protocol_error"
        assert "private upstream data" not in str(caught.value)


async def test_open_timeout_covers_waiting_for_session_updated_and_closes_socket():
    disconnected = asyncio.Event()

    async def handler(socket):
        await socket.recv()
        await socket.send(json.dumps({"type": "session.created"}))
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
        provider = QwenProvider(
            api_key="local-test-key",
            url=f"ws://127.0.0.1:{port}/realtime?model=local-test-model",
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


async def test_unknown_events_are_ignored_and_empty_final_is_preserved():
    async def handler(socket):
        await socket.recv()
        await socket.send(json.dumps({"type": "session.updated"}))
        for event in [
            {"type": "future.event", "payload": {"new": True}},
            {
                "type": "conversation.item.input_audio_transcription.completed",
                "item_id": "silent",
                "transcript": "",
            },
            {"type": "session.finished"},
        ]:
            await socket.send(json.dumps(event))
        await socket.wait_closed()

    async with local_provider(handler) as provider:
        await provider.open(language=None, audio=AudioFormat())
        assert [event async for event in provider.events()] == [
            Transcript("silent", "", True),
            Finished(),
        ]
