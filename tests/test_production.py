"""Behavior added for production use: early audio, retry/fallback, busy signal, drain, metrics."""

import asyncio
import logging
from contextlib import contextmanager

import pytest
from conftest import AUTH_HEADERS, TEST_SERVICE_API_KEY
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from voice_bridge.config import Settings
from voice_bridge.main import create_app, drain
from voice_bridge.metrics import Counter, Gauge, Histogram, Metrics
from voice_bridge.providers.base import Finished, ProviderError, Transcript


class Fake:
    """Scriptable provider that records the order of the calls it receives."""

    def __init__(self, *, open_error=None, open_delay=0.0, finish_events=None, send_events=None):
        self.open_error = open_error
        self.open_delay = open_delay
        self.finish_events = [Finished()] if finish_events is None else finish_events
        self.send_events = send_events or []
        self.queue = asyncio.Queue()
        self.calls: list[tuple] = []
        self.closed = False
        self.open_cancelled = False

    async def open(self, *, language, audio):
        self.calls.append(("open", language))
        try:
            if self.open_delay:
                await asyncio.sleep(self.open_delay)
        except asyncio.CancelledError:
            self.open_cancelled = True
            raise
        if self.open_error is not None:
            raise self.open_error

    async def send_audio(self, audio):
        self.calls.append(("audio", audio))
        for event in self.send_events:
            await self.queue.put(event)

    async def finish(self):
        self.calls.append(("finish",))
        for event in self.finish_events:
            await self.queue.put(event)

    async def events(self):
        while True:
            event = await self.queue.get()
            if isinstance(event, Exception):
                raise event
            yield event

    async def close(self):
        self.closed = True


class Factory:
    """Hands out the scripted providers per provider name, in order."""

    def __init__(self, **scripted: list[Fake]):
        self.scripted = scripted
        self.created: list[str] = []

    def __call__(self, name):
        self.created.append(name)
        queue = self.scripted.get(name, [])
        if not queue:
            raise AssertionError(f"unexpected provider {name}")
        item = queue.pop(0)
        if isinstance(item, Exception):
            raise item
        return item


def make_settings(**kwargs):
    return Settings(
        _env_file=None,
        service_api_key=TEST_SERVICE_API_KEY,
        dashscope_api_key="fake-dashscope-key",
        **kwargs,
    )


@contextmanager
def service(factory, **kwargs):
    app = create_app(make_settings(**kwargs), factory)
    with TestClient(app, headers=AUTH_HEADERS) as client:
        yield client, app.state.metrics
    assert app.state.connections == 0


PCM = b"\x01\x02" * 160  # 320 bytes = 10 ms


def test_audio_sent_before_ready_is_kept_and_replayed_in_order():
    provider = Fake(open_delay=0.15)
    with service(Factory(qwen=[provider])) as (client, _):
        with client.websocket_connect("/v1/asr") as ws:
            ws.send_json({"type": "start"})
            for index in range(3):
                ws.send_bytes(bytes([index + 1, 0]) * 160)
            ws.send_json({"type": "stop"})
            assert ws.receive_json()["type"] == "ready"
            done = ws.receive_json()
    assert done["type"] == "done"
    assert done["audio_seconds"] == pytest.approx(0.03)
    kinds = [call[0] for call in provider.calls]
    assert kinds == ["open", "audio", "finish"]  # replayed before finish, nothing lost
    assert provider.calls[1][1] == b"".join(bytes([i + 1, 0]) * 160 for i in range(3))


def test_replay_is_split_into_chunks_no_larger_than_the_advertised_limit():
    provider = Fake(open_delay=0.15)
    with service(Factory(qwen=[provider]), max_audio_chunk_bytes=1000) as (client, _):
        with client.websocket_connect("/v1/asr") as ws:
            ws.send_json({"type": "start"})
            ws.send_bytes(b"\x00\x00" * 400)  # 800 bytes
            ws.send_bytes(b"\x00\x00" * 400)
            ws.send_json({"type": "stop"})
            assert ws.receive_json()["type"] == "ready"
            assert ws.receive_json()["type"] == "done"
    sizes = [len(call[1]) for call in provider.calls if call[0] == "audio"]
    assert sizes == [1000, 600]
    assert all(size % 2 == 0 for size in sizes)


