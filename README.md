# VoiceBridge

[![CI](https://github.com/likebeans/VoiceBridge/actions/workflows/ci.yml/badge.svg)](https://github.com/likebeans/VoiceBridge/actions/workflows/ci.yml)
[![Python 3.11+](https://img.shields.io/badge/Python-3.11%2B-blue)](pyproject.toml)
[![License: Apache-2.0](https://img.shields.io/badge/License-Apache--2.0-blue)](LICENSE)

English · [简体中文](README.zh-CN.md)

**Add live speech-to-text input to your app.** VoiceBridge is a lightweight Python service for chat inputs, AI assistants, and other interactive applications. Built with FastAPI and WebSocket, it gives Qwen and FunASR behind a MaaS gateway a common streaming API: clients send audio, display live text, and receive a complete transcript when recording stops.

Includes a browser demo, microphone recorder, and WAV streaming example. The backend accepts 16 kHz, 16-bit, mono PCM; the browser module handles capture, resampling, and encoding.

[Use cases](#what-you-can-build) · [Features](#features) · [Quick start](#quick-start) · [Frontend integration](docs/frontend-integration.md) · [API reference](docs/reference.md) · [Contributing](CONTRIBUTING.md)

## What you can build

- **Voice input for chat and agent apps**: let users speak into a microphone and watch text appear in the input box. They can edit the transcript before your application sends it to an agent.
- **Dictation for short notes, forms, and search**: reuse the browser recorder to turn speech into editable text, with automatic recognition or an explicit language hint where the model supports it.
- **A shared transcription backend**: connect applications that can stream PCM over WebSocket. Use the same transcript format, capabilities endpoint, and error handling across supported providers.
- **An environment for trying speech integrations**: use the included browser demo or WAV example to check microphone capture, upstream connectivity, streaming results, and finalization.

Sessions default to a 120-second recording limit, suitable for voice input and short dictation. Duration and concurrency limits are configurable.

## Features

- **Live transcripts and complete snapshots**: receive partial results, finalized segments, `full_text`, and `confirmed_text` for updating your UI.
- **Explicit completion**: audio and stop requests stay ordered; `done` is sent after final results arrive. Missing final results and early disconnects produce explicit errors.
- **Provider and language capabilities**: query `/v1/asr/capabilities` for configured providers, models, and supported languages, then use one WebSocket protocol.
- **Reusable browser tooling**: a framework-independent recorder module, AudioWorklet resampling, local demo, and WAV streaming example.
- **Bounded sessions**: concurrency, duration, message-size, and timeout limits, plus backpressure, startup retries, and optional fallback during startup.
- **Operational tools**: `/healthz`, `/readyz`, Prometheus `/metrics`, graceful SIGTERM draining, and a non-root Docker image.

## How it fits into your app

1. **Your client captures audio.** The browser recorder converts microphone input to 16 kHz, 16-bit, mono PCM and sends it through your backend or gateway.
2. **Your gateway manages users.** It checks authentication, permissions, and quotas, then injects a fixed `X-API-Key` when connecting to VoiceBridge. Keys stay on the server.
3. **VoiceBridge handles transcription.** It connects to your configured upstream ASR service, manages the session, forwards audio, and normalizes recognition results.
4. **Your app uses the text.** Display live results and use the final transcript after `done`. Your application decides whether to save a note, call an agent, or run a search.

Audio is sent to the configured upstream service. VoiceBridge itself does not store audio or transcripts, generate AI replies, or synthesize speech. The included local proxy supports trying the demo; production integrations use your business gateway.

## Quick start

Requirements: Python 3.11+ and [uv](https://docs.astral.sh/uv/). The repository's `.python-version` and Docker image default to Python 3.13.

```bash
git clone https://github.com/likebeans/VoiceBridge.git
cd VoiceBridge
uv sync --extra dev --locked
cp -n .env.example .env
uv run python -c 'import secrets; print(secrets.token_urlsafe(32))'
```

Put the generated value in `.env` as `SERVICE_API_KEY`. This is the key your backend uses to access VoiceBridge, separate from the upstream model key; keep it out of frontend code, URLs, and browser storage. Choose one upstream configuration below and leave the other provider's key empty. You can configure both with valid credentials.

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

The command binds to loopback for local development. Without the `HOST` override, the default is `0.0.0.0:8000`. In another terminal, check the probes:

```bash
curl -fsS http://127.0.0.1:8000/healthz
curl -fsS http://127.0.0.1:8000/readyz
```

These report local process and configuration status; they do not verify upstream credentials, model access, or quota.

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

The WAV example reads `SERVICE_API_KEY` from the environment, not from `.env`. When `--provider` is omitted, it reads the protected capabilities endpoint and uses the configured provider. Only `done` means the session completed successfully. Use `--provider funasr` or `--language ja` to select a provider or language explicitly.

## Defaults and limits

- Clients may omit `provider` or send `null`. All entry points then select a configured provider, preferring `qwen` when both are configured. An explicitly named but unconfigured provider is still rejected.
- Defaults: 100 connections, 120 s recording and audio limits, 16 KiB audio chunks, 5 s start timeout, 15 s finish timeout. All are configurable.
- Audio must be raw `pcm_s16le`, 16 kHz, mono. WAV headers and WebM/Opus are not accepted.
- Retries and optional cross-provider fallback happen only before the first audio chunk; there is no mid-session failover or replay.
- Language support is tied to the exact model ID. The service does not translate text or return the detected language.
- Speaker diarization, word-level timestamps, recording storage, reconnect/resume, and cross-instance session recovery are not provided.

## Data handling

- The service does not write audio or transcripts to disk. Application logs do not record keys, audio, or transcript contents.
- Audio is sent to the configured ASR provider. Retention, data use, and processing location depend on that provider's policies, region, and account settings.
- The browser demo's event panel displays the current WebSocket events, including transcript text, in page memory. Refreshing clears them; this is separate from server logging.
- `/metrics` contains process-level aggregates and does not use session IDs, text, or raw error messages as metric labels.


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

- [中文 README](README.zh-CN.md): project overview, use cases, and quick start in Simplified Chinese.
- [Detailed reference (Chinese)](docs/reference.md): protocol, error codes, configuration, deployment, metrics, historical verification.
- [Frontend integration (Chinese)](docs/frontend-integration.md): browser recorder, input-box example, proxy, states and acceptance.
- [Performance notes (Chinese)](docs/performance.md): local capacity, early audio and shutdown tests.
- [Contributing](CONTRIBUTING.md), [Security](SECURITY.md), [Changelog](CHANGELOG.md).

## License

[Apache License 2.0](LICENSE).
