#!/usr/bin/env python3
"""Measure voice-bridge against a private local mock, never the paid gateway.

Example: .venv/bin/python scripts/loadtest.py --src-path /tmp/voice_bridge_baseline/src
Only the two scripts are needed. Logs and isolated child working directories are temporary.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import math
import os
import platform
import resource
import signal
import socket
import subprocess
import sys
import tempfile
import time
from collections import Counter
from contextlib import suppress
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, BinaryIO

from websockets.asyncio.client import ClientConnection, connect
from websockets.exceptions import ConnectionClosed, InvalidHandshake, InvalidStatus

# Import our sibling without leaving __pycache__ in the repository.
sys.dont_write_bytecode = True
from mock_gateway import (  # noqa: E402
    MockConfig,
    add_mock_arguments,
    nonnegative_float,
    positive_float,
)

SAMPLE_RATE = 16_000
BYTES_PER_SECOND = SAMPLE_RATE * 2
STAMP_NAMES = (
    "connect_start",
    "ws_open",
    "start_sent",
    "ready",
    "first_audio_sent",
    "first_transcript",
    "stop_sent",
    "done",
    "closed",
)

# This executes in a fresh cwd with a deliberately small environment. In addition to
# pinning the URL, fail closed on Python socket attempts outside the local mock.
SERVICE_LAUNCH = """
import json
import pathlib
import sys

source, mock_port = sys.argv[1], int(sys.argv[2])

def local_network_only(event, args):
    if event == "socket.getaddrinfo":
        host = args[0]
        if host not in ("127.0.0.1", b"127.0.0.1"):
            raise PermissionError("load test blocked non-loopback DNS")
    elif event == "socket.connect":
        address = args[1]
        if not isinstance(address, tuple) or address[:2] != ("127.0.0.1", mock_port):
            raise PermissionError("load test blocked a connection outside the mock")

sys.addaudithook(local_network_only)
if source:
    sys.path.insert(0, source)
import voice_bridge
actual = pathlib.Path(voice_bridge.__file__).resolve()
if source and not actual.is_relative_to(pathlib.Path(source).resolve()):
    raise RuntimeError("selected source lost to the editable install: " + str(actual))