def test_audio_after_ready_still_streams_normally_after_the_replay():
    provider = Fake(open_delay=0.1)
    with service(Factory(qwen=[provider])) as (client, _):
        with client.websocket_connect("/v1/asr") as ws:
            ws.send_json({"type": "start"})
            ws.send_bytes(b"\x01\x00" * 100)
            assert ws.receive_json()["type"] == "ready"
            ws.send_bytes(b"\x02\x00" * 100)
            ws.send_json({"type": "stop"})
            assert ws.receive_json()["type"] == "done"
    audio = b"".join(call[1] for call in provider.calls if call[0] == "audio")
    assert audio == b"\x01\x00" * 100 + b"\x02\x00" * 100


def test_too_much_audio_before_ready_is_an_error_and_cancels_the_open():
    provider = Fake(open_delay=30)
    with service(Factory(qwen=[provider]), pre_ready_buffer_seconds=0.01) as (client, _):
        with client.websocket_connect("/v1/asr") as ws:
            ws.send_json({"type": "start"})
            ws.send_bytes(PCM)  # exactly the 10 ms allowance
            ws.send_bytes(PCM)
            error = ws.receive_json()
            assert error["code"] == "pre_ready_overflow"
    assert provider.open_cancelled and provider.closed


@pytest.mark.parametrize(
    ("early", "code"),
    [
        (b"", "invalid_audio"),
        (b"\x00", "invalid_audio"),
        (b"\x00" * 16386, "message_too_large"),
    ],
)
def test_invalid_audio_is_rejected_even_before_ready(early, code):
    provider = Fake(open_delay=30)
    with service(Factory(qwen=[provider])) as (client, _):
        with client.websocket_connect("/v1/asr") as ws:
            ws.send_json({"type": "start"})
            ws.send_bytes(early)
            assert ws.receive_json()["code"] == code
    assert provider.closed


def test_audio_after_stop_and_junk_control_messages_are_rejected_before_ready():
    with service(Factory(qwen=[Fake(open_delay=30), Fake(open_delay=30)])) as (client, _):
        with client.websocket_connect("/v1/asr") as ws:
            ws.send_json({"type": "start"})
            ws.send_json({"type": "stop"})
            ws.send_bytes(PCM)
            assert ws.receive_json()["code"] == "audio_after_stop"
        with client.websocket_connect("/v1/asr") as ws:
            ws.send_json({"type": "start"})
            ws.send_json({"type": "start"})  # a second start is not a valid message
            assert ws.receive_json()["code"] == "invalid_message"


def test_early_stop_with_no_audio_finishes_cleanly():
    provider = Fake(open_delay=0.1)
    with service(Factory(qwen=[provider])) as (client, _):
        with client.websocket_connect("/v1/asr") as ws:
            ws.send_json({"type": "start"})
            ws.send_json({"type": "stop"})
            assert ws.receive_json()["type"] == "ready"
            done = ws.receive_json()
    assert done["type"] == "done" and done["audio_seconds"] == 0
    assert [call[0] for call in provider.calls] == ["open", "finish"]


def test_disconnect_while_opening_releases_the_upstream():
    provider = Fake(open_delay=30)
    with service(Factory(qwen=[provider])) as (client, _):
        with client.websocket_connect("/v1/asr") as ws:
            ws.send_json({"type": "start"})
            ws.send_bytes(PCM)
    assert provider.open_cancelled and provider.closed


def test_open_phase_is_bounded_by_the_open_timeout():
    provider = Fake(open_delay=30)
    with service(Factory(qwen=[provider]), open_timeout_seconds=0.1) as (client, metrics):
        with client.websocket_connect("/v1/asr") as ws:
            ws.send_json({"type": "start"})
            assert ws.receive_json()["code"] == "upstream_timeout"
    assert provider.open_cancelled and provider.closed
    assert metrics.sessions.value("qwen", "upstream_timeout") == 1


# --- retry and fallback ----------------------------------------------------------------------


def test_one_fast_retry_recovers_from_a_transient_connection_failure():
    broken = Fake(open_error=ProviderError("upstream_connection_error", "no route"))
    healthy = Fake()
    factory = Factory(qwen=[broken, healthy])
    with service(factory) as (client, metrics):
        with client.websocket_connect("/v1/asr") as ws:
            ws.send_json({"type": "start"})
            ready = ws.receive_json()
            assert ready["type"] == "ready" and "fallback_from" not in ready
            ws.send_json({"type": "stop"})
            assert ws.receive_json()["type"] == "done"
    assert factory.created == ["qwen", "qwen"]
    assert broken.closed  # the failed attempt is released before retrying
    assert metrics.upstream_retries.value("qwen") == 1
    assert metrics.upstream_open.value("qwen", "upstream_connection_error") == 1
    assert metrics.upstream_open.value("qwen", "ok") == 1


