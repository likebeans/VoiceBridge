#!/usr/bin/env python3
"""Loopback-only DashScope task mock; requires only websockets and the standard library."""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import math
import signal
from dataclasses import dataclass, fields
from http import HTTPStatus
from urllib.parse import urlsplit

from websockets.asyncio.server import ServerConnection, serve
from websockets.exceptions import ConnectionClosed
from websockets.http11 import Request, Response

SAMPLE_RATE = 16_000


def nonnegative_float(value: str) -> float:
    result = float(value)
    if not math.isfinite(result) or result < 0:
        raise argparse.ArgumentTypeError("must be finite and nonnegative")
    return result


def positive_float(value: str) -> float:
    result = nonnegative_float(value)
    if result == 0:
        raise argparse.ArgumentTypeError("must be positive")
    return result


def nonnegative_int(value: str) -> int:
    result = int(value)
    if result < 0:
        raise argparse.ArgumentTypeError("must be nonnegative")
    return result


@dataclass(frozen=True)
class MockConfig:
    start_delay_ms: float = 30
    partial_interval_ms: float = 300
    finish_delay_ms: float = 150
    sentence_seconds: float = 3
    reject_first: int = 0
    hang_open: bool = False
    fail_after_seconds: float | None = None

    @classmethod
    def from_args(cls, args: argparse.Namespace, prefix: str = "") -> MockConfig:
        return cls(**{field.name: getattr(args, prefix + field.name) for field in fields(cls)})

    def cli_arguments(self) -> list[str]:
        arguments: list[str] = []
        for field in fields(self):
            value = getattr(self, field.name)
            option = "--" + field.name.replace("_", "-")
            if value is True:
                arguments.append(option)
            elif value is not False and value is not None:
                arguments.extend((option, str(value)))
        return arguments


def add_mock_arguments(parser: argparse.ArgumentParser, prefix: str = "") -> None:
    """Share option defaults and validation between the standalone mock and harness."""
    parser.add_argument(f"--{prefix}start-delay-ms", type=nonnegative_float, default=30)
    parser.add_argument(f"--{prefix}partial-interval-ms", type=positive_float, default=300)
    parser.add_argument(f"--{prefix}finish-delay-ms", type=nonnegative_float, default=150)
    parser.add_argument(f"--{prefix}sentence-seconds", type=positive_float, default=3)
    parser.add_argument(f"--{prefix}reject-first", type=nonnegative_int, default=0)
    parser.add_argument(f"--{prefix}hang-open", action="store_true")
    parser.add_argument(f"--{prefix}fail-after-seconds", type=nonnegative_float)


async def send_event(
    socket: ServerConnection,
    task_id: str,
    event: str,
    payload: dict[str, object] | None = None,
    **header_fields: str,
) -> None:
    await socket.send(
        json.dumps(
            {
                "header": {"task_id": task_id, "event": event, **header_fields},
                "payload": payload or {},
            },
            ensure_ascii=False,
            separators=(",", ":"),
        )
    )


def control_header(raw: str | bytes) -> dict[str, object]:
    if not isinstance(raw, str):
        raise ValueError("expected a JSON text control frame")
    message = json.loads(raw)
    header = message.get("header") if isinstance(message, dict) else None
    if not isinstance(header, dict):
        raise ValueError("expected a header object")
    return header


