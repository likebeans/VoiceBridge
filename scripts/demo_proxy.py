#!/usr/bin/env python3
"""Loopback-only proxy that lets a local browser open the VoiceBridge demo.

Browser WebSockets cannot set custom handshake headers, and the demo must not
receive the service key. This process listens only on 127.0.0.1 and adds the key
from ``Settings`` when it forwards capabilities and the transcription WebSocket
to the loopback backend.

It is a development convenience, not a production authentication gateway: it
accepts any request that satisfies the local same-origin checks and must never be
exposed beyond the local machine.
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import socket
import sys
import urllib.request
from collections.abc import Sequence
from contextlib import suppress
from urllib.error import HTTPError
from urllib.parse import urlsplit
from urllib.request import HTTPRedirectHandler, ProxyHandler, build_opener

import uvicorn
from fastapi import FastAPI, Request, WebSocket
from fastapi.responses import PlainTextResponse, RedirectResponse, Response
from starlette.datastructures import Headers
from starlette.websockets import WebSocketDisconnect
from websockets.exceptions import ConnectionClosed

from voice_bridge.config import Settings
from voice_bridge.providers.transport import CONNECT_OPTIONS, WIRE_LOGGER, DirectConnect

LISTEN_HOST = "127.0.0.1"
DEFAULT_UPSTREAM = "http://127.0.0.1:8000"
DEFAULT_PORT = 8080

# The proxy itself is only reachable through loopback names. Requiring one of
# these in Host prevents DNS rebinding from turning an attacker domain into a
# same-origin page that can read capabilities or open transcription sessions.
BROWSER_HOSTNAMES = frozenset({"127.0.0.1", "localhost"})
# The upstream is selected by the person running the script, not by a browser.
UPSTREAM_HOSTNAMES = frozenset({"127.0.0.1", "localhost", "::1"})

DEMO_ASSETS = (
    "demo.css",
    "demo.js",
    "pcm-resampler.js",
    "pcm-worklet.js",
    "recorder.js",
)
PUBLIC_HTTP_PATHS = ("/demo", "/guide", *(f"/demo/assets/{name}" for name in DEMO_ASSETS))

HTTP_TIMEOUT_SECONDS = 5.0
MAX_HTTP_RESPONSE_BYTES = 1_048_576
# Match the service's WebSocket limits so one browser tab cannot buffer freely.
WS_MAX_MESSAGE_BYTES = 32_768
WS_MAX_QUEUE = 8
WS_OPEN_TIMEOUT_SECONDS = 5.0

SAFE_MEDIA_TYPES = frozenset(
    {
        "application/javascript",
        "application/json",
        "text/css",
        "text/html",
        "text/javascript",
        "text/plain",
    }
)


class UpstreamError(Exception):
    """An upstream HTTP request failed in a way that must not reach the client."""


class _NoRedirect(HTTPRedirectHandler):
    """Surface redirects as errors instead of forwarding a request elsewhere."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def validate_upstream(value: str) -> str:
    """Accept only an HTTP origin on a loopback host, without credentials or suffix."""
    try:
        parts = urlsplit(value)
        hostname = parts.hostname
        port = parts.port
    except ValueError as exc:
        raise ValueError("--upstream must be a valid loopback http URL") from exc
    if parts.scheme != "http" or hostname is None:
        raise ValueError("--upstream must be an http URL")
    if parts.username is not None or parts.password is not None:
        raise ValueError("--upstream must not contain credentials")
    if parts.query or parts.fragment:
        raise ValueError("--upstream must not contain a query or fragment")
    if parts.path not in {"", "/"}:
        raise ValueError("--upstream must not contain a path")
    if hostname.lower() not in UPSTREAM_HOSTNAMES:
        raise ValueError("--upstream must point at a loopback host")
    if port == 0:
        raise ValueError("--upstream port must be nonzero")
    normalized_host = f"[{hostname}]" if ":" in hostname else hostname
    netloc = normalized_host if port is None else f"{normalized_host}:{port}"
    return f"http://{netloc}"


