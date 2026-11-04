import asyncio
from contextlib import contextmanager

import pytest
from conftest import AUTH_HEADERS, TEST_SERVICE_API_KEY
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from voice_bridge.config import Settings
from voice_bridge.main import create_app
from voice_bridge.providers.base import Finished, ProviderError, SegmentOrder, Transcript


class FakeProvider:
    def __init__(self, *, finish_events=None, send_events=None, open_delay=0):
        self.finish_events = [Finished()] if finish_events is None else finish_events
        self.send_events = send_events or []
        self.open_delay = open_delay
        self.events_queue = asyncio.Queue()
        self.calls = []
        self.closed = False
        self.open_cancelled = False
        self.reader_closed = False

    async def open(self, *, language, audio):
        self.calls.append(("open", language, audio))
        try:
            if self.open_delay:
                await asyncio.sleep(self.open_delay)
        except asyncio.CancelledError:
            self.open_cancelled = True
            raise

    async def send_audio(self, audio):
        self.calls.append(("audio", audio))
        for event in self.send_events:
            await self.events_queue.put(event)

    async def finish(self):
        self.calls.append(("finish",))
        for event in self.finish_events:
            await self.events_queue.put(event)

    async def events(self):
        try:
            while True:
                event = await self.events_queue.get()
                if isinstance(event, Exception):
                    raise event
                yield event
        finally:
            self.reader_closed = True

    async def close(self):
        self.closed = True


def settings(**kwargs):
    return Settings(
        _env_file=None,
        service_api_key=TEST_SERVICE_API_KEY,
        dashscope_api_key="fake-dashscope-key",
        **kwargs,
    )


@contextmanager
def service(provider=None, **kwargs):
    provider = provider or FakeProvider()
    created = []

    def factory(name):
        created.append(name)
        return provider

    app = create_app(settings(**kwargs), factory)
    with TestClient(app, headers=AUTH_HEADERS) as client:
        yield client, provider, created
    assert app.state.connections == 0


def start(ws):
    ws.send_json({"type": "start", "language": "zh"})
    assert ws.receive_json()["type"] == "ready"


def test_tail_transcript_is_delivered_before_done_and_audio_before_finish():
    provider = FakeProvider(
        send_events=[Transcript("a", "查一下", False)],
        finish_events=[Transcript("a", "查一下订单。", True), Finished()],
    )
    with service(provider) as (client, _, _):
        with client.websocket_connect("/v1/asr") as ws:
            start(ws)
            ws.send_bytes(b"\x00\x01" * 160)
            partial = ws.receive_json()
            assert partial["full_text"] == "查一下"
            assert not partial["is_final"]
            ws.send_json({"type": "stop"})
            final = ws.receive_json()
            assert final["is_final"]
            assert final["full_text"] == "查一下订单。"
            done = ws.receive_json()
            assert done["type"] == "done"
            assert done["full_text"] == "查一下订单。"
            assert done["audio_seconds"] == 0.01
            with pytest.raises(WebSocketDisconnect) as closed:
                ws.receive_json()
            assert closed.value.code == 1000
    assert [call[0] for call in provider.calls] == ["open", "audio", "finish"]
    assert provider.closed and provider.reader_closed


def test_snapshots_replace_and_out_of_order_finals_preserve_speech_order():
    provider = FakeProvider(
        finish_events=[
            SegmentOrder("a", None),
            SegmentOrder("b", "a"),
            Transcript("a", "我像", False),
            Transcript("a", "我想", False),
            Transcript("b", "查订单。", True),
            Transcript("a", "我想", True),
            Transcript("a", "我想", True),
            Transcript("a", "过期中间结果", False),
            Finished(),
        ]
    )
    with service(provider) as (client, _, _):
        with client.websocket_connect("/v1/asr") as ws:
            start(ws)
            ws.send_json({"type": "stop"})
            results = [ws.receive_json() for _ in range(5)]
            assert [r["full_text"] for r in results[:4]] == [
                "我像",
                "我想",
                "我想查订单。",
                "我想查订单。",
            ]
            assert results[-1]["type"] == "done"
            assert results[-1]["full_text"] == "我想查订单。"


def test_unsupported_provider_does_not_open_upstream():
    with service() as (client, provider, created):
        with client.websocket_connect("/v1/asr") as ws:
            ws.send_json({"type": "start", "provider": "openai"})
            assert ws.receive_json()["code"] == "invalid_start"
        assert not created
        assert not provider.calls