def test_only_one_retry_then_the_error_reaches_the_client():
    factory = Factory(
        qwen=[Fake(open_error=ProviderError("upstream_connection_error", "x")) for _ in range(3)]
    )
    with service(factory) as (client, _):
        with client.websocket_connect("/v1/asr") as ws:
            ws.send_json({"type": "start"})
            assert ws.receive_json()["code"] == "upstream_connection_error"
    assert factory.created == ["qwen", "qwen"]


def test_a_timeout_is_not_retried_on_the_same_provider():
    factory = Factory(qwen=[Fake(open_error=ProviderError("upstream_timeout", "slow")), Fake()])
    with service(factory) as (client, _):
        with client.websocket_connect("/v1/asr") as ws:
            ws.send_json({"type": "start"})
            assert ws.receive_json()["code"] == "upstream_timeout"
    assert factory.created == ["qwen"]


def fallback_settings(**kwargs):
    return {"maas_api_key": "fake-maas-key", "fallback_provider": "funasr", **kwargs}


def test_fallback_provider_takes_over_before_the_session_starts():
    factory = Factory(
        qwen=[Fake(open_error=ProviderError("upstream_timeout", "slow"))], funasr=[Fake()]
    )
    with service(factory, **fallback_settings()) as (client, metrics):
        with client.websocket_connect("/v1/asr") as ws:
            ws.send_json({"type": "start", "provider": "qwen", "language": "zh"})
            ready = ws.receive_json()
            assert ready["provider"] == "funasr"
            assert ready["fallback_from"] == "qwen"
            ws.send_json({"type": "stop"})
            assert ws.receive_json()["type"] == "done"
    assert factory.created == ["qwen", "funasr"]
    assert metrics.fallbacks.value("qwen", "funasr") == 1
    assert metrics.sessions.value("funasr", "done") == 1


def test_fallback_also_covers_an_unconfigured_primary():
    factory = Factory(
        qwen=[ProviderError("provider_unavailable", "not configured")], funasr=[Fake()]
    )
    with service(factory, **fallback_settings()) as (client, _):
        with client.websocket_connect("/v1/asr") as ws:
            ws.send_json({"type": "start", "language": "zh"})
            assert ws.receive_json()["provider"] == "funasr"
            ws.send_json({"type": "stop"})
            assert ws.receive_json()["type"] == "done"
    assert factory.created == ["qwen", "funasr"]


@pytest.mark.parametrize("provider", ["qwen", "funasr"])
def test_client_mistakes_do_not_retry_or_trigger_the_fallback(provider):
    fallback = "funasr" if provider == "qwen" else "qwen"
    rejected = Fake(open_error=ProviderError("invalid_language", "bad"))
    factory = Factory(**{provider: [rejected, Fake()], fallback: [Fake()]})
    with service(factory, **fallback_settings(fallback_provider=fallback)) as (client, metrics):
        with client.websocket_connect("/v1/asr") as ws:
            ws.send_json({"type": "start", "provider": provider, "language": "zh"})
            assert ws.receive_json()["code"] == "invalid_language"
    assert factory.created == [provider]
    assert rejected.closed
    assert metrics.upstream_retries.value(provider) == 0
    assert metrics.fallbacks.value(provider, fallback) == 0


def test_when_everything_fails_the_requested_providers_error_is_reported():
    factory = Factory(
        qwen=[Fake(open_error=ProviderError("upstream_timeout", "a"))],
        funasr=[Fake(open_error=ProviderError("upstream_error", "b"))],
    )
    with service(factory, **fallback_settings()) as (client, _):
        with client.websocket_connect("/v1/asr") as ws:
            ws.send_json({"type": "start", "language": "zh"})
            assert ws.receive_json()["code"] == "upstream_timeout"
    assert factory.created == ["qwen", "funasr"]


