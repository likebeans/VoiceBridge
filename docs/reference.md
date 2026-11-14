# 声桥参考文档

本文件保留接入、协议、配置、部署和测试所需的详细资料。首次启动请看 [README](../README.md)；浏览器录音与输入框接入见[前端接入文档](frontend-integration.md)；容量数据见[容量与延迟验证](performance.md)。英文简介见 [README.en.md](../README.en.md)。

文中“当前”指本仓库本次开源整理后的行为。历史验证数据均标注日期，不代表本轮重新运行的结果。

## 服务边界与数据处理

声桥是实时语音转文字代理，不是离线识别服务。服务本身不把音频或转写文本写入磁盘，应用日志也不记录密钥、音频或转写原文；但音频会发送到所配置的上游 ASR 服务，上游的留存、日志和处理方式受其自身政策与账号配置约束。不要在缺少上游数据约定时宣称“音频从未离开本机”。

浏览器试录页 `/demo` 的“联调信息”面板会显示服务端 WebSocket 事件，其中包含当前转写文字。这些事件只保存在当前页面内存，刷新页面即丢失，不是服务端日志，也不写入磁盘。Prometheus 指标不与会话 ID、文字或原始错误消息关联。

服务不校验浏览器 Origin，不负责最终用户登录、权限或配额。业务后端或网关通过固定的 `X-API-Key` 接入，最终用户鉴权仍由业务系统完成。

## 当前能力

| 供应商 | 默认模型 | 状态 |
| --- | --- | --- |
| Qwen | `qwen3-asr-flash-realtime` | 官方 realtime 入口适配器已实现；截至 2026-11-04 的自动化协议测试通过，真实官方 Qwen 直连尚未验证 |
| FunASR / MaaS | `fun-asr-realtime` | 适配器已实现；2026-11-14 已针对下文指定的 MaaS 网关完成一次真实流式转写，收到 final 文本与结束事件 |
| OpenAI / GPT | — | 尚未实现；已预留统一供应商接口 |

2026-11-14 的真实调用使用 2.483 秒合成英文音频，返回 final 文本 “This is a voice bridge open source release check.” 和 Finished 事件，未使用敏感录音。该次验证只确认指定 MaaS 网关的通路、流式结果与收尾，不代表识别质量、并发容量或所有声明语言。Qwen 已确认当前有效配置（包括环境变量覆盖）仍没有可用 Key，因此不能做真实调用验证。

Qwen 与 FunASR 适配器直连配置地址，不读取系统代理，不跟随重定向。部署环境需能直接访问对应上游。

**FunASR 的支持范围是有限的。** 当前适配器使用指定 MaaS 网关提供的 DashScope 风格实时任务协议（`/v1/audio/asr-realtime?model=...`），不是任意自建开源 FunASR 服务的通用客户端。自建 FunASR 若暴露不同的 WebSocket 或 HTTP 协议，需要单独实现适配器，不能只改 `MAAS_BASE_URL` 就假定兼容。

后端已具备：

- **统一流式结果**：临时文字、分段定稿和完整快照；处理重复事件与迟到的段落顺序信息。
- **完整收尾**：音频与 `stop` 按序转发，等待上游结束确认；结果未定稿或提前断开时明确报错。
- **有界资源使用**：并发上限、分片大小限制、录音与音频时长限制、各阶段超时和发送背压。
- **启动容错**：有限重试、可选供应商兜底，以及默认最多 3 秒的 `ready` 前音频缓冲。
- **多语言识别**：按准确模型 ID 提供语言能力，支持自动识别和显式语言提示。
- **部署支持**：固定 Key 的服务间接入、存活与就绪探针、Prometheus 指标、SIGTERM 排空和非 root 容器。

## 接口与协议

### 接口一览