class MockGateway:
    def __init__(self, config: MockConfig) -> None:
        self.config = config
        self.handshakes = 0

    def process_request(self, socket: ServerConnection, request: Request) -> Response | None:
        if request.headers.get_all("Authorization") != ["Bearer mock-key"]:
            return socket.respond(HTTPStatus.UNAUTHORIZED, "Mock authorization required\n")
        if urlsplit(request.path).path != "/v1/audio/asr-realtime":
            return socket.respond(HTTPStatus.NOT_FOUND, "Unknown mock endpoint\n")
        self.handshakes += 1
        if self.handshakes <= self.config.reject_first:
            return socket.respond(HTTPStatus.SERVICE_UNAVAILABLE, "Injected rejection\n")
        return None

    async def handle(self, socket: ServerConnection) -> None:
        try:
            await self._session(socket)
        except ConnectionClosed:
            pass
        except (ValueError, TypeError):
            await socket.close(code=1002, reason="Invalid mock task protocol")

    async def _session(self, socket: ServerConnection) -> None:
        header = control_header(await socket.recv())
        task_id = header.get("task_id")
        if header.get("action") != "run-task" or not isinstance(task_id, str) or not task_id:
            raise ValueError("expected run-task with a task_id")
        if self.config.hang_open:
            await socket.wait_closed()
            return
        await asyncio.sleep(self.config.start_delay_ms / 1000)
        await send_event(socket, task_id, "task-started")

        # Integer sample boundaries avoid floating point drift and work with large chunks.
        partial_samples = max(1, round(self.config.partial_interval_ms * SAMPLE_RATE / 1000))
        sentence_samples = max(1, round(self.config.sentence_seconds * SAMPLE_RATE))
        failure_samples = (
            None
            if self.config.fail_after_seconds is None
            else round(self.config.fail_after_seconds * SAMPLE_RATE)
        )
        total_samples = 0
        next_partial = partial_samples
        next_sentence = sentence_samples
        sentence_id = 0
        tokens: list[str] = []

        async def result(final: bool) -> None:
            if tokens:
                await send_event(
                    socket,
                    task_id,
                    "result-generated",
                    {
                        "output": {
                            "sentence": {
                                "sentence_id": sentence_id,
                                "text": " ".join(tokens),
                                "sentence_end": final,
                            }
                        }
                    },
                )

        async for raw in socket:
            if isinstance(raw, bytes):
                if not raw or len(raw) % 2:
                    raise ValueError("expected complete PCM16LE samples")
                total_samples += len(raw) // 2
                limit = (
                    total_samples
                    if failure_samples is None
                    else min(total_samples, failure_samples)
                )
                while min(next_partial, next_sentence) <= limit:
                    if next_partial <= next_sentence:
                        tokens.append(f"词{len(tokens) + 1}")
                        await result(False)
                        next_partial += partial_samples
                    else:
                        await result(True)
                        tokens.clear()
                        sentence_id += 1
                        next_sentence += sentence_samples
                if failure_samples is not None and total_samples >= failure_samples:
                    await send_event(socket, task_id, "task-failed", error_code="TEST_FAILURE")
                    await socket.wait_closed()
                    return
            else:
                header = control_header(raw)
                if header.get("action") != "finish-task" or header.get("task_id") != task_id:
                    raise ValueError("expected matching finish-task")
                await asyncio.sleep(self.config.finish_delay_ms / 1000)
                await result(True)
                await send_event(socket, task_id, "task-finished")
                # Returning from a websockets handler closes it. The real gateway keeps it open.
                await socket.wait_closed()
                return


async def run_mock(port: int, config: MockConfig) -> None:
    gateway = MockGateway(config)
    stopping = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(sig, stopping.set)
    async with serve(
        gateway.handle,
        "127.0.0.1",
        port,
        process_request=gateway.process_request,
        compression=None,
        max_size=32768,
        max_queue=16,
        ping_interval=None,
        close_timeout=1,
        backlog=1024,
    ) as server:
        bound_port = server.sockets[0].getsockname()[1]
        print(f"MOCK_READY {bound_port}", flush=True)
        await stopping.wait()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--port", type=int, default=9100)
    add_mock_arguments(parser)
    args = parser.parse_args()
    if not 0 <= args.port <= 65535:
        parser.error("port must be between 0 and 65535")
    logging.basicConfig(level=logging.WARNING)
    asyncio.run(run_mock(args.port, MockConfig.from_args(args)))


if __name__ == "__main__":
    main()
