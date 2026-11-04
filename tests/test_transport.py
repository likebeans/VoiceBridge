"""Both providers use the same direct, redaction-safe connection policy."""

import json
import logging

import pytest
from websockets.asyncio.server import serve

from voice_bridge.providers.base import AudioFormat, Finished, ProviderError
from voice_bridge.providers.funasr import FunAsrProvider, _safe_error_code
from voice_bridge.providers.qwen import QwenProvider


@pytest.mark.parametrize("provider_type", [FunAsrProvider, QwenProvider])
@pytest.mark.parametrize(
    "status,code",
    [
        (401, "upstream_auth_error"),
        (403, "upstream_auth_error"),
        (429, "upstream_rate_limited"),
        (503, "upstream_unavailable"),
        (404, "upstream_rejected"),
    ],
)
async def test_http_rejections_have_safe_distinct_codes(provider_type, status, code):
    async def reject(socket, request):
        return socket.respond(status, "private upstream response local-test-key")

    async def handler(socket):
        pytest.fail("A rejected handshake cannot reach the handler")

    async with serve(handler, "127.0.0.1", 0, process_request=reject) as server:
        port = server.sockets[0].getsockname()[1]
        provider = provider_type(api_key="local-test-key", url=f"ws://127.0.0.1:{port}")
        try:
            with pytest.raises(ProviderError) as error:
                await provider.open(language=None, audio=AudioFormat())
            assert error.value.code == code
            assert error.value.upstream_code == f"HTTP_{status}"
            assert "private" not in str(error.value)
            assert "local-test-key" not in str(error.value)
        finally:
            await provider.close()


async def test_qwen_redirect_is_not_followed():
    reached = []

    async def target(socket):
        reached.append(True)
        await socket.wait_closed()

    async with serve(target, "127.0.0.1", 0) as target_server:
        port = target_server.sockets[0].getsockname()[1]

        async def redirect(socket, request):
            response = socket.respond(302, "moved")
            response.headers["Location"] = f"ws://127.0.0.1:{port}"
            return response

        async with serve(target, "127.0.0.1", 0, process_request=redirect) as server:
            source = server.sockets[0].getsockname()[1]
            provider = QwenProvider(api_key="local-test-key", url=f"ws://127.0.0.1:{source}")
            try:
                with pytest.raises(ProviderError, match="rejected"):
                    await provider.open(language=None, audio=AudioFormat())
            finally:
                await provider.close()
    assert not reached


async def test_qwen_wire_logs_do_not_inherit_root_debug(caplog):
    async def handler(socket):
        await socket.recv()
        for event in [
            {"type": "session.updated"},
            {
                "type": "conversation.item.input_audio_transcription.completed",
                "item_id": "a",
                "transcript": "private spoken words",
            },
            {"type": "session.finished"},
        ]:
            await socket.send(json.dumps(event))
        await socket.wait_closed()

    with caplog.at_level(logging.DEBUG):
        async with serve(handler, "127.0.0.1", 0) as server:
            port = server.sockets[0].getsockname()[1]
            provider = QwenProvider(api_key="local-test-key", url=f"ws://127.0.0.1:{port}")
            try:
                await provider.open(language=None, audio=AudioFormat())
                assert [e async for e in provider.events()][-1] == Finished()
            finally:
                await provider.close()
    logged = "\n".join(r.getMessage() for r in caplog.records if r.name != "websockets.server")
    assert "local-test-key" not in logged
    assert "private spoken words" not in logged


@pytest.mark.parametrize(
    "value", ["local-test-key", "SECRET_CODE", "private\ntext", "a" * 100, 42, {}]
)
def test_only_known_upstream_codes_are_retained(value):
    assert _safe_error_code(value) == "unrecognized"


def test_recognized_vendor_codes_are_retained_without_a_message():
    assert _safe_error_code("Throttling.User") == "Throttling.User"
    assert _safe_error_code(None) is None