def _authority(value: str | None) -> tuple[str, int] | None:
    """Parse a Host-style authority and require an explicit host and valid port."""
    if not value or "@" in value or any(character in value for character in "/?#"):
        return None
    try:
        parts = urlsplit(f"//{value}")
        hostname = parts.hostname
        port = parts.port
    except ValueError:
        return None
    if (
        hostname is None
        or parts.username is not None
        or parts.password is not None
        or parts.path
        or parts.query
        or parts.fragment
    ):
        return None
    return hostname.lower(), 80 if port is None else port


def _single_header(headers: Headers, name: str) -> str | None:
    values = headers.getlist(name)
    return values[0] if len(values) == 1 else None


def _valid_host(headers: Headers, expected_port: int) -> str | None:
    host = _single_header(headers, "host")
    authority = _authority(host)
    if authority is None:
        return None
    hostname, port = authority
    if hostname not in BROWSER_HOSTNAMES or port != expected_port:
        return None
    return host


def _valid_origin(headers: Headers, host: str, expected_port: int) -> bool:
    """Require the browser page origin to be exactly the Host of this WebSocket."""
    origin = _single_header(headers, "origin")
    if not origin:
        return False
    try:
        parts = urlsplit(origin)
        origin_port = parts.port
    except ValueError:
        return False
    if (
        parts.scheme != "http"
        or parts.username is not None
        or parts.password is not None
        or parts.path
        or parts.query
        or parts.fragment
        or parts.hostname is None
    ):
        return False
    host_authority = _authority(host)
    if host_authority is None:
        return False
    hostname, port = host_authority
    return (
        parts.hostname.lower() == hostname
        and (80 if origin_port is None else origin_port) == port
        and port == expected_port
    )


def _fetch_http(url: str, headers: dict[str, str]) -> tuple[str, bytes]:
    """GET one fixed upstream path without proxy settings or redirect following."""
    request = urllib.request.Request(url, headers=headers, method="GET")
    opener = build_opener(ProxyHandler({}), _NoRedirect())
    try:
        with opener.open(request, timeout=HTTP_TIMEOUT_SECONDS) as response:
            if not 200 <= response.status < 300:
                raise UpstreamError("upstream returned a non-success status")
            declared = response.headers.get("Content-Length")
            if declared is not None:
                try:
                    if int(declared) > MAX_HTTP_RESPONSE_BYTES:
                        raise UpstreamError("upstream response is too large")
                except ValueError:
                    pass
            body = response.read(MAX_HTTP_RESPONSE_BYTES + 1)
            if len(body) > MAX_HTTP_RESPONSE_BYTES:
                raise UpstreamError("upstream response is too large")
            return response.headers.get_content_type(), body
    except UpstreamError:
        raise
    except HTTPError as exc:
        # Do not keep the upstream response object (or its headers) alive.
        with suppress(Exception):
            exc.close()
        raise UpstreamError("upstream HTTP request failed") from exc
    except Exception as exc:
        # Never put an upstream exception message, body or headers on the
        # browser response or in logs.
        raise UpstreamError("upstream HTTP request failed") from exc


def _safe_media_type(value: str) -> str:
    normalized = value.lower()
    return normalized if normalized in SAFE_MEDIA_TYPES else "application/octet-stream"


def _sendable_close_code(code: int | None) -> int:
    if code is None:
        return 1000
    if 1000 <= code <= 1003 or 1007 <= code <= 1014 or 3000 <= code <= 4999:
        return code
    return 1011 if code in {1005, 1006, 1015} else 1000


def _close_reason(reason: str | None) -> str:
    if not reason:
        return ""
    return reason.encode("utf-8")[:123].decode("utf-8", "ignore")


async def _close_client(client: WebSocket, code: int | None, reason: str | None) -> None:
    with suppress(Exception):
        await client.close(code=_sendable_close_code(code), reason=_close_reason(reason))


