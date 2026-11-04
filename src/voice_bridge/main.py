import asyncio
import hmac
from contextlib import asynccontextmanager, suppress
from pathlib import Path
from typing import Annotated

from fastapi import Depends, FastAPI, HTTPException, Request, Response, Security, WebSocket
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, PlainTextResponse
from fastapi.security import APIKeyHeader
from fastapi.staticfiles import StaticFiles
from starlette.datastructures import Headers

from voice_bridge.config import Settings
from voice_bridge.languages import language_metadata
from voice_bridge.metrics import Metrics
from voice_bridge.providers.base import ASRProvider, ProviderError
from voice_bridge.session import ProviderFactory, TranscriptionSession


async def drain(app: FastAPI, grace_seconds: float, abort: asyncio.Event | None = None) -> None:
    """Stop taking new sessions and wait for running ones to finish, up to the grace period.

    Called on SIGTERM before uvicorn closes connections. Running sessions keep their audio
    flowing; new connections get a `shutting_down` error and /readyz turns unready so the
    load balancer stops sending traffic. `abort` ends the wait early (a forced exit).
    """
    app.state.draining = True
    app.state.metrics.draining.set(1)
    waits = [asyncio.ensure_future(app.state.idle.wait())]
    if abort is not None:
        waits.append(asyncio.ensure_future(abort.wait()))
    try:
        await asyncio.wait(waits, timeout=grace_seconds, return_when=asyncio.FIRST_COMPLETED)
    finally:
        for wait in waits:
            wait.cancel()
        await asyncio.gather(*waits, return_exceptions=True)