@pytest.mark.parametrize(
    "provider,model,language",
    [
        ("qwen", "qwen3-asr-flash-realtime", "yue"),
        ("qwen", "qwen3-asr-flash-realtime", "tr"),
        ("funasr", "fun-asr-realtime", "nl"),
        ("funasr", "custom-asr", "zz"),
        ("qwen", "custom-asr", "zz"),
    ],
)
def test_explicit_language_unsupported_by_fallback_keeps_primary_error(provider, model, language):
    fallback = "funasr" if provider == "qwen" else "qwen"
    factory = Factory(
        **{
            provider: [Fake(open_error=ProviderError("upstream_timeout", "slow"))],
            fallback: [Fake()],
        }
    )
    with service(
        factory,
        **fallback_settings(fallback_provider=fallback, **{f"{provider}_model": model}),
    ) as (client, metrics):
        with client.websocket_connect("/v1/asr") as ws:
            ws.send_json({"type": "start", "provider": provider, "language": language})
            assert ws.receive_json()["code"] == "upstream_timeout"
    assert factory.created == [provider]
    assert metrics.fallbacks.value(provider, fallback) == 0


@pytest.mark.parametrize("provider", ["qwen", "funasr"])
@pytest.mark.parametrize("language_fields", [{}, {"language": None}, {"language": " AUTO "}])
def test_auto_cannot_fallback_between_models_with_different_coverage(provider, language_fields):
    fallback = "funasr" if provider == "qwen" else "qwen"
    factory = Factory(
        **{
            provider: [Fake(open_error=ProviderError("upstream_timeout", "slow"))],
            fallback: [Fake()],
        }
    )
    with service(factory, **fallback_settings(fallback_provider=fallback)) as (client, metrics):
        with client.websocket_connect("/v1/asr") as ws:
            ws.send_json({"type": "start", "provider": provider, **language_fields})
            assert ws.receive_json()["code"] == "upstream_timeout"
    assert factory.created == [provider]
    assert metrics.fallbacks.value(provider, fallback) == 0


def test_auto_can_fallback_when_target_preserves_all_source_languages():
    primary = Fake(open_error=ProviderError("upstream_timeout", "slow"))
    fallback = Fake()
    factory = Factory(funasr=[primary], qwen=[fallback])
    with service(
        factory,
        **fallback_settings(fallback_provider="qwen", funasr_model="fun-asr-realtime-2026-02-28"),
    ) as (client, metrics):
        with client.websocket_connect("/v1/asr") as ws:
            ws.send_json({"type": "start", "provider": "funasr"})
            ready = ws.receive_json()
            assert ready["type"] == "ready"
            assert ready["provider"] == "qwen"
            assert ready["fallback_from"] == "funasr"
            ws.send_json({"type": "stop"})
            assert ws.receive_json()["type"] == "done"
    assert factory.created == ["funasr", "qwen"]
    assert primary.calls[0] == fallback.calls[0] == ("open", None)
    assert metrics.fallbacks.value("funasr", "qwen") == 1


@pytest.mark.parametrize("provider", ["qwen", "funasr"])
@pytest.mark.parametrize("language", [None, "zh"])
def test_unknown_model_is_never_a_fallback_target(provider, language):
    fallback = "funasr" if provider == "qwen" else "qwen"
    factory = Factory(
        **{
            provider: [Fake(open_error=ProviderError("upstream_timeout", "slow"))],
            fallback: [Fake()],
        }
    )
    with service(
        factory,
        **fallback_settings(fallback_provider=fallback, **{f"{fallback}_model": "custom-asr"}),
    ) as (client, metrics):
        with client.websocket_connect("/v1/asr") as ws:
            ws.send_json({"type": "start", "provider": provider, "language": language})
            assert ws.receive_json()["code"] == "upstream_timeout"
    assert factory.created == [provider]
    assert metrics.fallbacks.value(provider, fallback) == 0


@pytest.mark.parametrize("provider", ["qwen", "funasr"])
@pytest.mark.parametrize("language", [None, "zh"])
def test_unknown_primary_fallback_requires_supported_explicit_language(provider, language):
    fallback = "funasr" if provider == "qwen" else "qwen"
    factory = Factory(
        **{
            provider: [Fake(open_error=ProviderError("upstream_timeout", "slow"))],
            fallback: [Fake()],
        }
    )
    with service(
        factory,
        **fallback_settings(fallback_provider=fallback, **{f"{provider}_model": "custom-asr"}),
    ) as (client, metrics):
        with client.websocket_connect("/v1/asr") as ws:
            ws.send_json({"type": "start", "provider": provider, "language": language})
            result = ws.receive_json()
            if language is None:
                assert result["code"] == "upstream_timeout"
            else:
                assert result["type"] == "ready"
                assert result["provider"] == fallback
                assert result["fallback_from"] == provider
                ws.send_json({"type": "stop"})
                assert ws.receive_json()["type"] == "done"
    assert factory.created == ([provider] if language is None else [provider, fallback])
    assert metrics.fallbacks.value(provider, fallback) == (0 if language is None else 1)


