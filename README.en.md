# VoiceBridge

[![CI](https://github.com/likebeans/VoiceBridge/actions/workflows/ci.yml/badge.svg)](https://github.com/likebeans/VoiceBridge/actions/workflows/ci.yml)
[![Python 3.11+](https://img.shields.io/badge/Python-3.11%2B-blue)](pyproject.toml)
[![License: Apache-2.0](https://img.shields.io/badge/License-Apache--2.0-blue)](LICENSE)

[简体中文](README.md) · English

Realtime voice input for chat and agent applications. Built with Python, FastAPI, and WebSocket, VoiceBridge gives Qwen and FunASR behind a MaaS gateway a common streaming interface: clients send audio and receive live text snapshots, followed by a complete transcript when recording stops.

VoiceBridge is a transcription backend, not an offline recognizer. Audio is sent to the configured upstream provider and may be retained under that provider's policy. The service itself does not write audio or transcripts to disk. Final-user authentication, quotas, and sending text to an agent remain the responsibility of your business backend or gateway, which accesses VoiceBridge with a fixed `X-API-Key`.

Includes a browser demo, microphone recorder, and WAV streaming example. The backend accepts 16 kHz, 16-bit, mono PCM; the browser module handles capture, resampling, and encoding.

[Quick start](#quick-start) · [Defaults and limits](#defaults-and-limits) · [Frontend integration](docs/frontend-integration.md) · [API reference](docs/reference.md) · [Contributing](CONTRIBUTING.md)

## Quick start

Requirements: Python 3.11+ and [uv](https://docs.astral.sh/uv/).

```bash
git clone https://github.com/likebeans/VoiceBridge.git
cd VoiceBridge
uv sync --extra dev --locked
cp -n .env.example .env
uv run python -c 'import secrets; print(secrets.token_urlsafe(32))'
```

Put the generated value in `.env` as `SERVICE_API_KEY`. Choose one upstream configuration below and leave the other provider's key empty. You can configure both later with valid credentials.

Qwen:

```dotenv
DASHSCOPE_API_KEY=your-dashscope-api-key
QWEN_BASE_URL=wss://dashscope.aliyuncs.com/api-ws/v1/realtime
QWEN_MODEL=qwen3-asr-flash-realtime
```

FunASR through the MaaS gateway:

```dotenv
MAAS_API_KEY=your-maas-api-key
MAAS_BASE_URL=https://maas.marketingforce.com
FUNASR_MODEL=fun-asr-realtime
```

Create a Qwen key and find the workspace endpoint in the [Model Studio API key console](https://bailian.console.aliyun.com/cn-beijing/model/settings/api-key). The official docs currently show workspace URLs such as `wss://{WorkspaceId}.cn-beijing.maas.aliyuncs.com/api-ws/v1/realtime`. The project defaults to the DashScope endpoint above; confirm or replace it for your account.

The Qwen adapter has automated protocol tests, but **a real Qwen call has not been verified** in this repository yet. FunASR support targets the DashScope-style realtime task protocol of the specific MaaS gateway above. Self-hosted FunASR deployments using another protocol need a separate adapter. See the [gateway homepage](https://maas.marketingforce.com/) and [login page](https://maas.marketingforce.com/login); access requires an account and ASR model permission, with account availability determined by the gateway operator. Real WAV and browser sessions have completed through this gateway; see [verification status](#verification-status).

Start the backend:

```bash
HOST=127.0.0.1 uv run voice-bridge
```

Then, in a second terminal at the repository root, start the local-only demo proxy:

```bash
uv run python scripts/demo_proxy.py
```

It listens on `127.0.0.1:8080`, proxies to `http://127.0.0.1:8000`, and injects `SERVICE_API_KEY` from `.env`. Open <http://127.0.0.1:8080/demo>, choose a configured provider, allow the microphone, and record. The proxy is for local development only; production still needs your own authenticated gateway.

To test without a browser, prepare a 16 kHz, 16-bit, mono PCM WAV. In another terminal, run these commands and enter the same service key when prompted (input is hidden):

```bash
read -rs SERVICE_API_KEY
export SERVICE_API_KEY
uv run python examples/stream_wav.py /absolute/path/sample.wav \
  --url http://127.0.0.1:8000
```

When `--provider` is omitted, the script reads the protected capabilities endpoint and uses the configured provider. Only `done` means the session completed successfully.

## Defaults and limits

- Clients may omit `provider` or send `null`. All entry points then select a configured provider, preferring `qwen` when both are configured. An explicitly named but unconfigured provider is still rejected.
- Defaults: 100 connections, 120 s recording and audio limits, 16 KiB audio chunks, 5 s start timeout, 15 s finish timeout. All are configurable.
- Audio must be raw `pcm_s16le`, 16 kHz, mono. WAV headers and WebM/Opus are not accepted.
- Retries and optional cross-provider fallback happen only before the first audio chunk; there is no mid-session failover or replay.

## Verification status

Local verification completed on macOS ARM64 on 2026-11-14:

| Check | Result |
| --- | --- |
| Python 3.11 / 3.13 | 367 tests passed on each version, run in core-service and proxy batches |
| Browser modules (Node 22.7) | 70 tests passed |
| Ruff and formatting | Passed |
| wheel / sdist and fresh installation | Built successfully; API, demo, and guide assets worked in a fresh virtual environment |
| Docker | Image build and non-root runtime checks passed |
| Real FunASR / MaaS calls | WAV example and browser recording through the local proxy returned streaming results and `done` |
| Real Qwen connection | Not verified; adapter protocol tests are available |

The browser check used synthetic English audio and returned `This is a voice bridge open source release check.` through the actual MaaS gateway. This verifies the integration path, not multilingual recognition quality or production capacity.

[GitHub Actions](https://github.com/likebeans/VoiceBridge/actions/workflows/ci.yml) checks Python 3.11 / 3.13 on Linux, browser modules, linting, formatting, and fresh installation. See each commit's run for its results. CI does not use real provider keys. Historical load-test results are recorded in the [performance notes](docs/performance.md).

## Documentation

- [Detailed reference (Chinese)](docs/reference.md): protocol, error codes, configuration, deployment, metrics, historical verification.
- [Frontend integration (Chinese)](docs/frontend-integration.md): browser recorder, input-box example, proxy, states and acceptance.
- [Performance notes (Chinese)](docs/performance.md): local capacity, early audio and shutdown tests.
- [Contributing](CONTRIBUTING.md), [Security](SECURITY.md), [Changelog](CHANGELOG.md).

## License

[Apache License 2.0](LICENSE).