print("SOURCE_VERIFIED " + json.dumps(str(actual)), flush=True)
from voice_bridge.main import run
run()
"""


class LocalConnect(connect):
    """Never follow an HTTP redirect away from our loopback service."""

    def process_redirect(self, exc: Exception) -> Exception:
        return exc


@dataclass
class Child:
    process: subprocess.Popen[bytes]
    started: float
    usage: resource.struct_rusage | None = None
    ended: float | None = None
    killed: bool = False

    @classmethod
    def start(
        cls,
        command: list[str],
        cwd: Path,
        env: dict[str, str],
        stdout: BinaryIO,
        stderr: BinaryIO,
    ) -> Child:
        started = time.monotonic()
        process = subprocess.Popen(
            command,
            cwd=cwd,
            env=env,
            stdin=subprocess.DEVNULL,
            stdout=stdout,
            stderr=stderr,
            start_new_session=True,
        )
        return cls(process=process, started=started)

    def reap(self) -> bool:
        """Be the sole reaper: Popen.poll/wait would discard the wait4 rusage."""
        if self.usage is None:
            pid, status, usage = os.wait4(self.process.pid, os.WNOHANG)
            if pid:
                self.process.returncode = os.waitstatus_to_exitcode(status)
                self.usage = usage
                self.ended = time.monotonic()
        return self.usage is not None

    async def stop(self) -> None:
        if self.reap():
            return
        changed = asyncio.Event()
        loop = asyncio.get_running_loop()
        # No asyncio subprocess watcher: it would reap the child before our wait4.
        loop.add_signal_handler(signal.SIGCHLD, changed.set)
        try:
            with suppress(ProcessLookupError):
                os.kill(self.process.pid, signal.SIGTERM)
            try:
                async with asyncio.timeout(20):
                    while not self.reap():
                        await changed.wait()
                        changed.clear()
            except TimeoutError:
                self.killed = True
                with suppress(ProcessLookupError):
                    os.kill(self.process.pid, signal.SIGKILL)
                while not self.reap():
                    await changed.wait()
                    changed.clear()
        finally:
            loop.remove_signal_handler(signal.SIGCHLD)

    def stats(self, run_wall_seconds: float) -> dict[str, Any]:
        assert self.usage is not None and self.ended is not None
        cpu = self.usage.ru_utime + self.usage.ru_stime
        rss_bytes = self.usage.ru_maxrss * (1 if sys.platform == "darwin" else 1024)
        return {
            "pid": self.process.pid,
            "exit_code": self.process.returncode,
            "killed_on_timeout": self.killed,
            "user_cpu_seconds": self.usage.ru_utime,
            "system_cpu_seconds": self.usage.ru_stime,
            "cpu_seconds": cpu,
            "average_cpu_cores": cpu / run_wall_seconds,
            "max_rss_bytes": rss_bytes,
            "max_rss_mib": rss_bytes / 1024**2,
            "process_wall_seconds": self.ended - self.started,
            "cpu_scope": "child lifetime (includes startup/shutdown), divided by client run wall",
        }


@dataclass
class Session:
    id: int
    timestamps: dict[str, float | None] = field(default_factory=lambda: dict.fromkeys(STAMP_NAMES))
    outcome: str = "closed_early"
    close_code: int | None = None
    http_status: int | None = None
    transcript_messages: int = 0
    final_transcript_messages: int = 0
    audio_bytes_sent: int = 0
    audio_chunks_sent: int = 0
    audio_bytes_before_ready: int = 0
    audio_chunks_before_ready: int = 0
    done_audio_seconds: float | None = None
    exception_message: str | None = None

    def stamp(self, name: str) -> None:
        if self.timestamps[name] is None:
            self.timestamps[name] = time.monotonic()


@dataclass
class LoadState:
    open_now: int = 0
    ready_now: int = 0
    peak_open: int = 0
    peak_ready: int = 0
    pacing_lag_ms: list[float] = field(default_factory=list)

    def opened(self) -> None:
        self.open_now += 1
        self.peak_open = max(self.peak_open, self.open_now)

    def ready(self) -> None:
        self.ready_now += 1
        self.peak_ready = max(self.peak_ready, self.ready_now)


async def receive_messages(
    ws: ClientConnection, session: Session, ready: asyncio.Event, state: LoadState
) -> None:
    async for raw in ws:
        message = json.loads(raw)
        kind = message["type"]
        if kind == "ready" and not ready.is_set():
            session.stamp("ready")
            state.ready()
            ready.set()
        elif kind == "transcript":
            if message.get("text"):
                session.stamp("first_transcript")
            session.transcript_messages += 1
            session.final_transcript_messages += bool(message.get("is_final"))
        elif kind == "done":
            session.stamp("done")
            session.done_audio_seconds = message.get("audio_seconds")
            session.outcome = "done"
        elif kind == "error":
            code = message.get("code", "unknown")
            session.outcome = "rejected" if code == "server_busy" else f"error:{code}"
        # Continue reading after done/error to record the server's close, not our own.


async def send_audio(
    ws: ClientConnection,
    session: Session,
    ready: asyncio.Event,
    state: LoadState,
    pcm: bytes,
    total_samples: int,
    early_audio: bool,
) -> None:
    if not early_audio:
        await ready.wait()
    origin = time.monotonic()
    samples_sent = 0
    chunk_samples = len(pcm) // 2
    while samples_sent < total_samples:
        deadline = origin + samples_sent / SAMPLE_RATE
        await asyncio.sleep(max(0, deadline - time.monotonic()))
        state.pacing_lag_ms.append(max(0, time.monotonic() - deadline) * 1000)
        remaining = min(chunk_samples, total_samples - samples_sent)
        payload = pcm if remaining == chunk_samples else pcm[: remaining * 2]
        before_ready = not ready.is_set()
        # Timestamp send initiation so a very fast response cannot precede its timestamp.
        session.stamp("first_audio_sent")
        await ws.send(payload)
        session.audio_bytes_sent += len(payload)
        session.audio_chunks_sent += 1
        if before_ready:
            session.audio_bytes_before_ready += len(payload)
            session.audio_chunks_before_ready += 1
        samples_sent += remaining
    # The last chunk represents audio through this deadline; don't stop one chunk early.
    await asyncio.sleep(max(0, origin + total_samples / SAMPLE_RATE - time.monotonic()))
    await ready.wait()
    session.stamp("stop_sent")
    await ws.send('{"type":"stop"}')


async def run_session(
    number: int,
    scheduled_start: float,
    args: argparse.Namespace,
    state: LoadState,
    pcm: bytes,
) -> Session:
    await asyncio.sleep(max(0, scheduled_start - time.monotonic()))
    session = Session(number)
    ready = asyncio.Event()
    ws: ClientConnection | None = None
    tasks: list[asyncio.Task[None]] = []
    session.stamp("connect_start")
    try:
        async with asyncio.timeout(args.duration + 30):
            ws = await LocalConnect(
                f"ws://127.0.0.1:{args.port}/v1/asr",
                additional_headers={"X-API-Key": "load-test-service-key-00000000000000"},
                proxy=None,
                compression=None,
                ping_interval=None,
                open_timeout=None,
                close_timeout=1,
                max_size=2**20,
                max_queue=32,
            )
            session.stamp("ws_open")
            state.opened()
            session.stamp("start_sent")
            await ws.send('{"type":"start","provider":"funasr","language":"zh"}')
            receiver = asyncio.create_task(receive_messages(ws, session, ready, state))
            sender = asyncio.create_task(
                send_audio(ws, session, ready, state, pcm, args.total_samples, args.early_audio)
            )
            tasks.extend((receiver, sender))
            finished, _ = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
            if receiver in finished:
                await receiver
            else:
                try:
                    await sender
                except ConnectionClosed:
                    # The receiver may still hold a queued error/done frame; drain it.
                    pass
                await receiver
    except InvalidStatus as exc:
        session.http_status = exc.response.status_code
        session.outcome = (
            "rejected"
            if session.http_status in {403, 429, 503}
            else f"error:http_{session.http_status}"
        )
    except InvalidHandshake as exc:
        session.outcome = "rejected"
        session.exception_message = str(exc)[:300]
    except ConnectionClosed:
        pass
    except TimeoutError:
        session.outcome = "timeout"
    except Exception as exc:
        session.outcome = f"exception:{type(exc).__name__}"
        session.exception_message = str(exc)[:300]
    finally:
        for task in tasks:
            if not task.done():
                task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        if ws is not None:
            # Inspect the remote close before cleaning up; our cleanup uses 1000 itself.
            remote_close_code = ws.close_code
            if (
                remote_close_code == 1013
                and session.timestamps["ready"] is None
                and not session.outcome.startswith("error:")
            ):
                session.outcome = "rejected"
            elif session.timestamps["ready"] is None and session.outcome == "closed_early":
                session.outcome = "rejected"
            if session.outcome == "done" and remote_close_code != 1000:
                session.outcome = "closed_early"
            session.close_code = remote_close_code
            if remote_close_code is not None:
                session.stamp("closed")
            try:
                with suppress(OSError, ConnectionClosed, TimeoutError):
                    await ws.close()
            finally:
                ws.transport.abort()
                state.open_now -= 1
                if ready.is_set():
                    state.ready_now -= 1
                session.stamp("closed")
    return session


def percentiles(values: list[float]) -> dict[str, float | None]:
    if not values:
        return dict.fromkeys(("min", "p50", "p90", "p99", "max"))
    ordered = sorted(values)

    def percentile(fraction: float) -> float:
        position = (len(ordered) - 1) * fraction
        lower = math.floor(position)
        upper = math.ceil(position)
        return ordered[lower] + (ordered[upper] - ordered[lower]) * (position - lower)

    return {
        name: percentile(p)
        for name, p in zip(("min", "p50", "p90", "p99", "max"), (0, 0.5, 0.9, 0.99, 1), strict=True)
    }


async def probe_generator(lags: list[float], cpu_windows: list[float]) -> None:
    previous_wall = time.monotonic()
    previous_cpu = time.process_time()
    while True:
        deadline = time.monotonic() + 0.05
        await asyncio.sleep(0.05)
        now = time.monotonic()
        lags.append(max(0, now - deadline) * 1000)
        if now - previous_wall >= 1:
            current_cpu = time.process_time()
            cpu_windows.append((current_cpu - previous_cpu) / (now - previous_wall))
            previous_cpu, previous_wall = current_cpu, now


async def run_clients(args: argparse.Namespace) -> tuple[list[Session], LoadState, dict[str, Any]]:
    # A single immutable silence buffer is reused across all sessions and sends.
    pcm = bytes(round(args.chunk_ms * SAMPLE_RATE / 1000) * 2)
    state = LoadState()
    lags: list[float] = []
    cpu_windows: list[float] = []
    started, cpu_started = time.monotonic(), time.process_time()
    probe = asyncio.create_task(probe_generator(lags, cpu_windows))
    tasks = [
        asyncio.create_task(
            run_session(
                i + 1,
                started + (args.ramp * i / (args.connections - 1) if args.connections > 1 else 0),
                args,
                state,
                pcm,
            )
        )
        for i in range(args.connections)
    ]
    try:
        sessions = await asyncio.gather(*tasks)
    finally:
        for task in (*tasks, probe):
            if not task.done():
                task.cancel()
        await asyncio.gather(*tasks, probe, return_exceptions=True)
    wall, cpu = time.monotonic() - started, time.process_time() - cpu_started
    return (
        sessions,
        state,
        {
            "run_wall_seconds": wall,
            "cpu_seconds": cpu,
            "average_cpu_cores": cpu / wall,
            "peak_one_second_cpu_cores": max(cpu_windows, default=cpu / wall),
            "event_loop_lag_ms": percentiles(lags),
            "audio_schedule_lag_ms": percentiles(state.pacing_lag_ms),
        },
    )


def free_port(exclude: int | None = None) -> int:
    while True:
        with socket.socket() as listener:
            listener.bind(("127.0.0.1", 0))
            port = listener.getsockname()[1]
        if port != exclude:
            return port


async def health_ok(port: int) -> bool:
    writer: asyncio.StreamWriter | None = None
    try:
        async with asyncio.timeout(0.5):
            reader, writer = await asyncio.open_connection("127.0.0.1", port)
            writer.write(b"GET /healthz HTTP/1.1\r\nHost: localhost\r\nConnection: close\r\n\r\n")
            await writer.drain()
            return (await reader.readline()).split()[1:2] == [b"200"]
    except (OSError, TimeoutError):
        return False
    finally:
        if writer is not None:
            writer.close()
            with suppress(OSError):
                await writer.wait_closed()


async def wait_for_mock(child: Child, stdout_path: Path, port: int) -> None:
    async with asyncio.timeout(20):
        while True:
            if child.reap():
                raise RuntimeError("mock exited before becoming ready")
            if f"MOCK_READY {port}\n" in await asyncio.to_thread(stdout_path.read_text):
                return
            await asyncio.sleep(0.05)


async def wait_for_service(child: Child, port: int, stderr_path: Path) -> None:
    async with asyncio.timeout(20):
        while True:
            if child.reap():
                raise RuntimeError("service exited before becoming healthy")
            # Only probe after OUR child has bound the port. An unrelated healthy server
            # on an occupied port could otherwise redirect the load to its paid upstream.
            log = await asyncio.to_thread(stderr_path.read_text, errors="replace")
            if f"Uvicorn running on http://127.0.0.1:{port} " in log and await health_ok(port):
                return
            await asyncio.sleep(0.05)


async def stop_children(service: Child | None, mock: Child | None) -> None:
    try:
        if service is not None:
            await service.stop()
    finally:
        if mock is not None:
            await mock.stop()


def log_summary(path: Path) -> dict[str, Any]:
    text = path.read_text(errors="replace")
    lines = text.splitlines()
    notable = Counter(
        line.strip()
        for line in lines
        if any(
            term in line.lower()
            for term in ("warning", "error", "failed", "traceback", "deprecated")
        )
    )
    return {
        "line_count": len(lines),
        "notable_lines": [{"line": line, "count": count} for line, count in notable.items()],
        "http_403_lines": sum("403" in line and "Forbidden" in line for line in lines),
    }


def build_report(
    args: argparse.Namespace,
    config: MockConfig,
    sessions: list[Session],
    state: LoadState,
    client_stats: dict[str, Any],
    service: Child,
    mock: Child,
    source: str,
    service_log: dict[str, Any],
    mock_log: dict[str, Any],
) -> dict[str, Any]:
    successful = [session for session in sessions if session.outcome == "done"]

    def latency(start: str, end: str, subtract_ms: float = 0) -> dict[str, float | None]:
        values = []
        for session in successful:
            first, last = session.timestamps[start], session.timestamps[end]
            if first is not None and last is not None:
                values.append((last - first) * 1000 - subtract_ms)
        return percentiles(values)

    wall = client_stats["run_wall_seconds"]
    warnings = []
    if client_stats["average_cpu_cores"] >= 0.8 or client_stats["peak_one_second_cpu_cores"] >= 0.9:
        warnings.append("Client CPU approached one core; latency may reflect generator saturation.")
    if (client_stats["audio_schedule_lag_ms"]["p99"] or 0) > args.chunk_ms * 0.2:
        warnings.append("Client p99 scheduling lag exceeded 20% of the audio chunk interval.")
    if any(
        session.done_audio_seconds is None
        or abs(session.done_audio_seconds - session.audio_bytes_sent / BYTES_PER_SECOND) > 0.0001
        for session in successful
    ):
        warnings.append("A successful session's done.audio_seconds disagreed with sent PCM bytes.")
    for name, child in (("service", service), ("mock", mock)):
        # Uvicorn deliberately reraises SIGTERM after completing graceful shutdown.
        if child.killed or child.process.returncode not in (0, -signal.SIGTERM):
            warnings.append(f"{name} exit code {child.process.returncode}; killed={child.killed}.")
    return {
        "label": args.label,
        "recorded_at_utc": datetime.now(UTC).isoformat(),
        "platform": platform.platform(),
        "python": sys.version.split()[0],
        "source_file": source,
        "config": {
            "connections": args.connections,
            "duration_seconds": args.duration,
            "actual_audio_seconds": args.total_samples / SAMPLE_RATE,
            "ramp_seconds": args.ramp,
            "max_connections": args.max_connections,
            "chunk_ms": args.chunk_ms,
            "early_audio": args.early_audio,
            "src_path": str(args.src_path) if args.src_path else None,
            "port": args.port,
            "mock_port": args.mock_port,
            "mock": asdict(config),
        },
        "outcomes": dict(sorted(Counter(session.outcome for session in sessions).items())),
        "close_codes": dict(Counter(str(session.close_code) for session in sessions)),
        "http_statuses": dict(Counter(str(session.http_status) for session in sessions)),
        "peak_concurrent_sessions": state.peak_ready,
        "peak_open_websockets": state.peak_open,
        "successful_sessions": len(successful),
        "latency_ms_successful": {
            "connect_to_ready": latency("connect_start", "ready"),
            "start_to_ready": latency("start_sent", "ready"),
            "start_to_first_transcript": latency("start_sent", "first_transcript"),
            "first_audio_to_first_transcript": latency("first_audio_sent", "first_transcript"),
            "stop_to_done": latency("stop_sent", "done"),
            "approx_service_added_ready": latency("start_sent", "ready", config.start_delay_ms),
            "approx_service_added_done": latency("stop_sent", "done", config.finish_delay_ms),
        },
        "transcript_messages_per_session": percentiles(
            [float(session.transcript_messages) for session in sessions]
        ),
        "transcript_messages_per_successful_session": percentiles(
            [float(session.transcript_messages) for session in successful]
        ),
        "audio_before_ready": {
            "bytes": sum(session.audio_bytes_before_ready for session in sessions),
            "chunks": sum(session.audio_chunks_before_ready for session in sessions),
        },
        "service": service.stats(wall),
        "mock": mock.stats(wall),
        "client": client_stats,
        "service_stderr": service_log,
        "mock_stderr": mock_log,
        "warnings": warnings,
        "notes": [
            "Service-added latency subtracts fixed mock delays; these are approximations. "
            "Mock scheduling, loopback I/O and load-generator scheduling on this machine "
            "add noise.",
            "All timestamps use time.monotonic; start_to_ready starts at start_sent. "
            "connect_to_ready additionally includes the client WebSocket handshake.",
            "CPU/RSS are wait4 child-lifetime values; average cores divide CPU by client run wall. "
            "Client CPU covers only the load interval. Mock, service and clients are "
            "separate processes.",
            "Peak concurrent sessions counts ready sessions until closure; peak open WebSockets "
            "also includes sessions waiting for ready. Percentiles use linear interpolation.",
            "PCM silence is sent at the start of each chunk interval, so a 300 ms partial interval "
            "with 100 ms chunks yields the first transcript about 200 ms after the first send.",
        ],
        "sessions": [asdict(session) for session in sessions],
    }


async def orchestrate(args: argparse.Namespace) -> dict[str, Any]:
    config = MockConfig.from_args(args, "mock_")
    with tempfile.TemporaryDirectory(prefix="voice_bridge_loadtest_", dir="/tmp") as directory:
        root = Path(directory)
        service_cwd, mock_cwd = root / "service", root / "mock"
        service_cwd.mkdir()
        mock_cwd.mkdir()
        env = {
            "PATH": "/usr/bin:/bin",
            "HOME": str(root),
            "PYTHONDONTWRITEBYTECODE": "1",
            "PYTHONUNBUFFERED": "1",
        }
        service_env = {
            **env,
            "HOST": "127.0.0.1",
            "PORT": str(args.port),
            "MAX_CONNECTIONS": str(args.max_connections),
            "MAAS_API_KEY": "mock-key",
            "SERVICE_API_KEY": "load-test-service-key-00000000000000",
            "MAAS_BASE_URL": f"http://127.0.0.1:{args.mock_port}",
        }
        if args.src_path:
            service_env["PYTHONPATH"] = str(args.src_path)
        service: Child | None = None
        mock: Child | None = None
        paths = {
            name: root / f"{name}.log"
            for name in ("service_stdout", "service_stderr", "mock_stdout", "mock_stderr")
        }
        with (
            paths["service_stdout"].open("wb") as service_out,
            paths["service_stderr"].open("wb") as service_err,
            paths["mock_stdout"].open("wb") as mock_out,
            paths["mock_stderr"].open("wb") as mock_err,
        ):
            try:
                mock = Child.start(
                    [
                        sys.executable,
                        str(Path(__file__).with_name("mock_gateway.py")),
                        "--port",
                        str(args.mock_port),
                        *config.cli_arguments(),
                    ],
                    mock_cwd,
                    env,
                    mock_out,
                    mock_err,
                )
                await wait_for_mock(mock, paths["mock_stdout"], args.mock_port)
                service = Child.start(
                    [
                        sys.executable,
                        "-c",
                        SERVICE_LAUNCH,
                        str(args.src_path) if args.src_path else "",
                        str(args.mock_port),
                    ],
                    service_cwd,
                    service_env,
                    service_out,
                    service_err,
                )
                await wait_for_service(service, args.port, paths["service_stderr"])
                source_lines = paths["service_stdout"].read_text().splitlines()
                source = next(
                    json.loads(line.removeprefix("SOURCE_VERIFIED "))
                    for line in source_lines
                    if line.startswith("SOURCE_VERIFIED ")
                )
                print(f"Verified service source: {source}", flush=True)
                print(f"Local ports: service={args.port}, mock={args.mock_port}", flush=True)
                sessions, state, client_stats = await run_clients(args)
            except BaseException:
                for name in ("service_stderr", "mock_stderr"):
                    lines = paths[name].read_text(errors="replace").splitlines()
                    if lines:
                        print(f"{name} (last 20 lines):", file=sys.stderr)
                        print("\n".join(lines[-20:]), file=sys.stderr)
                raise
            finally:
                cleanup = asyncio.create_task(stop_children(service, mock))
                try:
                    await asyncio.shield(cleanup)
                except asyncio.CancelledError:
                    # A first Ctrl-C during shutdown must not interrupt reaping or log cleanup.
                    await cleanup
                    raise
        assert service is not None and mock is not None
        return build_report(
            args,
            config,
            sessions,
            state,
            client_stats,
            service,
            mock,
            source,
            log_summary(paths["service_stderr"]),
            log_summary(paths["mock_stderr"]),
        )


def print_report(report: dict[str, Any]) -> None:
    print(f"\n{report['label']} | outcomes: {json.dumps(report['outcomes'], sort_keys=True)}")
    print(
        f"Peak ready/open: {report['peak_concurrent_sessions']}/{report['peak_open_websockets']} | "
        f"load wall: {report['client']['run_wall_seconds']:.3f} s | "
        f"audio before ready: {report['audio_before_ready']}"
    )
    print(
        f"{'Successful-session latency (ms)':<38} "
        f"{'min':>9} {'p50':>9} {'p90':>9} {'p99':>9} {'max':>9}"
    )
    for name, stats in report["latency_ms_successful"].items():
        cells = " ".join(
            f"{value:9.3f}" if value is not None else f"{'n/a':>9}" for value in stats.values()
        )
        print(f"{name:<38} {cells}")
    print("Transcripts/session (all):", report["transcript_messages_per_session"])
    print("Transcripts/session (done):", report["transcript_messages_per_successful_session"])
    print(f"{'Process':<12} {'CPU seconds':>13} {'Avg cores':>12} {'Max RSS MiB':>14}")
    for name in ("service", "mock", "client"):
        stats = report[name]
        rss = f"{stats['max_rss_mib']:.2f}" if "max_rss_mib" in stats else "n/a"
        print(
            f"{name:<12} {stats['cpu_seconds']:13.3f} {stats['average_cpu_cores']:12.3f} {rss:>14}"
        )
    client = report["client"]
    print(
        f"Client peak 1 s cores: {client['peak_one_second_cpu_cores']:.3f}; "
        f"p99 audio schedule lag: {client['audio_schedule_lag_ms']['p99']} ms"
    )
    print("Service stderr:", json.dumps(report["service_stderr"], ensure_ascii=False))
    print("Mock stderr:", json.dumps(report["mock_stderr"], ensure_ascii=False))
    for warning in report["warnings"]:
        print(f"WARNING: {warning}")
    for note in report["notes"]:
        print(note)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--connections", type=int, default=100)
    parser.add_argument("--duration", type=positive_float, default=10)
    parser.add_argument("--ramp", type=nonnegative_float, default=5)
    parser.add_argument("--max-connections", type=int, default=100)
    parser.add_argument("--src-path", type=Path)
    parser.add_argument("--early-audio", action="store_true")
    parser.add_argument("--chunk-ms", type=positive_float, default=100)
    parser.add_argument("--json-out", type=Path)
    parser.add_argument(
        "--append-json",
        action="store_true",
        help="keep repeated runs in a JSON {runs: [...]} object",
    )
    parser.add_argument("--label", default="local mock load test")
    parser.add_argument("--port", type=int)
    parser.add_argument("--mock-port", type=int)
    add_mock_arguments(parser, "mock-")
    args = parser.parse_args()
    if args.append_json and args.json_out is None:
        parser.error("append-json requires json-out")
    if args.connections < 1 or args.max_connections < 1:
        parser.error("connections and max-connections must be positive")
    if not 1 <= round(args.chunk_ms * SAMPLE_RATE / 1000) <= 8192:
        parser.error("chunk-ms must produce between 2 and 16384 bytes of PCM")
    args.total_samples = round(args.duration * SAMPLE_RATE)
    if args.total_samples < 1:
        parser.error("duration must represent at least one PCM sample")
    for port in (args.port, args.mock_port):
        if port is not None and not 1 <= port <= 65535:
            parser.error("ports must be between 1 and 65535")
    if args.src_path:
        args.src_path = args.src_path.resolve()
        if not (args.src_path / "voice_bridge" / "__init__.py").is_file():
            parser.error("src-path must contain voice_bridge/__init__.py")
    args.port = args.port or free_port(args.mock_port)
    args.mock_port = args.mock_port or free_port(args.port)
    if args.port == args.mock_port:
        parser.error("service and mock ports must differ")
    return args


def main() -> None:
    args = parse_args()
    try:
        report = asyncio.run(orchestrate(args))
    except KeyboardInterrupt:
        print("Interrupted; child processes cleaned up.", file=sys.stderr)
        raise SystemExit(130) from None
    print_report(report)
    if args.json_out:
        output = report
        if args.append_json:
            previous = json.loads(args.json_out.read_text()) if args.json_out.exists() else None
            runs = previous.get("runs", [previous]) if previous is not None else []
            output = {"runs": [*runs, report]}
        args.json_out.write_text(json.dumps(output, indent=2, ensure_ascii=False) + "\n")
        print(f"JSON written: {args.json_out}")


if __name__ == "__main__":
    main()