| 方法 | 路径 | 需要 `X-API-Key` | 用途 |
| --- | --- | --- | --- |
| WebSocket | `/v1/asr` | 是 | 实时转写，每次录音建立一个连接 |
| GET | `/v1/asr/capabilities` | 是 | 供应商配置状态、模型语言列表、推荐供应商、音频格式和时长上限；不返回密钥 |
| GET | `/healthz` | 否 | 存活探针，进程正常时返回 200 |
| GET | `/readyz` | 否 | 就绪与负载查询；满载或排空时返回 503 |
| GET | `/metrics` | 是 | Prometheus 文本指标 |
| GET | `/demo` | 否 | 浏览器试录工具，实际转写需经注入 Key 的代理 |
| GET | `/guide` | 否 | 可阅读的前端接入文档 |
| GET | `/docs` | 否 | HTTP OpenAPI 文档；WebSocket 消息协议见下文 |

`/demo`、`/guide`、静态资源和 `/docs` 可直接打开，但页面不内嵌服务密钥。直接访问 `http://127.0.0.1:8000/demo` 能加载页面，但能力接口会返回 401，无法开始转写。

需要在终端查询能力接口时，可先输入服务接入密钥（输入不回显），再发起请求：

```bash
read -rs SERVICE_API_KEY
export SERVICE_API_KEY
curl -fsS -H "X-API-Key: $SERVICE_API_KEY" \
  http://127.0.0.1:8000/v1/asr/capabilities
```

### 业务后端接入

调用方在 HTTP 请求及 WebSocket 握手中添加 `X-API-Key: <SERVICE_API_KEY>`。服务要求单一请求头且值精确匹配，不接受查询参数、Cookie 或 `start` 消息中的密钥；服务接入密钥不会转发给模型供应商。

缺少或错误的密钥返回 HTTP 401。WebSocket 在升级前校验，鉴权失败不会创建转写会话、占用会话名额或连接上游，也不会返回 WebSocket `error` 消息。

Java 业务后端可在握手构建器中设置请求头（变量 `client` 为 `HttpClient`，`listener` 为 `WebSocket.Listener`）：

```java
client.newWebSocketBuilder()
    .header("X-API-Key", System.getenv("SERVICE_API_KEY"))
    .buildAsync(URI.create("wss://your-asr-host/v1/asr"), listener);
```

浏览器通过已完成用户鉴权的业务后端或网关接入，由后者保管并注入服务密钥。浏览器录音模块、试录页与代理部署方式见[前端接入文档](frontend-integration.md)。

### 音频约定

| 项目 | 要求 |
| --- | --- |
| 编码 | `pcm_s16le`，有符号 16 bit little-endian PCM |
| 采样率 / 声道 | 固定 16000 Hz / 单声道 |
| 传输形式 | WebSocket 二进制消息，内容为裸 PCM |
| 推荐分片 | 20–100 ms；50 ms 为 1600 字节，100 ms 为 3200 字节 |
| 分片约束 | 非空、偶数字节，且不超过 `ready.max_audio_chunk_bytes` |
| 音频时长 | 每 32000 字节对应 1 秒 |

不能直接发送 WAV 文件头、Base64 字符串或 WebM/Opus 容器。裸 PCM 不包含格式元数据，后端无法可靠推断真实采样率，调用方必须保证格式。浏览器采集、连续重采样与尾包发送可复用项目提供的录音模块，详见[前端接入文档](frontend-integration.md)。

### 会话流程

1. 携带 `X-API-Key` 握手连接 `/v1/asr`，升级成功后默认 5 秒内发送 `start`。
2. 收到 `ready`，开始发送二进制音频，同时接收 `transcript`。
3. 停止采集，发完尾部音频，再发送 `stop`。
4. 继续接收最后的转写结果，直到 `done`；随后服务以 1000 关闭连接。

客户端发送：

```json
{
  "type": "start",
  "provider": "qwen",
  "language": "zh",
  "audio": {"encoding": "pcm_s16le", "sample_rate": 16000, "channels": 1}
}
```

`provider` 可选 `qwen` 或 `funasr`。**省略或传 `null` 时，服务按当前配置选择供应商：两者都配置时优先 `qwen`，只配置 `funasr` 时选择 `funasr`。** 显式指定一个未配置的供应商仍返回 `provider_unavailable`，不会被静默改选。能力接口的 `default_provider` 使用与协议相同的选择规则。

`language` 省略、`null` 或 `"auto"` 表示不指定提示；`audio` 可省略，默认为上述固定格式。控制消息及 `audio` 对象拒绝未知字段。

