#!/usr/bin/env python3
"""Install the built wheel into a fresh venv and exercise the packaged application.

CI calls this after `uv build`. It checks the wheel and sdist contents, installs the
wheel with pip into a throwaway virtual environment, starts the installed console
script, and probes the HTTP endpoints, demo assets and guide.

Only fake provider credentials are used. QWEN_BASE_URL points at a closed loopback
port, so no request can reach a real speech service.
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import socket
import subprocess
import sys
import tarfile
import tempfile
import time
import urllib.error
import urllib.request
import venv
import zipfile
from pathlib import Path

ASSETS = (
    "demo.css",
    "demo.js",
    "guide.html",
    "index.html",
    "pcm-resampler.js",
    "pcm-worklet.js",
    "recorder.js",
)
SERVICE_KEY = "smoke_service_key_0123456789ABCDEFGHIJKLMNOP"
DASHSCOPE_KEY = "fake-dashscope-key-for-install-smoke"
UPSTREAM_URL = "wss://127.0.0.1:9/realtime"
OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))


def fail(message: str) -> None:
    raise SystemExit(f"smoke install failed: {message}")


def resolve_artifacts(target: Path) -> tuple[Path, Path]:
    """Accept the wheel itself or the dist directory containing exactly one wheel/sdist."""
    if target.is_file():
        wheel = target
    elif target.is_dir():
        wheels = sorted(target.glob("voice_bridge-*.whl"))
        if len(wheels) != 1:
            fail(f"expected exactly one wheel in {target}, found {len(wheels)}")
        wheel = wheels[0]
    else:
        fail(f"distribution path does not exist: {target}")
    sdists = sorted(wheel.parent.glob("voice_bridge-*.tar.gz"))
    if len(sdists) != 1:
        fail(f"expected exactly one sdist next to {wheel.name}, found {len(sdists)}")
    return wheel, sdists[0]


def check_license(text: str, source: str) -> None:
    for marker in ("Apache License", "Version 2.0, January 2004", "END OF TERMS AND CONDITIONS"):
        if marker not in text:
            fail(f"{source} does not contain the full Apache-2.0 text (missing {marker!r})")


def check_wheel(wheel: Path) -> None:
    with zipfile.ZipFile(wheel) as archive:
        names = set(archive.namelist())
        for asset in ASSETS:
            member = f"voice_bridge/web/{asset}"
            if member not in names:
                fail(f"{wheel.name} is missing packaged web asset {member}")
        metadata_members = [name for name in names if name.endswith(".dist-info/METADATA")]
        if len(metadata_members) != 1:
            fail(f"{wheel.name} must contain exactly one METADATA file")
        metadata = archive.read(metadata_members[0]).decode("utf-8")
        expression = None
        for line in metadata.splitlines():
            if line.startswith("License-Expression: "):
                expression = line.split(": ", 1)[1]
                break
        if expression != "Apache-2.0":
            fail(f"{wheel.name} has License-Expression {expression!r}, expected 'Apache-2.0'")
        dist_info = metadata_members[0].split("/", 1)[0]
        license_member = f"{dist_info}/licenses/LICENSE"
        if license_member not in names:
            fail(f"{wheel.name} is missing {license_member}")
        license_text = archive.read(license_member).decode("utf-8")
        check_license(license_text, f"{wheel.name}:{license_member}")
    print(f"wheel contents ok: {wheel.name}")


def check_sdist(sdist: Path) -> None:
    with tarfile.open(sdist) as archive:
        members = {member.name: member for member in archive.getmembers() if member.isfile()}
        roots = {name.split("/", 1)[0] for name in members}
        if len(roots) != 1:
            fail(f"{sdist.name} must contain exactly one top-level directory")
        root = roots.pop()
        required = ["LICENSE", "PKG-INFO", "README.md", "pyproject.toml"]
        required.extend(f"src/voice_bridge/web/{asset}" for asset in ASSETS)
        for relative in required:
            if f"{root}/{relative}" not in members:
                fail(f"{sdist.name} is missing {relative}")
        license_file = archive.extractfile(members[f"{root}/LICENSE"])
        if license_file is None:
            fail(f"{sdist.name} has an unreadable LICENSE")
        check_license(license_file.read().decode("utf-8"), f"{sdist.name}:LICENSE")
        pkg_info = archive.extractfile(members[f"{root}/PKG-INFO"])
        if pkg_info is None or b"License-Expression: Apache-2.0" not in pkg_info.read():
            fail(f"{sdist.name} does not declare License-Expression: Apache-2.0")
    print(f"sdist contents ok: {sdist.name}")


def create_venv(workdir: Path) -> Path:
    env_dir = workdir / "venv"
    # Match the venv CLI: copying a standalone macOS executable breaks its dylib lookup.
    venv.EnvBuilder(with_pip=True, clear=True, symlinks=os.name != "nt").create(env_dir)
    python = env_dir / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
    if not python.is_file():
        fail(f"virtual environment has no interpreter: {python}")
    return python


def run(arguments: list[str], *, cwd: Path | None = None) -> None:
    try:
        subprocess.run(arguments, cwd=cwd, check=True)
    except subprocess.CalledProcessError as error:
        fail(f"command failed with status {error.returncode}: {' '.join(arguments)}")


def installed_module(python: Path, workdir: Path) -> Path:
    code = "import pathlib, voice_bridge; print(pathlib.Path(voice_bridge.__file__).resolve())"
    env = os.environ.copy()
    for name in ("PYTHONHOME", "PYTHONPATH", "VIRTUAL_ENV"):
        env.pop(name, None)
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    env["PYTHONUTF8"] = "1"
    result = subprocess.run(
        [str(python), "-c", code],
        cwd=workdir,
        env=env,
        check=True,
        capture_output=True,
        text=True,
    )
    return Path(result.stdout.strip())


def free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def start_server(workdir: Path, port: int) -> tuple[subprocess.Popen[bytes], Path]:
    log_path = workdir / "server.log"
    bin_dir = "Scripts" if os.name == "nt" else "bin"
    script_name = "voice-bridge.exe" if os.name == "nt" else "voice-bridge"
    executable = workdir / "venv" / bin_dir / script_name
    if not executable.is_file():
        fail(f"console script was not installed: {executable}")
    env = {
        "PATH": os.environ.get("PATH", ""),
        "PYTHONDONTWRITEBYTECODE": "1",
        "PYTHONUTF8": "1",
        "SERVICE_API_KEY": SERVICE_KEY,
        "DASHSCOPE_API_KEY": DASHSCOPE_KEY,
        "MAAS_API_KEY": "",
        "QWEN_BASE_URL": UPSTREAM_URL,
        "HOST": "127.0.0.1",
        "PORT": str(port),
    }
    with log_path.open("wb") as log:
        process = subprocess.Popen(
            [str(executable)],
            cwd=workdir,
            env=env,
            stdout=log,
            stderr=subprocess.STDOUT,
        )
    return process, log_path


def http_get(port: int, path: str, api_key: str | None = None) -> tuple[int, str, bytes]:
    request = urllib.request.Request(f"http://127.0.0.1:{port}{path}")
    if api_key is not None:
        request.add_header("X-API-Key", api_key)
    try:
        with OPENER.open(request, timeout=5) as response:
            return response.status, response.headers.get_content_type(), response.read()
    except urllib.error.HTTPError as error:
        return error.code, error.headers.get_content_type(), error.read()


def read_log(log_path: Path) -> str:
    try:
        return log_path.read_text(encoding="utf-8", errors="replace")[-4000:]
    except OSError:
        return "<server log unavailable>"


def wait_for_health(port: int, process: subprocess.Popen[bytes], log_path: Path) -> None:
    deadline = time.monotonic() + 30
    while time.monotonic() < deadline:
        if process.poll() is not None:
            fail(f"server exited during startup:\n{read_log(log_path)}")
        try:
            status, _, body = http_get(port, "/healthz")
            if status == 200 and json.loads(body) == {"status": "ok"}:
                return
        except (OSError, json.JSONDecodeError):
            pass
        time.sleep(0.2)
    fail(f"server did not become healthy within 30s:\n{read_log(log_path)}")


def verify_endpoints(port: int) -> None:
    status, _, body = http_get(port, "/healthz")
    if status != 200 or json.loads(body) != {"status": "ok"}:
        fail(f"/healthz returned {status}: {body!r}")

    status, _, body = http_get(port, "/readyz")
    if status != 200 or json.loads(body).get("status") != "ready":
        fail(f"/readyz returned {status}: {body!r}")

    status, _, _ = http_get(port, "/v1/asr/capabilities")
    if status != 401:
        fail(f"/v1/asr/capabilities without a key returned {status}, expected 401")

    status, _, body = http_get(port, "/v1/asr/capabilities", SERVICE_KEY)
    if status != 200:
        fail(f"/v1/asr/capabilities with a key returned {status}: {body!r}")
    capabilities = json.loads(body)
    configured = {provider["id"]: provider["configured"] for provider in capabilities["providers"]}
    if configured != {"qwen": True, "funasr": False}:
        fail(f"capabilities reported unexpected providers: {configured!r}")
    if capabilities.get("default_provider") != "qwen":
        fail(f"capabilities default_provider is {capabilities.get('default_provider')!r}")
    if SERVICE_KEY.encode() in body or DASHSCOPE_KEY.encode() in body:
        fail("capabilities leaked a configured key")

    status, content_type, body = http_get(port, "/demo")
    if status != 200 or content_type != "text/html":
        fail(f"/demo returned {status} with content type {content_type!r}")

    status, content_type, body = http_get(port, "/guide")
    if status != 200 or content_type != "text/html":
        fail(f"/guide returned {status} with content type {content_type!r}")

    for asset in ASSETS:
        status, content_type, _ = http_get(port, f"/demo/assets/{asset}")
        if status != 200:
            fail(f"/demo/assets/{asset} returned {status}")
        expected_types = {
            ".js": {"text/javascript", "application/javascript"},
            ".css": {"text/css"},
            ".html": {"text/html"},
        }
        if content_type not in expected_types[Path(asset).suffix]:
            fail(f"/demo/assets/{asset} has unusable content type {content_type!r}")


def stop_server(process: subprocess.Popen[bytes]) -> None:
    if process.poll() is None:
        process.terminate()
        try:
            process.wait(timeout=20)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=5)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "dist",
        nargs="?",
        default="dist",
        type=Path,
        help="wheel or directory containing the wheel and sdist (default: dist)",
    )
    args = parser.parse_args()

    wheel, sdist = resolve_artifacts(args.dist)
    check_wheel(wheel)
    check_sdist(sdist)

    with tempfile.TemporaryDirectory(prefix="voice-bridge-install-") as temporary:
        workdir = Path(temporary).resolve()
        wheel_copy = workdir / wheel.name
        shutil.copy2(wheel, wheel_copy)
        python = create_venv(workdir)
        run(
            [
                str(python),
                "-m",
                "pip",
                "install",
                "--disable-pip-version-check",
                "--no-input",
                str(wheel_copy),
            ],
            cwd=workdir,
        )
        module = installed_module(python, workdir)
        if not module.is_relative_to(workdir):
            fail(f"imported voice_bridge from outside the virtual environment: {module}")

        port = free_port()
        process, log_path = start_server(workdir, port)
        try:
            wait_for_health(port, process, log_path)
            verify_endpoints(port)
        except BaseException:
            print(read_log(log_path), file=sys.stderr)
            raise
        finally:
            stop_server(process)
        print(f"installed wheel serves health, capabilities, demo and guide (port {port})")

    print("smoke install passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