@pytest.mark.parametrize("provider", ["qwen", "funasr"])
@pytest.mark.parametrize("language", ["fil", " TL ", "tl-PH"])
def test_fallback_converts_canonical_filipino_to_each_provider_wire_code(provider, language):
    fallback_name = "funasr" if provider == "qwen" else "qwen"
    primary = Fake(open_error=ProviderError("upstream_timeout", "slow"))
    fallback = Fake()
    factory = Factory(**{provider: [primary], fallback_name: [fallback]})
    with service(factory, **fallback_settings(fallback_provider=fallback_name)) as (
        client,
        metrics,
    ):
        with client.websocket_connect("/v1/asr") as ws:
            ws.send_json({"type": "start", "provider": provider, "language": language})
            ready = ws.receive_json()
            assert ready["type"] == "ready"
            assert ready["provider"] == fallback_name
            assert ready["fallback_from"] == provider
            ws.send_json({"type": "stop"})
            assert ws.receive_json()["type"] == "done"
    assert factory.created == [provider, fallback_name]
    assert primary.calls[0] == ("open", "tl" if provider == "funasr" else "fil")
    assert fallback.calls[0] == ("open", "tl" if fallback_name == "funasr" else "fil")
    assert metrics.fallbacks.value(provider, fallback_name) == 1


def test_requesting_the_fallback_provider_itself_does_not_loop():
    factory = Factory(funasr=[Fake(open_error=ProviderError("upstream_timeout", "slow"))])
    with service(factory, **fallback_settings()) as (client, _):
        with client.websocket_connect("/v1/asr") as ws:
            ws.send_json({"type": "start", "provider": "funasr"})
            assert ws.receive_json()["code"] == "upstream_timeout"
    assert factory.created == ["funasr"]


def test_fallback_needs_a_key_for_that_provider():
    settings = make_settings(fallback_provider="funasr")
    with pytest.raises(ValueError, match="FALLBACK_PROVIDER"):
        settings.validate_provider_config()
    assert make_settings(fallback_provider="  ").fallback_provider is None
    with pytest.raises(ValueError):
        make_settings(fallback_provider="openai")


# --- busy signal, readiness, drain -----------------------------------------------------------


def test_readyz_reports_load_and_goes_unready_at_the_cap():
    with service(Factory(qwen=[Fake()]), max_connections=1) as (client, _):
        assert client.get("/readyz").json() == {
            "status": "ready",
            "active_sessions": 0,
            "max_sessions": 1,
        }
        with client.websocket_connect("/v1/asr"):
            full = client.get("/readyz")
            assert full.status_code == 503
            assert full.json()["status"] == "full"
            assert full.json()["active_sessions"] == 1
        assert client.get("/readyz").status_code == 200
        assert client.get("/healthz").status_code == 200  # liveness is unaffected


def test_a_draining_service_refuses_new_sessions_and_reports_unready():
    app = create_app(make_settings(), Factory(qwen=[Fake()]))
    with TestClient(app, headers=AUTH_HEADERS) as client:
        app.state.draining = True
        ready = client.get("/readyz")
        assert ready.status_code == 503 and ready.json()["status"] == "draining"
        assert client.get("/healthz").status_code == 200
        with client.websocket_connect("/v1/asr") as ws:
            assert ws.receive_json()["code"] == "shutting_down"
            with pytest.raises(WebSocketDisconnect) as closed:
                ws.receive_json()
            assert closed.value.code == 1013
        assert app.state.metrics.rejected.value("shutting_down") == 1


def test_busy_rejections_are_counted():
    with service(Factory(qwen=[Fake()]), max_connections=1) as (client, metrics):
        with client.websocket_connect("/v1/asr"):
            with client.websocket_connect("/v1/asr") as refused:
                assert refused.receive_json()["code"] == "server_busy"
        assert metrics.rejected.value("server_busy") == 1