各入口在省略 `provider` 时行为一致：

| 入口 | 省略 `provider` 时 |
| --- | --- |
| WebSocket `start` | 服务选择已配置的供应商，两者都有时优先 `qwen` |
| WAV 脚本 `--provider` | 脚本先读取受保护的能力接口，显式使用其 `default_provider` |
| 能力接口的 `default_provider` | 已配置的供应商，两者都有时优先 `qwen`，均无时为 `null` |
| 浏览器录音模块 `provider` | 模块先读取同源能力接口，显式发送 `default_provider`；显式传入时跳过读取 |
| 浏览器试录页 | 页面读取能力接口并让用户选择已配置项，开始录音时显式发送 |

上游会话建立并确认配置后，服务返回：

```json
{
  "type": "ready",
  "session_id": "<session-id>",
  "provider": "qwen",
  "audio": {"encoding": "pcm_s16le", "sample_rate": 16000, "channels": 1},
  "max_session_seconds": 120,
  "max_audio_seconds": 120,
  "max_audio_chunk_bytes": 16384
}
```

`ready.provider` 表示实际提供转写的供应商；发生启动兜底时，额外返回 `fallback_from`。自动选择得到的供应商会作为明确值出现在 `ready.provider`，不记为兜底。

发送音频期间，服务持续推送快照：

```json
{
  "type": "transcript",
  "session_id": "<session-id>",
  "sequence": 3,
  "segment_id": "<provider-item-id>",
  "segment_index": 0,
  "revision": 2,
  "text": "我想查一下订单。",
  "is_final": true,
  "full_text": "我想查一下订单。",
  "confirmed_text": "我想查一下订单。"
}
```

- `full_text` 是本次录音的完整当前快照，包含临时文字。**整体替换本次语音区域，不要逐次追加。**
- `text` / `is_final` 表示某个语音段的快照与定稿状态；一段定稿不代表录音结束。
- `confirmed_text` 只包含已定稿段，较早段落迟到时可能重新排序，也应整体替换。
- `sequence` 是本连接内的转写事件序号，`revision` 是该段的更新版本；`segment_index` 是当前已知顺序中的位置，可能随迟到信息变化。
- 文本按模型原文拼接，不额外插入段间空格、标点或换行。

尾包发送完毕后，客户端发送：

```json
{"type": "stop"}
```

`stop` 幂等，之后继续传音频会得到 `audio_after_stop`。服务按上游协议结束输入，并在收齐最终结果后返回：

```json
{"type": "done", "session_id": "<session-id>", "full_text": "我想查一下订单。", "audio_seconds": 2.4}
```

**只有 `done` 表示完整成功，单独收到正常关闭码 1000 也不能代替它。** 无有效语音时 `full_text` 可以为空；`audio_seconds` 按成功转发的 PCM 字节数计算。用户取消录音可直接关闭 WebSocket，服务会释放上游连接，不保证收回尚未定稿的文字。

### 错误与恢复

```json
{
  "type": "error",
  "session_id": "<session-id>",
  "code": "finish_timeout",
  "message": "Timed out waiting for final transcription",
  "confirmed_text": "已经确认的内容。"
}
```

按 `code` 做程序分支和本地化提示，保留已确认快照，舍弃未确认文字。网络中断、握手失败或传输层消息过大时，可能没有 JSON 错误；未收到 `done` 就应按中断处理。