async def _close_upstream(upstream, code: int | None, reason: str | None) -> None:
    with suppress(Exception):
        await upstream.close(code=_sendable_close_code(code), reason=_close_reason(reason))


async def _forward_client_to_upstream(client: WebSocket, upstream) -> None:
    """Relay text and binary frames; direct awaits provide backpressure."""
    try:
        while True:
            event = await client.receive()
            if event["type"] == "websocket.disconnect":
                await _close_upstream(upstream, event.get("code"), event.get("reason"))
                return
            text = event.get("text")
            if text is not None:
                await upstream.send(text)
                continue
            data = event.get("bytes")
            if data is not None:
                await upstream.send(data)
    except WebSocketDisconnect as exc:
        await _close_upstream(upstream, exc.code, exc.reason)
    except ConnectionClosed as exc:
        await _close_client(client, exc.code, exc.reason)
    except asyncio.CancelledError:
        raise
    except Exception:
        await _close_upstream(upstream, 1011, None)
        await _close_client(client, 1011, None)


async def _forward_upstream_to_client(upstream, client: WebSocket) -> None:
    """Relay every upstream frame, including error and done, in arrival order."""
    try:
        async for message in upstream:
            if isinstance(message, str):
                await client.send_text(message)
            else:
                await client.send_bytes(message)
        await _close_client(client, upstream.close_code, upstream.close_reason)
    except ConnectionClosed as exc:
        await _close_client(client, exc.code, exc.reason)
    except asyncio.CancelledError:
        raise
    except Exception:
        await _close_client(client, 1011, None)
        await _close_upstream(upstream, 1011, None)


async def _relay(client: WebSocket, upstream) -> None:
    """Run both directions, then cancel the survivor as soon as one ends."""
    tasks = [
        asyncio.create_task(_forward_client_to_upstream(client, upstream)),
        asyncio.create_task(_forward_upstream_to_client(upstream, client)),
    ]
    try:
        done, pending = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
        for task in pending:
            task.cancel()
        await asyncio.gather(*pending, return_exceptions=True)
        for task in done:
            with suppress(Exception):
                task.result()
    finally:
        for task in tasks:
            if not task.done():
                task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        await _close_upstream(upstream, 1000, None)


def _websocket_url(upstream: str) -> str:
    parts = urlsplit(upstream)
    return f"ws://{parts.netloc}/v1/asr"


def create_proxy_app(upstream: str, service_api_key: str, *, port: int) -> FastAPI:
    """Build the fixed-route proxy. ``port`` is the actual listening port."""
    base = validate_upstream(upstream)
    if not service_api_key:
        raise ValueError("SERVICE_API_KEY is required")
    if not 0 < port <= 65535:
        raise ValueError("port must be between 1 and 65535")

    app = FastAPI(
        title="VoiceBridge demo proxy",
        version="0.1.0",
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
        redirect_slashes=False,
    )

    @app.middleware("http")
    async def strict_host(request: Request, call_next):
        if _valid_host(request.headers, port) is None:
            return PlainTextResponse("Invalid Host header", status_code=400)
        return await call_next(request)

    def http_handler(path: str, protected: bool):
        async def handler() -> Response:
            headers = {"Accept": "application/json"} if protected else {}
            if protected:
                headers["X-API-Key"] = service_api_key
            try:
                async with asyncio.timeout(HTTP_TIMEOUT_SECONDS):
                    media_type, body = await asyncio.to_thread(_fetch_http, base + path, headers)
            except (TimeoutError, UpstreamError):
                return PlainTextResponse(
                    "Upstream request failed",
                    status_code=502,
                    headers={"Cache-Control": "no-store"},
                )
            return Response(
                content=body,
                media_type=_safe_media_type(media_type),
                headers={"Cache-Control": "no-store"},
            )

        return handler

    @app.get("/", include_in_schema=False)
    async def root() -> RedirectResponse:
        return RedirectResponse("/demo", status_code=307)

    for path in PUBLIC_HTTP_PATHS:
        app.add_api_route(
            path,
            http_handler(path, protected=False),
            methods=["GET"],
            include_in_schema=False,
        )
    app.add_api_route(
        "/v1/asr/capabilities",
        http_handler("/v1/asr/capabilities", protected=True),
        methods=["GET"],
        include_in_schema=False,
    )

    @app.websocket("/v1/asr")
    async def transcribe(websocket: WebSocket) -> None:
        host = _valid_host(websocket.headers, port)
        if host is None or not _valid_origin(websocket.headers, host, port):
            await _close_client(websocket, 1008, None)
            return
        try:
            # CONNECT_OPTIONS disables websockets' system-proxy lookup and
            # DirectConnect refuses handshake redirects.
            upstream_socket = await DirectConnect(
                _websocket_url(base),
                additional_headers={"X-API-Key": service_api_key},
                open_timeout=WS_OPEN_TIMEOUT_SECONDS,
                close_timeout=2,
                max_size=WS_MAX_MESSAGE_BYTES,
                max_queue=WS_MAX_QUEUE,
                logger=WIRE_LOGGER,
                **CONNECT_OPTIONS,
            )
        except Exception:
            # Upstream handshake details may contain response headers; expose
            # only the fact that the local proxy could not open the session.
            await _close_client(websocket, 1011, None)
            return
        try:
            await websocket.accept()
        except Exception:
            await _close_upstream(upstream_socket, 1000, None)
            return
        await _relay(websocket, upstream_socket)

    return app