@pytest.mark.parametrize("explicit_provider", [False, True])
@pytest.mark.parametrize("origin", [None, "https://agent.example", "http://localhost:3000"])
def test_authenticated_direct_start_does_not_restrict_origin(origin, explicit_provider):
    with service() as (client, _, created):
        headers = {} if origin is None else {"origin": origin}
        with client.websocket_connect("/v1/asr", headers=headers) as ws:
            message = {"type": "start"}
            if explicit_provider:
                message["provider"] = "qwen"
            ws.send_json(message)
            ready = ws.receive_json()
            assert ready["type"] == "ready"
            assert ready["provider"] == "qwen"
            ws.send_json({"type": "stop"})
            assert ws.receive_json()["type"] == "done"
        assert created == ["qwen"]


@pytest.mark.parametrize(
    ("audio", "expected"),
    [(b"", "invalid_audio"), (b"\x00", "invalid_audio"), (b"\x00" * 16386, "message_too_large")],
)
def test_invalid_audio_is_rejected(audio, expected):
    with service() as (client, provider, _):
        with client.websocket_connect("/v1/asr") as ws:
            start(ws)
            ws.send_bytes(audio)
            assert ws.receive_json()["code"] == expected
    assert provider.closed
    assert not any(call[0] == "audio" for call in provider.calls)


def test_duplicate_stop_is_idempotent_and_finish_timeout_does_not_return_done():
    with service(FakeProvider(finish_events=[]), finish_timeout_seconds=0.05) as (client, p, _):
        with client.websocket_connect("/v1/asr") as ws:
            start(ws)
            ws.send_json({"type": "stop"})
            ws.send_json({"type": "stop"})
            error = ws.receive_json()
            assert error["type"] == "error"
            assert error["code"] == "finish_timeout"
    assert [c[0] for c in p.calls].count("finish") == 1
    assert p.closed and p.reader_closed


def test_audio_after_stop_is_rejected():
    with service(FakeProvider(finish_events=[])) as (client, _, _):
        with client.websocket_connect("/v1/asr") as ws:
            start(ws)
            ws.send_json({"type": "stop"})
            ws.send_bytes(b"\x00\x00")
            assert ws.receive_json()["code"] == "audio_after_stop"


def test_upstream_error_keeps_only_confirmed_text():
    provider = FakeProvider(
        finish_events=[
            Transcript("a", "确认。", True),
            Transcript("b", "待确认", False),
            ProviderError("upstream_error", "Speech recognition failed"),
        ]
    )
    with service(provider) as (client, _, _):
        with client.websocket_connect("/v1/asr") as ws:
            start(ws)
            ws.send_json({"type": "stop"})
            ws.receive_json()
            ws.receive_json()
            error = ws.receive_json()
            assert error["code"] == "upstream_error"
            assert error["confirmed_text"] == "确认。"
    assert provider.closed


def test_finished_with_pending_text_is_an_error():
    provider = FakeProvider(finish_events=[Transcript("a", "未确认", False), Finished()])
    with service(provider) as (client, _, _):
        with client.websocket_connect("/v1/asr") as ws:
            start(ws)
            ws.send_json({"type": "stop"})
            ws.receive_json()
            assert ws.receive_json()["code"] == "incomplete_transcript"


def test_disconnect_cancels_pending_connection():
    provider = FakeProvider(open_delay=60)
    with service(provider) as (client, _, _):
        with client.websocket_connect("/v1/asr") as ws:
            ws.send_json({"type": "start"})
            ws.close()
    assert provider.closed and provider.open_cancelled


def test_idle_timeout_cleans_up_provider():
    with service(idle_timeout_seconds=0.05) as (client, provider, _):
        with client.websocket_connect("/v1/asr") as ws:
            start(ws)
            assert ws.receive_json()["code"] == "idle_timeout"
    assert provider.closed and provider.reader_closed


def test_missing_start_and_invalid_format_never_connect_to_provider():
    with service(start_timeout_seconds=0.05) as (client, _, created):
        with client.websocket_connect("/v1/asr") as ws:
            assert ws.receive_json()["code"] == "start_timeout"
        with client.websocket_connect("/v1/asr") as ws:
            ws.send_json(
                {
                    "type": "start",
                    "audio": {"encoding": "webm", "sample_rate": 48000, "channels": 2},
                }
            )
            assert ws.receive_json()["code"] == "invalid_start"
        assert not created