| 错误码 | 含义与处理方向 |
| --- | --- |
| `provider_unavailable` | 显式选择的供应商未配置密钥；检查 `provider` 与服务配置 |
| `invalid_language` | 模型不支持该语言；按能力接口选择 |
| `invalid_start` / `invalid_message` / `invalid_audio` | 检查消息结构、顺序及 PCM 格式 |
| `message_too_large` / `pre_ready_overflow` | 缩小分片或限制建连期间的提前上传量 |
| `audio_after_stop` | 修正尾包与 `stop` 的发送顺序 |
| `start_timeout` / `idle_timeout` | 在时限内发送控制消息和音频；静音 PCM 也属于输入 |
| `audio_limit` / `session_limit` | 在音频或录音时限前主动停止；到达限制会报错，不会自动成功收尾 |
| `upstream_auth_error` / `upstream_rate_limited` | 检查上游密钥、地域、模型权限或配额；分别对应 HTTP 401/403 和 429 |
| `upstream_unavailable` / `upstream_rejected` | 上游临时不可用，或拒绝建立会话 |
| `upstream_connection_error` / `upstream_timeout` / `upstream_closed` | 上游连接失败、超时或提前结束 |
| `upstream_error` / `upstream_protocol_error` / `incomplete_transcript` / `finish_timeout` | 识别或收尾未完整完成，保留已定稿内容并提示重录 |
| `slow_client` | 客户端未及时接收结果，检查网络和消息消费 |
| `server_busy` / `shutting_down` | 本实例满载或正在排空；调用方可排队、退避或选择其他实例 |

满载和排空时，服务接受 WebSocket 后返回错误，再以 1013 关闭；此时 `session_id` 为 `null`，`confirmed_text` 为空。查询 `/readyz` 不预留连接名额，调用方仍需处理实际建连时的拒绝。

### 提前上传、重试与兜底

默认接入流程等 `ready` 再发音频。需要重叠录音与建连时，可在 `start` 后立即上传：服务最多缓存 `PRE_READY_BUFFER_SECONDS` 的音频，默认 3 秒 / 96000 字节，建立上游后按序转发；超限明确报错。提前到达的 `stop` 会在缓存音频发完后生效。客户端自身也应限制缓冲，避免网络拥塞导致无限积压。

每个供应商仅对网络断开、HTTP 500/502/503/504 或明确的临时服务故障做一次启动重试，带 50–150 ms 抖动。401/403、429 和超时不做同供应商重试。每次尝试默认最多 5 秒，所有尝试与兜底共享 12 秒的整体预算。

跨供应商兜底默认关闭。配置 `FALLBACK_PROVIDER=qwen` 或 `funasr` 并提供对应密钥后，主供应商启动失败且语言覆盖兼容时才会切换。**重试与切换均发生在第一包音频转发前**；录音中途失败不自动切换，也不自动重连重放整段音频，以免重复出字和重复计费。重新录音应建立新会话。

## 多语言识别

语言能力按**准确的模型 ID**维护，完整代码与名称见 `/v1/asr/capabilities` 的 `providers[].languages`。当前目录依据官方参数文档维护于 2026-11-04：

| 模型 | 已声明的语言范围 |
| --- | --- |
| `fun-asr-realtime` | 30 种语言，包括中文、英语、日语、韩语、法语、德语、西班牙语、阿拉伯语等 |
| `qwen3-asr-flash-realtime` | 27 个语言代码，含粤语 `yue`、土耳其语 `tr`、乌克兰语 `uk`、冰岛语 `is`；与 FunASR 范围不同 |
| `fun-asr-realtime-2026-02-28` | 中文 `zh`、英语 `en`、日语 `ja` |

`fun-asr-realtime` 的完整 30 种语言如下：

| 代码 | 语言 | 代码 | 语言 | 代码 | 语言 |
| --- | --- | --- | --- | --- | --- |
| `zh` | 中文 | `ar` | 阿拉伯语 | `fi` | 芬兰语 |
| `en` | 英语 | `fr` | 法语 | `no` | 挪威语 |
| `ja` | 日语 | `de` | 德语 | `el` | 希腊语 |
| `ko` | 韩语 | `es` | 西班牙语 | `pl` | 波兰语 |
| `vi` | 越南语 | `pt` | 葡萄牙语 | `cs` | 捷克语 |
| `th` | 泰语 | `ru` | 俄语 | `hu` | 匈牙利语 |
| `id` | 印尼语 | `it` | 意大利语 | `ro` | 罗马尼亚语 |
| `ms` | 马来语 | `nl` | 荷兰语 | `bg` | 保加利亚语 |
| `fil` | 菲律宾语 | `sv` | 瑞典语 | `hr` | 克罗地亚语 |
| `hi` | 印地语 | `da` | 丹麦语 | `sk` | 斯洛伐克语 |