async def test_drain_returns_as_soon_as_the_last_session_ends():
    app = create_app(make_settings(), Factory())
    app.state.idle.clear()
    waiting = asyncio.create_task(drain(app, 30))
    await asyncio.sleep(0.05)
    assert app.state.draining and not waiting.done()
    assert app.state.metrics.draining.value() == 1
    app.state.idle.set()
    async with asyncio.timeout(1):
        await waiting


async def test_drain_gives_up_after_the_grace_period_and_can_be_aborted():
    app = create_app(make_settings(), Factory())
    app.state.idle.clear()
    async with asyncio.timeout(1):
        await drain(app, 0.05)  # sessions never finish: the grace period ends the wait
    abort = asyncio.Event()
    waiting = asyncio.create_task(drain(app, 30, abort))
    await asyncio.sleep(0.05)
    assert not waiting.done()
    abort.set()
    async with asyncio.timeout(1):
        await waiting


# --- metrics -----------------------------------------------------------------------------------


def test_session_metrics_and_endpoint():
    provider = Fake(
        send_events=[Transcript("a", "你好", False)],
        finish_events=[Transcript("a", "你好。", True), Finished()],
    )
    with service(Factory(qwen=[provider])) as (client, metrics):
        with client.websocket_connect("/v1/asr") as ws:
            ws.send_json({"type": "start"})
            assert ws.receive_json()["type"] == "ready"
            ws.send_bytes(b"\x00\x00" * 8000)  # one second in two chunks
            ws.send_bytes(b"\x00\x00" * 8000)
            ws.receive_json()
            ws.send_json({"type": "stop"})
            while ws.receive_json()["type"] != "done":
                pass
        assert metrics.sessions.value("qwen", "done") == 1
        assert metrics.audio_seconds.value("qwen") == pytest.approx(1.0)
        assert metrics.active_sessions.value() == 0
        for histogram in (
            metrics.ready_seconds,
            metrics.first_transcript_seconds,
            metrics.finish_seconds,
            metrics.session_seconds,
        ):
            assert histogram.count("qwen") == 1
        body = client.get("/metrics")
        assert body.headers["content-type"].startswith("text/plain")
        assert 'asr_sessions_total{provider="qwen",outcome="done"} 1' in body.text
        assert "asr_active_sessions 0" in body.text


def test_failed_and_abandoned_sessions_are_recorded_by_outcome():
    with service(Factory(qwen=[Fake(), Fake()])) as (client, metrics):
        with client.websocket_connect("/v1/asr") as ws:
            ws.send_json({"type": "start"})
            ws.receive_json()
            ws.send_bytes(b"\x00")
            assert ws.receive_json()["code"] == "invalid_audio"
        with client.websocket_connect("/v1/asr") as ws:
            ws.send_json({"type": "start"})
            ws.receive_json()
            ws.close()
            # Finish the disconnect exchange before TestClient cancels the ASGI task
            # on context exit. Cancellation is a different (interrupted) outcome.
            with pytest.raises(WebSocketDisconnect):
                ws.receive_json()
        assert metrics.sessions.value("qwen", "invalid_audio") == 1
    assert metrics.sessions.value("qwen", "client_disconnected") == 1


def test_a_mid_session_provider_failure_is_logged_with_its_sanitized_code(caplog):
    provider = Fake(
        finish_events=[ProviderError("upstream_error", "failed", upstream_code="Throttling.User")]
    )
    with caplog.at_level(logging.WARNING, logger="voice_bridge.session"):
        with service(Factory(qwen=[provider])) as (client, _):
            with client.websocket_connect("/v1/asr") as ws:
                ws.send_json({"type": "start"})
                ws.receive_json()
                ws.send_json({"type": "stop"})
                error = ws.receive_json()
    assert error["code"] == "upstream_error"
    assert "Throttling.User" not in str(error)  # the vendor code stays server-side
    messages = [record.getMessage() for record in caplog.records]
    assert any("upstream_error" in m and "upstream_code=Throttling.User" in m for m in messages)


