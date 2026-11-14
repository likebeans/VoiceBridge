# 声桥（VoiceBridge）

[![CI](https://github.com/likebeans/VoiceBridge/actions/workflows/ci.yml/badge.svg)](https://github.com/likebeans/VoiceBridge/actions/workflows/ci.yml)
[![Python 3.11+](https://img.shields.io/badge/Python-3.11%2B-blue)](pyproject.toml)
[![License: Apache-2.0](https://img.shields.io/badge/License-Apache--2.0-blue)](LICENSE)

[English](README.md) · 简体中文

**为你的应用接入实时语音输入。** 声桥是基于 Python、FastAPI 和 WebSocket 的轻量语音转文字后端，可用于聊天输入框、AI 助手和其他交互式应用。它将 Qwen 与 MaaS 网关上的 FunASR 统一为一套流式接口：客户端持续发送音频，页面实时显示文字，停止后收齐最终结果。

提供可运行的浏览器试录页、麦克风采集模块和 WAV 调用示例。后端接收 16 kHz、16 bit、单声道 PCM；浏览器模块负责采集、重采样和编码。

[项目用途](#可以用来做什么) · [核心能力](#核心能力) · [快速启动](#本机快速启动) · [前端接入](docs/frontend-integration.md) · [接口与配置](docs/reference.md) · [参与贡献](CONTRIBUTING.md)

## 可以用来做什么

- **给聊天和 agent 产品增加语音输入**：用户点击麦克风说话，输入框随识别结果更新；录音结束后，用户可以编辑文字，再由业务产品决定何时发送给 agent。
- **听写短笔记、表单内容和搜索词**：复用浏览器录音模块，将语音转成可编辑文本；按模型能力选择自动识别或指定语言。
- **为多个应用共用一套转写后端**：能通过 WebSocket 发送 PCM 的客户端均可接入，使用相同的结果结构、能力查询和错误处理，切换供应商时无需重写结果解析。
- **验证语音接入流程**：使用自带的浏览器 Demo 或 WAV 示例检查麦克风采集、上游连接、流式文字和停止后的完整结果。

默认每次录音上限为 120 秒，适合语音输入和短时听写；时长与并发限制可以配置。

## 核心能力

- **实时文字与完整快照**：提供临时结果、分段定稿、`full_text` 和 `confirmed_text`，便于页面持续更新文字。
- **明确的完成信号**：按顺序转发音频与停止请求，收齐上游结果后发送 `done`；缺少定稿或连接提前断开时明确报错。
- **统一供应商与语言能力**：通过 `/v1/asr/capabilities` 查询已配置的服务、模型和语言清单，使用同一套 WebSocket 协议接入。
- **可复用的浏览器模块**：提供不依赖前端框架的录音模块、AudioWorklet 重采样、本机试录页和 WAV 调用示例。
- **有界的会话资源**：支持并发、时长、消息大小和各阶段超时限制，以及发送背压、启动重试和可选的启动兜底。
- **部署与运维工具**：提供 `/healthz`、`/readyz`、Prometheus `/metrics`、SIGTERM 排空和非 root Docker 镜像。

## 如何接入你的应用

1. **客户端采集音频**：浏览器录音模块把麦克风输入转换成 16 kHz、16 bit、单声道 PCM，经业务后端或网关发送。
2. **业务网关管理用户**：校验登录、权限和配额，在连接 VoiceBridge 时注入固定的 `X-API-Key`；密钥保留在服务端。
3. **VoiceBridge 负责转写**：连接所配置的上游 ASR 服务，管理会话、转发音频，并归一化识别结果。
4. **应用消费文字**：实时展示结果，收到 `done` 后使用最终文本；保存笔记、调用 agent 或执行搜索由应用决定。

音频会发送到所配置的上游服务。VoiceBridge 本身不保存音频和转写文本，也不生成 AI 回复或语音播报。本机试录可使用仓库提供的开发代理；生产接入使用你的业务网关。

## 本机快速启动

需要 Python 3.11+ 和 [uv](https://docs.astral.sh/uv/)。`.python-version` 与 Docker 默认使用 Python 3.13。

```bash
git clone https://github.com/likebeans/VoiceBridge.git
cd VoiceBridge
uv sync --extra dev --locked
cp -n .env.example .env
```

### 1. 生成服务接入密钥

```bash
uv run python -c 'import secrets; print(secrets.token_urlsafe(32))'
```

把结果写入 `.env` 的 `SERVICE_API_KEY`。这是业务后端访问本服务的固定密钥，不是模型密钥；不要写入前端代码、URL 或浏览器存储。

```dotenv
SERVICE_API_KEY=replace-with-your-generated-service-key
```

### 2. 配置至少一个上游

**Qwen（官方入口）**

在[百炼控制台 API Key 页面](https://bailian.console.aliyun.com/cn-beijing/model/settings/api-key)创建 Key，并查看工作空间实时地址。官方文档当前示例为 `wss://{WorkspaceId}.cn-beijing.maas.aliyuncs.com/api-ws/v1/realtime`（或 `ap-southeast-1`）；项目默认使用下面的 DashScope 地址，请按账号实际配置确认或替换。

```dotenv
DASHSCOPE_API_KEY=your-dashscope-api-key
QWEN_BASE_URL=wss://dashscope.aliyuncs.com/api-ws/v1/realtime
QWEN_MODEL=qwen3-asr-flash-realtime
```

官方入口适配器已实现并有自动化协议测试，**尚未完成真实 Qwen 直连验收**。使用前需确认 Key、地域、模型权限和地址匹配。参考[官方交互流程](https://help.aliyun.com/zh/model-studio/qwen-asr-realtime-interaction-process)。

**FunASR / MaaS**

```dotenv
MAAS_API_KEY=your-maas-api-key
MAAS_BASE_URL=https://maas.marketingforce.com
FUNASR_MODEL=fun-asr-realtime
```

FunASR 通过**指定 MaaS 网关**的 DashScope 风格实时任务协议接入，服务在基础地址后添加 `/v1/audio/asr-realtime?model=...`。自建开源 FunASR 若使用不同协议，需要单独实现适配器。在[网关官网](https://maas.marketingforce.com/)查看服务，通过[登录入口](https://maas.marketingforce.com/login)获取凭据；需要对应账号及 ASR 模型权限，账号开通方式以网关方为准。已通过该网关完成真实 WAV 调用和浏览器经本机代理的流式转写，详见[验证状态](#验证状态)。

两个供应商至少配置一个，也可以都配置。所有入口省略 `provider`（或传 `null`）时都选择已配置的供应商，两者都有时优先 `qwen`；显式指定未配置的供应商仍会报 `provider_unavailable`。

### 3. 启动后端

```bash
HOST=127.0.0.1 uv run voice-bridge
```

默认监听 `0.0.0.0:8000`，本机联调建议显式绑定回环地址。另开终端检查探针：

```bash
curl -fsS http://127.0.0.1:8000/healthz
curl -fsS http://127.0.0.1:8000/readyz
```

探针成功只说明本地进程和配置状态，不代表上游 Key 有效、额度充足或模型可用。

### 4. 在本机麦克风试录

另开一个终端，仍在项目根目录：

```bash
uv run python scripts/demo_proxy.py
```

默认监听 `127.0.0.1:8080`，代理到 `http://127.0.0.1:8000`，读取 `.env` 中的 `SERVICE_API_KEY` 并在转发能力查询与 WebSocket 时注入。浏览器打开：

```text
http://127.0.0.1:8080/demo
```

选择已配置的识别服务，允许麦克风，看到“正在录音”后再说话；停止后等待“转写完成”。录音需要 localhost 或 HTTPS。

这个代理**只用于本机开发**，只监听回环地址，不是生产用户鉴权网关。生产环境仍应由业务后端或受控网关校验最终用户权限并注入服务密钥，不能把 `demo_proxy.py` 暴露到公网。

### 5. 用 WAV 文件验证

准备 16 kHz、16 bit、单声道 PCM WAV。在新终端执行以下命令，输入同一个 `SERVICE_API_KEY` 后按回车（输入不回显）：

```bash
read -rs SERVICE_API_KEY
export SERVICE_API_KEY
uv run python examples/stream_wav.py /absolute/path/sample.wav \
  --url http://127.0.0.1:8000
```

脚本从环境变量读取 `SERVICE_API_KEY`，不读取 `.env`；省略 `--provider` 时会先读取受保护的能力接口，使用已配置的供应商。只有收到 `done` 才算完整成功。也可以追加 `--provider funasr`、`--language ja` 等显式参数。

## 能力与限制

- **输入格式**：后端只接受原始 `pcm_s16le`、16 kHz、单声道音频，不接受 WAV 文件头或 WebM/Opus；浏览器模块和 WAV 示例负责转换或提取 PCM。
- **重试边界**：重试与可选的跨供应商兜底仅发生在第一段音频发送前；没有会话中途切换或音频重放。
- **有界资源**：默认 100 个连接、最长 120 秒录音/120 秒音频、16 KiB 单包、5 秒 `start` 等待、15 秒收尾等待；支持背压和启动重试。
- **多语言提示**：按准确模型 ID 提供语言清单，支持自动识别和显式语言提示；不做翻译，也不返回检测语言。
- **当前未提供**：说话人区分、逐字时间戳、录音存储、翻译、断线续传、跨实例会话恢复，或自动把文字发送给 agent。

完整字段、错误码、配置项、Docker 部署和容量说明见**[详细参考文档](docs/reference.md)**。浏览器麦克风采集、输入框集成与错误处理见**[前端接入文档](docs/frontend-integration.md)**。

## 数据处理

- 服务不把音频或转写文本写入磁盘；应用日志不记录密钥、音频或转写原文。
- 音频会发送到所配置的上游 ASR 服务。上游是否留存、如何使用或跨境传输，按其自身政策、地域和账号配置处理；部署前应确认这些约定。
- 浏览器试录页的“联调信息”事件面板会显示当前 WebSocket 事件（含转写文字），只存在于页面内存，刷新即清空；这不是服务端日志。
- `/metrics` 只做进程内聚合，不以会话 ID、文字或原始错误消息作为标签。

## 验证状态

2026-11-14 在 macOS ARM64 完成的本地验证：

| 检查 | 结果 |
| --- | --- |
| Python 3.11 / 3.13 | 每个版本 367 项测试通过，按核心服务与代理模块分批运行 |
| 浏览器模块（Node 22.7） | 70 项测试通过 |
| Ruff 与格式检查 | 通过 |
| wheel / sdist 与安装烟测 | 构建通过；全新虚拟环境安装后接口、Demo、指南资源可用 |
| Docker | 镜像构建与非 root 运行检查通过 |
| FunASR / MaaS 真实调用 | WAV 示例、浏览器采集经本机代理均收到流式结果及 `done` |
| Qwen 真实直连 | 尚未验证；已有适配器协议测试 |

浏览器验收使用合成英文音频，经实际 MaaS 网关返回文字：`This is a voice bridge open source release check.`。该结果验证调用链路，不代表多语言识别质量或生产容量。

[GitHub Actions](https://github.com/likebeans/VoiceBridge/actions/workflows/ci.yml) 持续检查 Linux 上的 Python 3.11 / 3.13、浏览器模块、静态检查及安装烟测；运行结果以对应提交记录为准。CI 不使用真实供应商密钥。历史压测数据见[容量与延迟验证](docs/performance.md)。

## 文档与项目

| 文档 | 内容 |
| --- | --- |
| [详细参考](docs/reference.md) | 接口、WebSocket 协议、全部错误码、配置、部署、指标、历史验证与扩展 |
| [前端接入](docs/frontend-integration.md) | 浏览器录音模块、输入框示例、代理、状态与验收 |
| [HTML 接入指南](src/voice_bridge/web/guide.html) | 服务内 `/guide` 的源文件 |
| [容量与延迟验证](docs/performance.md) | 本机模拟压测、提前上传、故障与停机数据 |
| [English README](README.md) | 英文项目介绍与启动说明 |
| [CONTRIBUTING](CONTRIBUTING.md) | Python / Node 检查、供应商扩展流程 |
| [SECURITY](SECURITY.md) | 私密漏洞报告方式 |
| [CHANGELOG](CHANGELOG.md) | 版本变更与默认行为变化 |

## 许可证

[Apache License 2.0](LICENSE)。