语言代码忽略大小写和首尾空白，支持有限的常用别名，如 `en-US → en`、`ja-JP → ja`、`pt-BR → pt`。菲律宾语统一使用 `fil`，兼容 `tl`，适配器转换为供应商要求的代码。不是所有地区标签都可自动简写；中文地区标签不是繁简转换指令，FunASR 也不会把 `yue` 自动替换为 `zh`。

已知模型不支持的显式语言会在调用上游前返回 `invalid_language`，不重试或触发兜底。未知或自定义模型返回 `language_support_known: false`、`languages: []`、`auto_detect: null`；合法语言代码可直接交给上游，但服务不宣称已知其语言范围或自动识别能力。

启动兜底时，显式语言必须被备用模型支持；自动识别时，备用模型必须覆盖主模型的全部已知语言。**当前两个默认模型在自动模式下不会相互兜底**，使用 `zh`、`en`、`ja` 等共同语言提示时才可满足这一条件。未知模型不作为备用目标。

识别返回原语言文字，不做翻译，也不返回检测语言。`language` 是提示，不保证强制限定识别语言；官方语言目录也不等于已完成所有语言的识别质量测试。

## 配置参考

从项目工作目录的 `.env` 和环境变量读取配置，环境变量优先。模板见 [.env.example](../.env.example)，字段与校验规则见 [config.py](../src/voice_bridge/config.py)。

### 服务接入配置

| 环境变量 | 默认值 | 说明 |
| --- | --- | --- |
| `SERVICE_API_KEY` | 无，必填 | 32–256 个 ASCII 字母、数字、`_` 或 `-`；业务后端或网关通过 `X-API-Key` 发送，不向浏览器分发 |

配置值会去掉首尾空白，握手请求头则必须与处理后的密钥精确匹配。生成命令产出的随机值满足格式要求。

### 供应商配置

| 环境变量 | 默认值 | 说明 |
| --- | --- | --- |
| `DASHSCOPE_API_KEY` | 空 | Qwen 密钥，与 MaaS 密钥至少配置一个 |
| `QWEN_BASE_URL` | `wss://dashscope.aliyuncs.com/api-ws/v1/realtime` | Qwen WebSocket 地址，要求 `wss`，不能带凭证或 fragment |
| `QWEN_MODEL` | `qwen3-asr-flash-realtime` | Qwen 实时 ASR 模型 |
| `MAAS_API_KEY` | 空 | MaaS 网关密钥 |
| `MAAS_BASE_URL` | `https://maas.marketingforce.com` | HTTP(S) 基础地址，不能带凭证、query 或 fragment |
| `FUNASR_MODEL` | `fun-asr-realtime` | MaaS 实时 ASR 模型 |
| `FALLBACK_PROVIDER` | 空，关闭 | 可选 `qwen` / `funasr`；需配置其密钥并满足语言兼容条件 |
| `VAD_SILENCE_DURATION_MS` | `400` | 分段静音阈值，范围 200–2000 ms；FunASR 对应 `max_sentence_silence` |

Qwen 控制台可创建 API Key 并查看工作空间 Base URL：<https://bailian.console.aliyun.com/cn-beijing/model/settings/api-key>。官方实时文档目前给出工作空间域名，如 `wss://{WorkspaceId}.cn-beijing.maas.aliyuncs.com/api-ws/v1/realtime` 和 `ap-southeast-1` 区域版本；旧的 `dashscope.aliyuncs.com` 默认地址（北京地域）仍可正常使用，新加坡可使用 `wss://dashscope-intl.aliyuncs.com/api-ws/v1/realtime`。工作空间专属地址以百炼控制台为准。把控制台给出的地址填入 `QWEN_BASE_URL` 即可，无需修改代码；URL 中的 `model` 由服务根据 `QWEN_MODEL` 设置。密钥、模型权限和地址必须匹配地域。

