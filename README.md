# 声桥（VoiceBridge）

[![CI](https://github.com/likebeans/VoiceBridge/actions/workflows/ci.yml/badge.svg)](https://github.com/likebeans/VoiceBridge/actions/workflows/ci.yml)
[![Python 3.11+](https://img.shields.io/badge/Python-3.11%2B-blue)](pyproject.toml)
[![License: Apache-2.0](https://img.shields.io/badge/License-Apache--2.0-blue)](LICENSE)

简体中文 · [English](README.en.md)

为聊天输入框和 agent 产品接入实时语音输入。VoiceBridge 基于 Python、FastAPI 和 WebSocket，将 Qwen 与 MaaS 网关上的 FunASR 统一为一套流式转写接口：客户端持续发送音频，服务返回实时文字快照，并在停止后收齐最终结果。

**定位**：只做转写后端。业务后端或网关通过固定的 `X-API-Key` 接入，最终用户的登录、权限、配额和“把文字发给 agent”仍由业务系统负责。本服务不保存音频或转写文本到磁盘，但音频会发往所配置的上游 ASR 服务——它是实时转写代理，不是离线识别。

提供可运行的浏览器试录页、麦克风采集模块和 WAV 调用示例。后端接收 16 kHz、16 bit、单声道 PCM；浏览器模块负责采集、重采样和编码。

[快速启动](#本机快速启动) · [能力与限制](#能力与限制) · [前端接入](docs/frontend-integration.md) · [接口与配置](docs/reference.md) · [参与贡献](CONTRIBUTING.md)

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

- **统一流式结果**：临时文字、分段定稿和完整快照；处理重复事件与迟到的段落信息。
- **完整收尾**：音频与 `stop` 按序转发，等待上游结束确认；未定稿或提前断开时明确报错。
- **有界资源**：默认 100 个连接、最长 120 秒录音/120 秒音频、16 KiB 单包、5 秒 `start` 等待、15 秒收尾等待；支持背压和启动重试。
- **多语言提示**：按准确模型 ID 提供语言清单，支持自动识别和显式语言提示；不做翻译，也不返回检测语言。
- **部署支持**：存活/就绪探针、Prometheus 指标、SIGTERM 排空、非 root Docker 镜像。
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
| [English README](README.en.md) | 简短英文启动说明 |
| [CONTRIBUTING](CONTRIBUTING.md) | Python / Node 检查、供应商扩展流程 |
| [SECURITY](SECURITY.md) | 私密漏洞报告方式 |
| [CHANGELOG](CHANGELOG.md) | 版本变更与默认行为变化 |

## 许可证

[Apache License 2.0](LICENSE)。