def _port(value: str) -> int:
    try:
        port = int(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("port must be an integer") from exc
    if not 0 <= port <= 65535:
        raise argparse.ArgumentTypeError("port must be between 0 and 65535")
    return port


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Local-only browser demo proxy. Listens on 127.0.0.1 and injects the "
            "SERVICE_API_KEY from Settings/.env. Not a production authentication gateway."
        ),
    )
    parser.add_argument(
        "--upstream",
        default=DEFAULT_UPSTREAM,
        help="loopback backend origin (default: %(default)s)",
    )
    parser.add_argument(
        "--port",
        type=_port,
        default=DEFAULT_PORT,
        help="loopback listening port, 0 picks a free port (default: %(default)s)",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        upstream = validate_upstream(args.upstream)
    except ValueError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    try:
        settings = Settings()
    except Exception:
        print("error: cannot load Settings/.env", file=sys.stderr)
        return 2
    service_api_key = settings.service_api_key.get_secret_value()
    if not service_api_key:
        print("error: SERVICE_API_KEY is required in Settings/.env", file=sys.stderr)
        return 2

    # The upstream key is a secret; keep WebSocket library logs at WARNING.
    logging.getLogger("websockets").setLevel(logging.WARNING)

    listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    try:
        listener.bind((LISTEN_HOST, args.port))
        listener.listen()
    except OSError as exc:
        listener.close()
        print(f"error: cannot listen on {LISTEN_HOST}:{args.port}: {exc}", file=sys.stderr)
        return 2

    actual_port = listener.getsockname()[1]
    try:
        app = create_proxy_app(upstream, service_api_key, port=actual_port)
    except ValueError as exc:
        listener.close()
        print(f"error: {exc}", file=sys.stderr)
        return 2

    print(
        f"VoiceBridge demo proxy: http://{LISTEN_HOST}:{actual_port}/demo -> {upstream}",
        flush=True,
    )
    print(
        "Local development only; this proxy is not a production authentication gateway.",
        flush=True,
    )
    config = uvicorn.Config(
        app,
        host=LISTEN_HOST,
        port=actual_port,
        ws="websockets",
        ws_max_size=WS_MAX_MESSAGE_BYTES,
        ws_max_queue=WS_MAX_QUEUE,
        ws_per_message_deflate=False,
        access_log=False,
        log_level="info",
    )
    try:
        uvicorn.Server(config).run(sockets=[listener])
    finally:
        listener.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