FunASR 支持范围见[当前能力](#当前能力)：适配器面向指定 MaaS 网关的实时任务协议；服务在 `MAAS_BASE_URL` 后添加 `/v1/audio/asr-realtime?model=...`，不使用 `/v1/chat/completions`。网关公开首页为 <https://maas.marketingforce.com/>，获取密钥入口为 <https://maas.marketingforce.com/login>。使用需要对应网关账号和 ASR 模型权限；登录页未展示可核实的自助注册流程，本文不承诺任何人可免审核开通。适配器只实现该网关的 task 协议，不承诺兼容任意自建 FunASR。

降低静音阈值可更早定稿，也更容易切开自然停顿；它不等同于首字延迟。热词、ITN、标点控制和语气词过滤尚未验证到稳定效果，当前不提供这些配置项。

### 限额、超时与监听

以下时间单位均为秒，字段名含 `BYTES` 的为字节。

| 环境变量 | 默认值 | 说明 |
| --- | --- | --- |
| `HOST` | `0.0.0.0` | 监听地址 |
| `PORT` | `8000` | 监听端口 |
| `MAX_CONNECTIONS` | `100` | 单进程会话上限，包含等待 `start` 与上游建连的连接 |
| `MAX_SESSION_SECONDS` | `120` | `ready` 后到停止输入的实际录音时限，可配置 1–600 |
| `MAX_AUDIO_SECONDS` | `120` | 累计 PCM 音频时长上限，包含提前上传，可配置 1–600 |
| `START_TIMEOUT_SECONDS` | `5` | 等待首条 `start` 的时限 |
| `CONNECT_TIMEOUT_SECONDS` | `5` | 单次上游建立并确认会话配置的预算 |
| `OPEN_TIMEOUT_SECONDS` | `12` | 整体启动预算，包含重试和兜底 |
| `PRE_READY_BUFFER_SECONDS` | `3` | 建连期间的音频缓冲上限，大于 0 且不超过 10 |
| `IDLE_TIMEOUT_SECONDS` | `15` | 转写阶段等待下一条音频或 `stop` 的时限 |
| `FINISH_TIMEOUT_SECONDS` | `15` | 开始结束上游输入后的收尾预算，包含结束请求发送与结果等待 |
| `SEND_TIMEOUT_SECONDS` | `5` | 向上游发送或向客户端返回结果的单次超时 |
| `MAX_AUDIO_CHUNK_BYTES` | `16384` | 应用层单包音频上限，可配置 2–32768；实际音频仍须为偶数字节 |
| `MAX_CONTROL_MESSAGE_BYTES` | `8192` | 单条 JSON 控制消息上限，可配置 512–32768 |
| `SHUTDOWN_GRACE_SECONDS` | `180` | 停机时等待现有会话结束的窗口，可设为 0 |

录音时限与收尾时限分开计算。调整服务端启动或收尾预算时，也要同步调整调用方等待时间；浏览器模块通过 `timeouts.ready` / `timeouts.done` 配置，且需预留网络与尾包发送余量。

## 部署与监控

### Docker

```bash
docker build -t voice-bridge .
docker run --rm --env-file .env --stop-timeout 195 -p 8000:8000 voice-bridge
```

镜像以非 root 用户运行，默认启动 `voice-bridge`，并通过 `/healthz` 做容器存活检查。满载只影响就绪状态，不应被当成进程死亡。

线上通过支持 WebSocket Upgrade 的网关提供 HTTPS/WSS；网关的连接与空闲超时需要覆盖建连、最长录音和收尾阶段。业务后端或网关保管服务密钥，为转写及能力查询注入 `X-API-Key`；Prometheus 抓取 `/metrics` 时也需要该请求头。最终用户鉴权、用户级并发与排队由业务系统负责，探针与联调页面的路由范围在入口统一控制。

本机试录代理 `scripts/demo_proxy.py` 只监听回环地址，供开发联调使用，不是生产用户鉴权网关，不应直接暴露到网络。

### 容量与停机

默认单进程运行。CLI 额外限制 WebSocket 单消息 32 KiB、接收队列 8 条，并关闭消息压缩；应用层继续校验音频和控制消息大小。建连结束后串行转发音频并传播背压，不设置无限应用队列。

生产启动使用 **`voice-bridge`**。收到 SIGTERM 后，服务先将 `/readyz` 置为 503 并拒绝新会话，已有会话继续运行，直至完成或默认 180 秒的排空窗口耗尽；随后由 Uvicorn 关闭剩余连接，最多再清理 10 秒。Docker 示例的 195 秒停止等待覆盖这两个窗口。

调整录音、建连和收尾上限时，应同时检查 `SHUTDOWN_GRACE_SECONDS` 及容器或编排平台的终止等待时间。直接运行 `uvicorn ... --factory` 不会启用项目的定制 SIGTERM 排空逻辑。

多实例部署时，限额和指标都按进程计算；一条 WebSocket 在存续期间由同一实例处理。当前不支持断线续传、跨实例会话恢复或共享全局限流。

### 探针与指标

`/readyz` 返回 `status`（`ready` / `full` / `draining`）、`active_sessions` 和 `max_sessions`。可接入新会话时为 200，满载或排空时为 503；它不探测上游健康，也不预留名额。

| 指标 | 用途 |
| --- | --- |
| `asr_active_sessions` / `asr_draining` | 当前会话数与排空状态 |
| `asr_sessions_total{provider,outcome}` | 会话完成、失败或中断的结果分布 |
| `asr_sessions_rejected_total{reason}` | 本地满载与排空拒绝 |
| `asr_audio_seconds_total{provider}` | 成功转发的累计音频秒数 |
| `asr_ready_seconds` | 收到有效 `start` 后至 `ready` 的耗时 |
| `asr_upstream_open_seconds` | 每次上游尝试的耗时，含失败清理 |
| `asr_first_transcript_seconds` | 收到首包有效音频至首次非空文字发送完成 |
| `asr_finish_seconds` | 收到 `stop` 至 `done` 发送完成 |
| `asr_session_seconds` | 会话处理总耗时 |
| `asr_upstream_open_total` / `asr_upstream_retries_total` | 上游建连结果与启动重试次数 |
| `asr_provider_fallbacks_total` / `asr_close_codes_total` | 供应商兜底与关闭码分布 |

首字耗时包含开头静音及建连缓冲，不等同于模型推理耗时；没有有效文字的会话不产生首字样本。指标仅在进程内聚合，重启后重置，不以会话 ID、文字或原始错误消息作为标签。服务日志记录会话 ID、供应商、错误类别及允许列表内的上游错误码，不记录密钥、音频或转写原文；上游协议 DEBUG 线缆日志已禁用。

## 验证与测试

### 本轮实际验证（2026-11-14）

- **FunASR / MaaS**：使用指定 MaaS 网关和 2.483 秒合成英文音频完成真实调用，收到 final 文本 “This is a voice bridge open source release check.” 与 Finished 事件；无敏感录音。
- **Qwen**：检查有效配置（包括环境变量覆盖）后仍无可用 Key，未进行真实调用；其官方入口仍只有自动化协议测试证据，不能宣称已真实验证。
- **自动化检查**：CI 配置覆盖 pytest、Ruff 检查和格式检查、Node 测试及安装后冒烟；具体结果以对应提交的 CI 记录为准，本文不把历史数量当作本轮结果。

### 历史验证基线（2026-11-04）

以下记录迁移自开源整理前的旧 README，只说明当时环境中的结果，**不是本轮开源整理后的重新验证结果**。本轮自动化检查结果以对应提交的 CI 记录为准；涉及真实上游的记录需按实际密钥和账号重新确认。

| 验证项 | 结果与范围 |
| --- | --- |
| 后端自动化 | 当时 323 项通过，包含 24 项服务密钥鉴权用例，并覆盖协议、适配器、文本完整性、语言校验、超时、取消、容量与停机 |
| 静态检查 | 当时 Ruff 检查和格式检查通过 |
| 浏览器辅助模块 | 当时 62 项 Node 测试通过；Chrome 采集与重采样链路已验证，浏览器入口需业务代理 |
| 服务接入鉴权 | 真实进程验证：受保护 HTTP 接口拒绝无效密钥；WebSocket 无头、错头或重复头在升级前返回 401；正确密钥可完成 `ready → done` |
| 本机模拟容量 | 转写后端基线：100 会话正常完成；120 会话突发时 100 完成、20 明确拒绝；100 会话、20 ms 分片持续 60 秒测试通过。加入服务密钥后，压测入口另以 3 会话复测通过 |
| FunASR 真实调用 | 针对指定 MaaS 网关：中文及多种外语短合成音频获得流式结果和 `done`；覆盖日/法语自动识别、同次英日混说及空录音收尾 |
| 容器与停机 | Docker 构建、非 root 运行、健康检查和真实子进程 SIGTERM 排空已验证 |
| Qwen 真实直连 | 尚未验证，需要匹配地域且具有模型权限的 Key；官方入口适配器只有自动化协议测试 |

容量测试使用本机模拟上游，只证明服务在该测试条件下的转发与资源控制能力，不能推导真实网关配额、模型并发容量或生产延迟。真实调用使用短合成音频，尚未完成真人录音准确率、所有声明语言、长期稳定性或竞品质量对比评测。

详细数据、环境与统计口径见[容量与延迟验证](performance.md)。该文档中的测试仍有 Starlette / Uvicorn / websockets 的依赖弃用提示。

### 本地检查

```bash
uv run pytest -q
uv run ruff check .
uv run ruff format --check .
```

可选的浏览器模块测试需要 Node >= 22.7：

```bash
node --test tests/browser/*.test.mjs
```

自动化测试使用模拟供应商或本机 WebSocket 上游，不调用真实模型。CI 配置见 [.github/workflows/ci.yml](../.github/workflows/ci.yml)。

### 本地容量压测

```bash
uv run python scripts/loadtest.py --src-path src --connections 100 --duration 10 --ramp 5
uv run python scripts/loadtest.py --src-path src --connections 120 --duration 10 --ramp 0
```

压测脚本只启动本机模拟网关，使用假密钥，不读取项目 `.env`，不产生模型费用。更高分片频率、故障注入与提前上传的复现命令见[性能文档](performance.md)。

## 项目结构与扩展

```text
src/voice_bridge/
  main.py              # 路由、连接限额和停机排空
  config.py            # 环境变量、配置校验与默认供应商选择
  protocol.py          # 客户端消息校验
  languages.py         # 模型语言清单、别名及兜底兼容
  session.py           # 收发、超时、重试、停止和清理
  transcript.py        # 文字快照、段落顺序与去重
  metrics.py           # 进程内聚合指标
  healthcheck.py       # Docker 存活探针
  providers/
    base.py            # ASRProvider 与统一事件
    transport.py       # 直连、禁止重定向与握手拒绝分类
    qwen.py            # Qwen realtime 协议
    funasr.py          # 指定 MaaS 网关的 DashScope 任务协议
  web/                 # 试录页面、接入指南与浏览器采集模块
scripts/
  demo_proxy.py        # 仅本机开发用，注入服务密钥并代理能力查询与 WebSocket
```

接入新供应商时，实现 `ASRProvider` 的 `open`、`send_audio`、`finish`、`events` 和 `close`，将供应商事件归一为 `Transcript`、`SegmentOrder`、`Finished`，并扩展供应商枚举、工厂、能力接口及语言目录。客户端协议仍保持统一。

例如未来接 OpenAI，上游若要求 24 kHz，应由适配器对共享的 16 kHz PCM 输入做连续重采样；增量文字、分段关联和提交规则也由适配器转换，不能只替换 URL。

当前不提供说话人区分、逐字时间戳、会议录音存储、翻译或向 agent 自动发消息。这些能力可按产品需要单独扩展，不属于现有输入框实时转写协议。

## 官方资料

- [Qwen 实时语音识别交互流程](https://help.aliyun.com/zh/model-studio/qwen-asr-realtime-interaction-process)
- [Qwen 实时客户端事件](https://help.aliyun.com/zh/model-studio/qwen-asr-realtime-client-events)
- [Qwen 实时服务端事件](https://help.aliyun.com/zh/model-studio/qwen-asr-realtime-server-events)
- [FunASR 客户端事件](https://help.aliyun.com/zh/model-studio/fun-asr-client-events)
- [阿里云百炼 API Key 与工作空间地址](https://bailian.console.aliyun.com/cn-beijing/model/settings/api-key)