def test_metrics_primitives_render_prometheus_text():
    counter = Counter("jobs_total", "Jobs.", ("kind",))
    counter.inc('a"b\\c\nd')
    counter.inc("x", amount=2)
    with pytest.raises(ValueError):
        counter.inc("x", amount=-1)
    with pytest.raises(ValueError):
        counter.inc()  # wrong label count
    text = "\n".join(counter.render())
    assert "# TYPE jobs_total counter" in text
    assert 'jobs_total{kind="a\\"b\\\\c\\nd"} 1' in text
    assert 'jobs_total{kind="x"} 2' in text

    gauge = Gauge("level", "Level.")
    assert "\n".join(gauge.render()).endswith("level 0")  # present before first use
    gauge.inc()
    gauge.inc()
    gauge.dec()
    assert gauge.value() == 1

    histogram = Histogram("wait_seconds", "Wait.", buckets=(1, 5))
    for value in (0.5, 1, 3, 9):
        histogram.observe(value)
    lines = "\n".join(histogram.render())
    assert 'wait_seconds_bucket{le="1"} 2' in lines  # bounds are inclusive
    assert 'wait_seconds_bucket{le="5"} 3' in lines
    assert 'wait_seconds_bucket{le="+Inf"} 4' in lines
    assert "wait_seconds_sum 13.5" in lines
    assert "wait_seconds_count 4" in lines


@pytest.mark.parametrize(
    "code", ["upstream_auth_error", "upstream_rate_limited", "upstream_rejected"]
)
def test_permanent_or_rate_limit_rejections_do_not_retry(code):
    factory = Factory(qwen=[Fake(open_error=ProviderError(code, "rejected")), Fake()])
    with service(factory) as (client, metrics):
        with client.websocket_connect("/v1/asr") as ws:
            ws.send_json({"type": "start"})
            assert ws.receive_json()["code"] == code
    assert factory.created == ["qwen"]
    assert metrics.upstream_retries.value("qwen") == 0


def test_each_attempt_timeout_leaves_time_for_fallback():
    primary, fallback = Fake(open_delay=30), Fake()
    with service(
        Factory(qwen=[primary], funasr=[fallback]),
        connect_timeout_seconds=0.05,
        open_timeout_seconds=1,
        **fallback_settings(),
    ) as (client, _):
        with client.websocket_connect("/v1/asr") as ws:
            ws.send_json({"type": "start", "language": "zh"})
            ws.send_bytes(PCM)
            ws.send_json({"type": "stop"})
            assert ws.receive_json()["provider"] == "funasr"
            assert ws.receive_json()["type"] == "done"
    assert primary.closed and primary.open_cancelled
    assert not any(call[0] == "audio" for call in primary.calls)
    assert b"".join(call[1] for call in fallback.calls if call[0] == "audio") == PCM


def test_midstream_failure_never_retries_or_falls_back():
    primary = Fake(send_events=[ProviderError("upstream_closed", "closed")])
    factory = Factory(qwen=[primary, Fake()], funasr=[Fake()])
    with service(factory, **fallback_settings()) as (client, _):
        with client.websocket_connect("/v1/asr") as ws:
            ws.send_json({"type": "start", "language": "zh"})
            ws.receive_json()
            ws.send_bytes(PCM)
            assert ws.receive_json()["code"] == "upstream_closed"
    assert factory.created == ["qwen"]


async def test_audio_completing_after_wait_snapshot_is_not_lost(monkeypatch):
    from voice_bridge.protocol import StartMessage
    from voice_bridge.session import TranscriptionSession

    release = asyncio.Event()

    class Socket:
        async def receive(self):
            await release.wait()
            return {"type": "websocket.receive", "bytes": PCM}

    original_wait = asyncio.wait

    async def racing_wait(tasks, **kwargs):
        done, pending = await original_wait(tasks, **kwargs)
        # Reproduce a receive completing after wait built its `done` snapshot, but
        # before the session task resumes. Cancelling/ignoring it would lose audio.
        release.set()
        if pending:
            await original_wait(pending)
        return done, pending

    monkeypatch.setattr(asyncio, "wait", racing_wait)
    provider = Fake()
    session = TranscriptionSession(Socket(), make_settings(), Factory(qwen=[provider]))
    try:
        early = await session._open(StartMessage(type="start"))
        assert bytes(early.audio) == PCM
    finally:
        await provider.close()


@pytest.mark.parametrize("value", [float("inf"), float("nan")])
def test_nonfinite_limits_cannot_disable_timeouts(value):
    with pytest.raises(ValueError):
        make_settings(open_timeout_seconds=value)


def test_every_metric_has_help_and_type_lines_and_apps_do_not_share_state():
    first, second = Metrics(), Metrics()
    first.rejected.inc("server_busy")
    assert second.rejected.value("server_busy") == 0
    text = first.render()
    for name in ("asr_active_sessions", "asr_sessions_total", "asr_ready_seconds"):
        assert f"# HELP {name} " in text and f"# TYPE {name} " in text