def test_process_connection_limit_is_released_on_disconnect():
    with service(max_connections=1) as (client, _, _):
        with client.websocket_connect("/v1/asr") as first:
            # Over the limit the client is told why, instead of a bare failed handshake.
            with client.websocket_connect("/v1/asr") as refused:
                error = refused.receive_json()
                assert error["type"] == "error"
                assert error["code"] == "server_busy"
                assert error["confirmed_text"] == ""
                with pytest.raises(WebSocketDisconnect) as closed:
                    refused.receive_json()
                assert closed.value.code == 1013
            start(first)
            first.send_json({"type": "stop"})
            assert first.receive_json()["type"] == "done"
            with pytest.raises(WebSocketDisconnect):
                first.receive_json()
        with client.websocket_connect("/v1/asr") as second:
            start(second)
            second.send_json({"type": "stop"})
            assert second.receive_json()["type"] == "done"


def test_audio_duration_limit_rejects_excess_before_forwarding():
    with service(max_audio_seconds=1) as (client, provider, _):
        with client.websocket_connect("/v1/asr") as ws:
            start(ws)
            ws.send_bytes(b"\x00" * 16000)
            ws.send_bytes(b"\x00" * 16000)
            ws.send_bytes(b"\x00\x00")
            assert ws.receive_json()["code"] == "audio_limit"
    assert sum(len(c[1]) for c in provider.calls if c[0] == "audio") == 32000


def test_recording_limit_is_enforced_even_when_no_stop_arrives():
    with service(max_session_seconds=1, idle_timeout_seconds=5) as (client, provider, _):
        with client.websocket_connect("/v1/asr") as ws:
            start(ws)
            assert ws.receive_json()["code"] == "session_limit"
    assert provider.closed and provider.reader_closed


@pytest.mark.parametrize("draining", [False, True])
def test_disconnect_during_recording_or_drain_releases_provider(draining):
    provider = FakeProvider(finish_events=[])
    with service(provider) as (client, _, _):
        with client.websocket_connect("/v1/asr") as ws:
            start(ws)
            if draining:
                ws.send_json({"type": "stop"})
            ws.close()
    assert provider.closed and provider.reader_closed


@pytest.mark.parametrize("empty_final", [False, True])
def test_committed_segment_must_have_a_final_even_without_partial(empty_final):
    events = [SegmentOrder("a", None), Transcript("a", "确认。", True), SegmentOrder("b", "a")]
    if empty_final:
        events.append(Transcript("b", "", True))
    events.append(Finished())
    with service(FakeProvider(finish_events=events)) as (client, _, _):
        with client.websocket_connect("/v1/asr") as ws:
            start(ws)
            ws.send_json({"type": "stop"})
            while (result := ws.receive_json())["type"] == "transcript":
                pass
            if empty_final:
                assert result["type"] == "done"
                assert result["full_text"] == "确认。"
            else:
                assert result["code"] == "incomplete_transcript"
                assert result["confirmed_text"] == "确认。"


def test_invalid_order_keeps_confirmed_text_and_reports_error():
    events = [
        Transcript("a", "确认。", True),
        SegmentOrder("b", "a"),
        SegmentOrder("a", "b"),
    ]
    with service(FakeProvider(finish_events=events)) as (client, _, _):
        with client.websocket_connect("/v1/asr") as ws:
            start(ws)
            ws.send_json({"type": "stop"})
            while (result := ws.receive_json())["type"] == "transcript":
                pass
            assert result["code"] == "upstream_protocol_error"
            assert result["confirmed_text"] == "确认。"


def test_session_token_endpoint_is_removed():
    with service() as (client, _, _):
        assert client.post("/v1/asr/sessions", json={}).status_code == 404
        schema = client.get("/openapi.json").json()
        assert "/v1/asr/sessions" not in schema["paths"]


def test_oversized_start_is_rejected_before_opening_provider():
    with service(max_control_message_bytes=512) as (client, _, created):
        with client.websocket_connect("/v1/asr") as ws:
            ws.send_json({"type": "start", "language": "a" * 512})
            assert ws.receive_json()["code"] == "message_too_large"
        assert not created