def create_app(
    settings: Settings | None = None, provider_factory: ProviderFactory | None = None
) -> FastAPI:
    config = settings or Settings()
    metrics = Metrics()

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        config.validate_startup()
        yield

    app = FastAPI(title="声桥（VoiceBridge）", version="0.1.0", lifespan=lifespan)
    app.state.settings = config
    app.state.connections = 0
    app.state.draining = False
    app.state.metrics = metrics
    app.state.idle = asyncio.Event()  # set while no session is running
    app.state.idle.set()

    def default_factory(provider: str) -> ASRProvider:
        keys = {"funasr": config.maas_api_key, "qwen": config.dashscope_api_key}
        if provider in keys and not keys[provider].get_secret_value():
            raise ProviderError("provider_unavailable", "This speech provider is not configured.")
        if provider == "funasr":
            from voice_bridge.providers.funasr import FunAsrProvider

            return FunAsrProvider(
                api_key=config.maas_api_key.get_secret_value(),
                url=config.funasr_url,
                model=config.funasr_model,
                connect_timeout=config.connect_timeout_seconds,
                send_timeout=config.send_timeout_seconds,
                silence_duration_ms=config.vad_silence_duration_ms,
            )
        from voice_bridge.providers.qwen import QwenProvider

        if provider != "qwen":
            raise ValueError("Unsupported provider")
        return QwenProvider(
            api_key=config.dashscope_api_key.get_secret_value(),
            url=config.qwen_url,
            connect_timeout=config.connect_timeout_seconds,
            send_timeout=config.send_timeout_seconds,
            silence_duration_ms=config.vad_silence_duration_ms,
        )

    web_root = Path(__file__).with_name("web")
    app.mount("/demo/assets", StaticFiles(directory=web_root), name="demo-assets")

    @app.get("/demo", include_in_schema=False)
    async def demo() -> FileResponse:
        return FileResponse(web_root / "index.html", headers={"Cache-Control": "no-store"})

    @app.get("/guide", response_class=HTMLResponse, include_in_schema=False)
    def frontend_guide() -> str:
        return (
            '<!doctype html><html lang="zh-CN"><head><meta charset="utf-8">'
            '<meta name="viewport" content="width=device-width,initial-scale=1,viewport-fit=cover">'
            '<link rel="icon" href="data:,"></head><body>'
            + (web_root / "guide.html").read_text(encoding="utf-8")
            + "</body></html>"
        )

    api_key_header = APIKeyHeader(name="X-API-Key", auto_error=False)

    def authorized(headers: Headers) -> bool:
        values = headers.getlist("x-api-key")
        expected = config.service_api_key.get_secret_value()
        return (
            bool(expected)
            and len(values) == 1
            and hmac.compare_digest(values[0].encode("utf-8"), expected.encode("ascii"))
        )

    def require_api_key(
        request: Request, _key: Annotated[str | None, Security(api_key_header)]
    ) -> None:
        if not authorized(request.headers):
            raise HTTPException(status_code=401, detail="Invalid or missing API key")

    @app.get("/v1/asr/capabilities", dependencies=[Depends(require_api_key)])
    async def capabilities() -> dict:
        providers = [
            {
                "id": "qwen",
                "model": config.qwen_model,
                "configured": bool(config.dashscope_api_key.get_secret_value()),
                **language_metadata("qwen", config.qwen_model),
            },
            {
                "id": "funasr",
                "model": config.funasr_model,
                "configured": bool(config.maas_api_key.get_secret_value()),
                **language_metadata("funasr", config.funasr_model),
            },
        ]
        return {
            "providers": providers,
            "default_provider": next((p["id"] for p in providers if p["configured"]), None),
            "audio": {"encoding": "pcm_s16le", "sample_rate": 16000, "channels": 1},
            "max_session_seconds": config.max_session_seconds,
            "max_audio_seconds": config.max_audio_seconds,
        }

    @app.get("/healthz")
    async def health() -> dict:
        """Liveness: the process is up. Says nothing about capacity or upstream health."""
        return {"status": "ok"}

    @app.get("/readyz")
    async def readiness(response: Response) -> dict:
        """Readiness: 503 while shutting down or at the connection cap.

        The body doubles as a load query for callers that want to queue or degrade.
        """
        draining = app.state.draining
        full = app.state.connections >= config.max_connections
        if draining or full:
            response.status_code = 503
        return {
            "status": "draining" if draining else "full" if full else "ready",
            "active_sessions": app.state.connections,
            "max_sessions": config.max_connections,
        }

    @app.get("/metrics", dependencies=[Depends(require_api_key)], include_in_schema=False)
    async def prometheus() -> PlainTextResponse:
        # Aggregate counters only: no session ids, text or credentials.
        # Metrics readers use the same fixed key as transcription clients.
        return PlainTextResponse(
            metrics.render(), media_type="text/plain; version=0.0.4; charset=utf-8"
        )

    async def turn_away(ws: WebSocket, reason: str, message: str) -> None:
        """Tell the client why it cannot start, so it can queue, retry or degrade."""
        metrics.rejected.inc(reason)
        with suppress(Exception):
            async with asyncio.timeout(config.send_timeout_seconds):
                await ws.accept()
                await ws.send_json(
                    {
                        "type": "error",
                        "session_id": None,
                        "code": reason,
                        "message": message,
                        "confirmed_text": "",
                    }
                )
                await ws.close(code=1013)

    @app.websocket("/v1/asr")
    async def transcribe(ws: WebSocket) -> None:
        # Reject before accepting or reserving capacity, including while draining.
        if not authorized(ws.headers):
            await ws.send_denial_response(
                JSONResponse({"detail": "Invalid or missing API key"}, status_code=401)
            )
            return
        if app.state.draining:
            await turn_away(ws, "shutting_down", "The service is restarting; retry shortly")
            return
        if app.state.connections >= config.max_connections:
            await turn_away(ws, "server_busy", "The service is at capacity; retry shortly")
            return
        app.state.connections += 1
        app.state.idle.clear()
        metrics.active_sessions.inc()
        try:
            await ws.accept()
            session = TranscriptionSession(ws, config, provider_factory or default_factory, metrics)
            await session.run()
        finally:
            app.state.connections -= 1
            metrics.active_sessions.dec()
            if not app.state.connections:
                app.state.idle.set()

    return app


def run() -> None:
    import uvicorn

    settings = Settings()
    settings.validate_startup()
    app = create_app(settings)

    class DrainingServer(uvicorn.Server):
        forced = asyncio.Event()

        def handle_exit(self, sig, frame) -> None:
            super().handle_exit(sig, frame)
            if self.force_exit:  # a second Ctrl-C skips the wait
                self.forced.set()

        async def shutdown(self, sockets=None) -> None:
            await drain(app, settings.shutdown_grace_seconds, self.forced)
            await super().shutdown(sockets)

    config = uvicorn.Config(
        app,
        host=settings.host,
        port=settings.port,
        ws="websockets",
        ws_max_size=32768,
        ws_max_queue=8,
        ws_per_message_deflate=False,
        # Whatever is still open after the drain gets a close frame within this time.
        timeout_graceful_shutdown=10,
    )
    DrainingServer(config).run()
