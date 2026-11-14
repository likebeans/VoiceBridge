# 声桥前端接入

声桥把麦克风音频转为文字，适合接入聊天框、搜索框或表单。前端通过 WebSocket 发送音频，收到当前文字快照后更新本次语音区域；消息发送、保存和后续业务操作由现有产品处理。

服务支持 `qwen` 与 `funasr`，两者使用相同的前端协议。示例显式发送 `provider: "funasr"`；省略或传 `null` 时，各入口都会选择已配置的供应商，两者都有时优先 `qwen`。实际部署以 `/v1/asr/capabilities` 的返回为准。本文示例数据用于说明协议，不代表某次真实识别的结果；Qwen 官方入口尚无真实调用验证。

## 数据与日志

本服务不把音频或转写文本写入磁盘，应用日志也不记录密钥、音频或转写原文。但音频会发送到所配置的上游 ASR 服务，上游的留存、日志和处理方式受其自身政策与账号配置约束；这是实时转写代理，不是离线识别。

试录页的“联调信息”事件面板会显示当前 WebSocket 事件，其中包含转写文字。它只存在于当前页面内存，刷新即清空，不是服务端日志。接入产品时应按自己的隐私与留存要求处理输入框内容。


## 目录

- [数据与日志](#数据与日志)
- [先完成一次真实录音](#先完成一次真实录音)
- [地址与可用能力](#地址与可用能力)
- [多语言与自动识别](#多语言与自动识别)
- [用录音模块接入输入框](#用录音模块接入输入框)
- [完整 WebSocket 协议](#完整-websocket-协议)
- [音频采集与停止顺序](#音频采集与停止顺序)
- [页面状态与文本归属](#页面状态与文本归属)
- [错误与恢复](#错误与恢复)
- [联调验收清单](#联调验收清单)

## 先完成一次真实录音

1. 在项目根目录安装依赖并启动服务。服务端 `.env` 必须配置固定的 `SERVICE_API_KEY` 及至少一个供应商密钥；前端不填写这些密钥。

   ```bash
   uv sync --extra dev --locked
   HOST=127.0.0.1 uv run voice-bridge
   ```

2. 本机联调时，另开一个终端启动仓库自带代理，然后打开 `http://127.0.0.1:8080/demo`：

   ```bash
   uv run python scripts/demo_proxy.py
   ```

   代理默认从 `http://127.0.0.1:8000` 转发到本服务、监听 `127.0.0.1:8080`，并读取 `.env` 中的 `SERVICE_API_KEY` 注入能力查询与 WebSocket。它只监听回环地址，仅用于开发，不是生产用户鉴权网关。生产环境仍应由业务后端或受控网关校验最终用户权限并注入 `X-API-Key`。直接访问 `http://localhost:8000/demo` 可加载页面，但没有代理注入请求头时会显示鉴权提示，不能录音。
3. 选择页面标为已配置的识别服务（默认按能力接口选择），语言默认“自动识别”，也可从菜单指定日语、法语等语言。点击开始，允许麦克风权限。等页面进入“正在录音”再说话。
4. 说两句话，中间短暂停顿。观察文字更新；点击停止后等待最后一句定稿和 `done`，再编辑或使用结果。
5. 在浏览器开发者工具的 Network → WS 中查看 `/v1/asr`：应依次看到 `start`、`ready`、二进制音频、`transcript`、`stop`、`done`。中间可以出现多次转写更新。
6. 再测试一次取消、一次拒绝麦克风权限，以及一次录音中断网，验收预期见文末。

`/demo` 是真实麦克风测试入口；`/guide` 是服务内的阅读页。本文的纯阅读版本只用于阅读和复制代码，不能访问麦克风或连接你的 localhost WebSocket。需要在独立浏览器标签页访问本地 `/demo`。

浏览器录音需要安全上下文：同一台电脑上的 `http://localhost` 或 `http://127.0.0.1` 通常可用，线上应使用 HTTPS，并配套 WSS。手机中的 `localhost` 指手机自身；把地址改成电脑的局域网 IP 后，普通 HTTP 通常仍无法获得麦克风权限。手机联调需可访问且证书受浏览器信任的 HTTPS 地址，并让该地址转发 HTTP 与 WebSocket 到后端。不要用 `file://` 直接打开页面。

这些步骤是联调方法，并非“已在所有浏览器、设备或真实上游通过”的声明。是否完成真实测试，应以本次浏览器的实际文字和 `done` 事件为准。

## 地址与可用能力

| 地址 | 用途 |
| --- | --- |
| `GET /healthz` | 返回 `{"status":"ok"}`，只表示服务存活 |
| `GET /v1/asr/capabilities` | 需要 `X-API-Key`；返回供应商配置状态、模型语言列表、推荐供应商、固定音频格式和时长限制 |
| `WS /v1/asr` | 握手需要 `X-API-Key`；实时转写，每次录音建立一个新连接 |
| `GET /metrics` | 需要 `X-API-Key`；Prometheus 聚合指标 |
| `GET /readyz` | 免鉴权的接入状态探针，满载或排空返回 503 |
| `GET /demo` | 真实浏览器麦克风测试页 |
| `GET /guide` | 本接入文档的 HTML 阅读页 |
| `/demo/assets/recorder.js` | 可复用的浏览器录音与会话模块 |
| `/demo/assets/pcm-worklet.js` | 音频采集 Worklet |
| `/demo/assets/pcm-resampler.js` | 连续重采样模块，供 Worklet 使用 |

本服务使用固定的服务间 API Key：`.env` 配置 `SERVICE_API_KEY`，业务后端在 `/v1/asr` 的 WebSocket 握手、能力接口和指标接口中传入 `X-API-Key: <固定Key>`。头名称不区分大小写，值精确匹配，每次请求只能有一个该头；缺少、错误或重复时返回 HTTP 401。WebSocket 在升级前拒绝，不返回 ready、不创建上游会话、不占用转写并发。Key 只从请求头读取，不支持 URL、cookie 或 start 消息传入；配置缺失会阻止服务启动。

浏览器原生 `WebSocket` 不能设置自定义请求头。前端连接业务后端或受控网关，再由它检查用户权限并在转发时注入固定 Key；浏览器仍使用原有录音模块，不携带这个服务 Key。代理应覆盖 HTTP 能力读取和 WebSocket Upgrade。`/demo`、`/guide`、静态资源和 `/docs` 可直接打开，但页面不会内嵌 Key，也没有绕过鉴权的试录通道。`/docs` 的 Authorize 可供服务端联调时输入 Key。`/healthz`、`/readyz` 免鉴权，便于健康探针。

服务 Key 与模型供应商 Key 独立，均只保存在后端，不写入前端源码、URL、浏览器存储或日志。HTTP 接入使用 HTTPS，WebSocket 使用 WSS。本服务不检查浏览器 Origin，也不替代业务后端的用户登录与权限控制。

能力响应示例（配置组合和数字仅用于说明；下面语言数组为节选，实际接口会返回完整清单）：

```json
{
  "providers": [
    {
      "id": "qwen", "model": "qwen3-asr-flash-realtime", "configured": false,
      "languages": [{"code": "zh", "name": "中文"}, {"code": "ja", "name": "日语"}],
      "auto_detect": true, "language_support_known": true
    },
    {
      "id": "funasr", "model": "fun-asr-realtime", "configured": true,
      "languages": [{"code": "zh", "name": "中文"}, {"code": "fr", "name": "法语"}],
      "auto_detect": true, "language_support_known": true
    }
  ],
  "default_provider": "funasr",
  "audio": {"encoding": "pcm_s16le", "sample_rate": 16000, "channels": 1},
  "max_session_seconds": 120,
  "max_audio_seconds": 120
}
```

- `providers[].id`：`start.provider` 可选的标识。
- `providers[].model`：服务端当前配置的模型名。前端不能通过 `start.model` 覆盖；多余字段会被拒绝。
- `providers[].configured`：对应供应商的服务端凭据是否已配置，**不保证上游连通、余额、权限或识别效果**。这个接口不发起模型调用。
- `providers[].languages`：当前模型完整的 `{code, name}` 语言清单，用于生成菜单；代码是本服务的统一代码。
- `providers[].auto_detect`：已知模型为 `true`；未知或自定义模型为 `null`，表示未确认。
- `providers[].language_support_known`：是否有准确模型 ID 对应的语言清单；为 `false` 时 `languages` 为空，不应展示为“全部语言”。
- `default_provider`：推荐给页面的已配置供应商。有 Qwen 配置时选 `qwen`，否则有 FunASR 配置时选 `funasr`，均未配置时为 `null`。正常服务启动要求至少配置一个供应商。
- `audio`：当前固定格式。时长限制是服务端配置值，当前会话以 `ready` 为准。

**默认选择规则一致：**能力接口的 `default_provider`、WebSocket `start` 省略或传 `null` 的 `provider`、WAV 脚本省略 `--provider`、录音模块省略 `provider`，都选择已配置的供应商；两者都配置时优先 `qwen`，只配置 FunASR 时选择 `funasr`。显式指定未配置的供应商仍会得到 `provider_unavailable`，不会被静默改选。页面应展示能力接口返回的配置状态，并显式发送所选供应商；试录页已经这样做。

下面的浏览器示例连接同源业务代理，由代理在转发到本服务时设置 `X-API-Key`。页面通过 `/v1/asr/capabilities` 获取能力，并将自身 HTTP/HTTPS 地址转换为 WS/WSS：

```javascript
const response = await fetch("/v1/asr/capabilities");
if (!response.ok) throw new Error(`能力接口失败：${response.status}`);
const capabilities = await response.json();
const provider = capabilities.default_provider;
if (!provider) throw new Error("服务端尚未配置语音供应商");

const url = new URL("/v1/asr", window.location.href);
url.protocol = url.protocol === "https:" ? "wss:" : "ws:";
// 使用 url.href 和显式的 provider 创建录音会话。
```

如果前端开发服务器和后端端口不同，将 `/v1/asr`（包含 WebSocket Upgrade）与 `/v1/asr/capabilities` 代理到后端，并由可信的服务器端代理从环境变量注入 Key；不能把 Key 打包到前端开发配置中。录音模块放到前端静态目录。当前后端没有配置跨源 HTTP CORS；能建立跨源 WebSocket 不代表跨源 `fetch` 或模块导入也能成功。HTTPS 页面应连接 `wss://`，避免混合内容被浏览器拦截。

## 多语言与自动识别

页面和录音模块默认不指定语言提示。`start.language` 省略、`null` 或 `"auto"` 均为自动识别；已知录音语言时可传 `"ja"`、`"ko"`、`"fr"`、`"ar"` 等代码。服务识别原语言文字，不做翻译，不返回检测语言；不要把请求的提示显示为“检测到的语言”。

当前 `fun-asr-realtime` 文档支持 30 种语言，`qwen3-asr-flash-realtime` 提供 27 个语言代码，菜单必须读取对应供应商的完整能力列表。两份清单不同，例如 Qwen 包含 `yue`（粤语）、`tr`（土耳其语）、`uk`（乌克兰语）、`is`（冰岛语），FunASR 包含 `nl`（荷兰语）、`el`（希腊语）等；FunASR 不提供独立 `yue` 提示。版本也会影响范围，`fun-asr-realtime-2026-02-28` 仅列出 `zh/en/ja`。

代码忽略大小写和首尾空白；服务兼容常用别名，如 `en-US → en`、`ja-JP → ja`、`pt-BR → pt`。并非所有地区标签都自动截取前缀。菲律宾语统一传 `fil`，也兼容 `tl`，服务自动适配 FunASR 的 `tl` 与 Qwen 的 `fil`。`zh-TW` 等中文地区别名不表示繁简输出转换。语言属于模型提示，不能当作强制语言锁定。

已知模型不支持显式所选语言时，服务在调用上游前返回 `invalid_language`。未知或自定义模型仍可直接传入语法合法的语言提示，由上游决定；页面只显示“不指定语言（模型能力未确认）”，不宣称具体覆盖范围。

切换供应商时，应保留仍受支持的语言选择；若不再受支持，提示用户重选并禁用开始录音，直到明确选择自动识别或受支持的语言。录音中不能修改语言。对阿拉伯语等从右到左书写的结果，可给输入框设置 `dir="auto"`。

可选启动兜底仅在语言兼容时生效：显式提示必须被备用模型支持；自动模式要求备用模型覆盖主模型的全部已知语言。因此两个默认模型之间的自动模式不会跨供应商兜底。未知模型不会作为备用目标；`invalid_language` 不重试、不触发兜底。已开始转发音频后仍不会切换供应商。

2026-11-04 已用当前 MaaS 网关跑通 11 种非中英文语言的短合成音频（日、韩、法、德、西、俄、阿、越、泰、印尼、印地语）、日语和法语自动识别，以及一次英语后接日语的录音，均有中间文字和 `done`。该验证确认通路，不代表完整识别质量评估或任意混说方式的保证。语言清单依据：[FunASR 客户端参数](https://help.aliyun.com/zh/model-studio/fun-asr-client-events)、[Qwen 实时客户端参数](https://help.aliyun.com/zh/model-studio/qwen-asr-realtime-client-events)。

## 用录音模块接入输入框

优先复用三个相邻的 ES 模块：`recorder.js`、`pcm-worklet.js`、`pcm-resampler.js`。整体复制时保留相对目录关系；默认 Worklet 地址相对于 `recorder.js` 自身解析。也可用构造参数 `workletUrl` 指向实际部署位置，并保证 Worklet 可找到重采样模块。

### 模块契约

```javascript
import { RealtimeTranscriber } from "/demo/assets/recorder.js";

const recorder = new RealtimeTranscriber({
  url: "ws://localhost:8000/v1/asr", // 实际项目按当前部署地址构造
  provider: "funasr",              // 省略时先读同源 capabilities，用 default_provider
  language: null                   // 默认 null，自动识别；也可用 ja、fr 等
  // workletUrl: new URL("./pcm-worklet.js", import.meta.url)
});

await recorder.start(); // 请求权限、连接并开始会话
await recorder.stop();  // 停止采集，flush 尾包，发送 stop 并等待完成
recorder.cancel();      // 放弃本次会话并释放资源
```

`provider` 可省略或传 `null`。此时模块在初始化阶段读取**页面同源**的 `/v1/asr/capabilities`，要求返回已配置的 `default_provider`，再把它作为显式值写入 `start`；初始化被取消时会中止该请求。显式传入 `provider` 则跳过能力读取。浏览器能力查询与 WebSocket 握手一样需要代理注入 `X-API-Key`，直接连接没有鉴权头的后端端口会得到 401；模块会以 `capabilities_error` 报告读取或解析失败，并在不支持 fetch 时报告 `unsupported_browser`。WAV 脚本省略 `--provider` 时采用相同规则，但通过带服务密钥的受保护 HTTP 请求读取能力。

构造参数 `timeouts` 可覆盖各阶段的毫秒值，例如 `timeouts: { ready: 40000, done: 40000 }`。未传时默认 `init: 30000`、`ready: 20000`、`flush: 3000`、`done: 25000`，只覆盖指定项；值必须是 1 到 2147483647 的有限数字，未知字段拒绝。`init` 覆盖权限、模块加载、provider 能力读取及 WebSocket 打开前的初始化；`ready` 从 WebSocket 打开并发送 start 开始；`flush` 从停止采集请求开始；`done` 从尾包发完并发送 stop 开始。

后端增大 `OPEN_TIMEOUT_SECONDS` 时，需同步增大客户端 `ready`；增大 `FINISH_TIMEOUT_SECONDS` 时，需同步增大客户端 `done`，并为音频队列排空和网络传输留出余量。例如上面的 40 秒适用于后端 30 秒预算并留 10 秒余量，并非服务默认值。客户端不会自动从 capabilities/ready 读取这些预算，后端仍独立执行自己的超时限制。

上面三个调用只是 API 说明，不能在实际页面连续执行。应分别绑定用户的开始、停止、取消操作。`start()` 在进入 recording 后 resolve，失败时 reject；录音期间调用 `stop()` 会在收到 done 后 resolve 为原始 done 对象，失败时 reject。前端也可统一通过 `done` 事件处理成功。

`connecting` 时调用 `stop()` 按取消处理，不返回成功转写结果；尚无会话时 stop 不做任何事。`cancel()` 会令待决的 start/stop 以 `cancelled` 拒绝，并发出 cancelled 状态，通常不发 error 事件。活动会话中重复 `start()` 会以 `invalid_state` 拒绝，不会新建连接，也不保证触发 error 事件。因此方法调用仍需 try/catch，并通过状态禁用重复开始。

`RealtimeTranscriber` 继承 `EventTarget`，通过 `addEventListener` 订阅：

| 事件 | `event.detail` | 页面处理 |
| --- | --- | --- |
| `statechange` | `{state}` | 更新按钮与状态提示 |
| `transcript` | 原始后端 `transcript` 对象 | 用 `full_text` 替换本次语音区域 |
| `done` | 原始后端 `done` 对象 | 使用最终 `full_text`，解锁编辑 |
| `error` | `{code, message, confirmed_text}` | 保留已确认文字，显示错误并允许重新录音 |

`state` 为 `idle / connecting / recording / stopping / done / error / cancelled`。同步在状态回调中取消或重启时，状态通知按发生顺序串行发出。每次录音创建新实例即可，不依赖同一实例能否重用。组件卸载时调用 `cancel()`，并解绑监听器。

### 可直接改造的输入框示例

以下示例适用于与后端同源的页面。它将本次语音追加在原文之后；每次更新仅替换语音部分。录音期间输入框只读，取消恢复完整原文，失败保留最近已确认文字，卸载忽略迟到事件。应用中的“发送”按钮也应在 `connecting / recording / stopping` 期间禁用。

```html
<label for="draft">消息</label>
<textarea id="draft" rows="5">这里可以保留原来的文字。</textarea>
<button id="voice-start" type="button">开始录音</button>
<button id="voice-stop" type="button" disabled>停止并完成</button>
<button id="voice-cancel" type="button" disabled>取消本次录音</button>
<p id="voice-status" role="status" aria-live="polite">尚未开始</p>

<script type="module">
import { RealtimeTranscriber } from "/demo/assets/recorder.js";

function mountVoiceInput() {
  const input = document.querySelector("#draft");
  const start = document.querySelector("#voice-start");
  const stop = document.querySelector("#voice-stop");
  const cancel = document.querySelector("#voice-cancel");
  const status = document.querySelector("#voice-status");
  const ui = new AbortController();
  let active = null;

  const labels = {
    idle: "尚未开始", connecting: "等待麦克风权限或连接就绪…",
    recording: "正在录音", stopping: "正在等待最后一句定稿…"
  };
  const compose = (base, voice) =>
    base + (base && voice ? "\n" : "") + voice;
  const syncButtons = () => {
    start.disabled = active !== null;
    stop.disabled = active?.client.state !== "recording";
    cancel.disabled = active === null;
  };
  const finish = (run, voice, message) => {
    if (active !== run) return;
    active = null; // 先失效，防止迟到事件修改文本
    run.events.abort();
    input.value = compose(run.base, voice);
    input.readOnly = run.wasReadOnly;
    status.textContent = message;
    syncButtons();
  };
  const fail = (run, error) => finish(
    run, error?.confirmed_text ?? run.confirmed,
    `识别中断，已保留已确认文字（${error?.code ?? "client_error"}）`
  );
  const cancelRun = () => {
    const run = active;
    if (!run) return;
    finish(run, "", "已取消，原文已恢复");
    run.client.cancel();
  };

  start.addEventListener("click", async () => {
    if (active) return;
    const url = new URL("/v1/asr", window.location.href);
    url.protocol = location.protocol === "https:" ? "wss:" : "ws:";
    const run = {
      client: new RealtimeTranscriber({
        url: url.href, provider: "funasr", language: null
      }),
      base: input.value, wasReadOnly: input.readOnly,
      confirmed: "", events: new AbortController()
    };
    active = run;
    input.readOnly = true;
    syncButtons();
    const options = {signal: run.events.signal};
    run.client.addEventListener("statechange", ({detail}) => {
      if (active !== run) return;
      status.textContent = labels[detail.state] ?? detail.state;
      syncButtons();
    }, options);
    run.client.addEventListener("transcript", ({detail}) => {
      if (active !== run) return;
      run.confirmed = detail.confirmed_text;
      input.value = compose(run.base, detail.full_text);
    }, options);
    run.client.addEventListener("done", ({detail}) => {
      finish(run, detail.full_text, "识别完成，可以编辑");
    }, options);
    run.client.addEventListener("error", ({detail}) => fail(run, detail), options);
    try { await run.client.start(); }
    catch (error) { fail(run, error); }
  }, {signal: ui.signal});

  stop.addEventListener("click", async () => {
    const run = active;
    if (run?.client.state !== "recording") return;
    try { await run.client.stop(); }
    catch (error) { fail(run, error); }
  }, {signal: ui.signal});
  cancel.addEventListener("click", cancelRun, {signal: ui.signal});
  window.addEventListener("pagehide", cancelRun, {signal: ui.signal});
  syncButtons();

  return () => { cancelRun(); ui.abort(); };
}

const disposeVoiceInput = mountVoiceInput();
// 组件卸载时调用 disposeVoiceInput()。
// React: 在 useEffect 中挂载并 return 清理函数；Vue: 在 onUnmounted 中清理。
</script>
```

该示例使用原生 `textarea.value`。接入 React/Vue 的受控输入时，将文本更新改为对应的 state/ref 更新，保留同样的“原文 + 本次语音快照”结构和会话身份检查。若希望在光标位置插入，录音开始时保存原文、原始选区及两侧文本，固定左右边界后替换中间的语音区域；不要在用户仍编辑时反复覆盖整个输入框。空结果、取消或没有确认文字的失败应恢复完整原文和原始选区，不能删除原先选中的文字。连续录音的插入点保留在上一次语音结束处，旧会话事件不能修改新一轮内容。

## 完整 WebSocket 协议

端点为 `ws://localhost:8000/v1/asr`，线上为对应的 `wss://…/v1/asr`。业务后端直连时先在握手请求头添加 `X-API-Key`；浏览器使用注入该头的业务代理。握手鉴权失败为 HTTP 401，不会进入后续消息协议。控制消息使用 JSON 文本帧；音频使用 WebSocket 二进制消息。`start`、`stop` 及 `audio` 配置不接受未定义字段。

正常顺序：连接 → `start` → `ready` → 音频与 `transcript` 并行 → flush 最后音频 → `stop` → 后续 `transcript` → `done` → 关闭连接。没有语音时，转写事件可能为空。

### 1. 客户端发送 start

连接打开后立即发送，默认必须在 5 秒内收到。

```json
{
  "type": "start",
  "provider": "funasr",
  "language": "zh",
  "audio": {"encoding": "pcm_s16le", "sample_rate": 16000, "channels": 1}
}
```

| 字段 | 类型与约束 | 省略时 |
| --- | --- | --- |
| `type` | 必填，字符串 `start` | 不可省略 |
| `provider` | `qwen`、`funasr`、`null` | 服务选择已配置的供应商；两者都有时优先 `qwen` |
| `language` | `null`、`"auto"` 或语言代码（规范化后 2–16 字符），如 `ja`、`fr`、`fil`；常用地区别名见多语言章节 | `null`，不指定语言提示，默认模型自动识别 |
| `audio` | 固定格式对象 | 使用下列全部默认值 |
| `audio.encoding` | `pcm_s16le` | `pcm_s16le` |
| `audio.sample_rate` | 整数 `16000` | `16000` |
| `audio.channels` | 整数 `1` | `1` |

其他 provider、采样率、声道数、编码和未知字段均会被拒绝。录音中不能再次发送 `start` 或切换 provider；需要新建会话。

### 2. 服务端返回 ready

仅在上游准备成功后发送。推荐前端等收到 `ready` 后再开始采集和上传；本仓库录音模块遵循这个顺序。当前后端也会有界缓存 ready 前收到的 PCM（默认最多 3 秒），并记住提前收到的 stop，待上游就绪后按顺序转发；超过缓存上限报 `pre_ready_overflow`。建连中想放弃应取消并关闭连接。

```json
{
  "type": "ready",
  "session_id": "example-session",
  "provider": "funasr",
  "audio": {"encoding": "pcm_s16le", "sample_rate": 16000, "channels": 1},
  "max_session_seconds": 120,
  "max_audio_seconds": 120,
  "max_audio_chunk_bytes": 16384
}
```

`session_id` 是服务端会话标识；`provider` 为本次实际使用的供应商。若服务端启用了备用供应商，且语言范围兼容、建连时发生切换，还会返回 `fallback_from`（原请求的供应商）；自动模式要求备用模型覆盖主模型全部语言。默认未启用切换，ready 后不会在录音中切换供应商。`audio` 是本次固定音频格式。三个上限是数字：`max_session_seconds` 限制 ready 后到 stop 的实际经过时间，`max_audio_seconds` 限制累计音频时长，`max_audio_chunk_bytes` 限制每条二进制消息字节数。以本次返回为准，在限制前主动停止并预留传输时间。

### 3. 客户端发送二进制 PCM

每条消息是无容器头的 **16 kHz、16-bit signed little-endian、单声道 PCM**，通过 `ws.send(arrayBuffer)` 或 `ws.send(typedArray)` 发送。每包非空、字节数为偶数，并且不超过 `ready.max_audio_chunk_bytes`。

| 音频时长 | 采样点 | 字节数 |
| --- | --- | --- |
| 20 ms | 320 | 640 |
| 50 ms | 800 | 1600 |
| 100 ms | 1600 | 3200 |
| 1 s | 16000 | 32000 |

建议每 20–100 ms 一包。默认上限 16384 字节相当于 512 ms，但不建议攒满上限才发。不要发送 WAV 文件头、WebM/Opus/AAC 容器、JSON 音频字段或 Base64 文本；声明为 16 kHz 不会改变真实音频。

### 4. 服务端返回 transcript

```json
{
  "type": "transcript",
  "session_id": "example-session",
  "sequence": 3,
  "segment_id": "example-segment",
  "segment_index": 0,
  "revision": 2,
  "text": "我想查一下订单。",
  "is_final": true,
  "full_text": "我想查一下订单。",
  "confirmed_text": "我想查一下订单。"
}
```

| 字段 | 含义 |
| --- | --- |
| `type` | 固定为 `transcript` |
| `session_id` | 本次会话标识 |
| `sequence` | 从 1 起、连接内递增的转写事件序号 |
| `segment_id` | 不透明的语音段标识；无需解析其格式 |
| `segment_index` | 从 0 起、当前已知语音顺序的位置；迟到的顺序信息可能改变它 |
| `revision` | 该段文字版本；纯顺序变化也可能发事件，因此不能只按 revision 丢弃事件 |
| `text` | 该段当前完整快照，不是增量字符 |
| `is_final` | 该段是否定稿；为 true 不表示本次录音结束 |
| `full_text` | 所有已知段按顺序拼接的当前完整快照，包含临时文字 |
| `confirmed_text` | 仅已定稿段按顺序拼接的快照 |

用 `full_text` 整体替换本次语音区域，**不要 `+=`**。`confirmed_text` 也要替换，较早语音段迟到时它可能重新排序。服务端按模型原文直接拼接，不自动添加段间空格、标点或换行。

### 5. 客户端发送 stop

```json
{"type": "stop"}
```

唯一字段 `type` 必须是 `stop`。重复 `stop` 幂等，但之后继续发送音频会得到 `audio_after_stop`。发 stop 前必须停止采集并发送完剩余 PCM；发出后继续接收事件，不能立即断开。

### 6. 服务端返回 done

```json
{
  "type": "done",
  "session_id": "example-session",
  "full_text": "我想查一下订单。",
  "audio_seconds": 2.4
}
```

`type` 固定为 `done`；`session_id` 标识会话；`full_text` 是本次最终全文，可能为空；`audio_seconds` 为服务端成功转发的 PCM 字节数除以 32000，单位秒，不是墙钟耗时或计费承诺。

服务收到上游结束事件、确认没有未定稿文字后才发 `done`，随后以代码 `1000` 关闭连接。**仅 `done` 表示成功。** 只收到 close（即使 close code 为 1000）也必须判定为未完整结束。默认停止后的最终结果等待时间为 15 秒；超时会返回错误。

### 7. 服务端返回 error

```json
{
  "type": "error",
  "session_id": "example-session",
  "code": "finish_timeout",
  "message": "Timed out waiting for final transcription",
  "confirmed_text": "已经确认的内容。"
}
```

`type`、`code`、`message`、`confirmed_text` 均为字符串；`type` 固定为 `error`，`code` 用于程序分支，`message` 是诊断文字，`confirmed_text` 是可保留的已确认快照。`session_id` 通常是会话字符串；并发已满或服务重启导致会话未创建时为 `null`。失败后连接关闭，不再等待成功。对用户的中文提示按 `code` 映射，不依赖 `message` 的具体拼写。

握手鉴权失败返回 HTTP 401（响应 `{"detail":"Invalid or missing API key"}`），业务后端应检查请求头；浏览器通常只能观察到握手失败，由代理排查。握手失败、网络中断、浏览器权限失败等场景可能没有服务端 WebSocket JSON 错误。自行实现客户端时，要保存最近的 `confirmed_text`，在未收到 done 的异常断开时提示中断并舍弃临时文字。模块的 `error` 事件统一提供 `code / message / confirmed_text`。

## 音频采集与停止顺序

浏览器音频常见为 44.1/48 kHz。推荐使用 `AudioWorklet` 读取 Float32 采样，按**实际 `AudioContext.sampleRate`** 处理，混为单声道、连续低通滤波重采样至 16 kHz，再转换为 PCM16LE。直接抽点会混叠；每包独立重采样可能在边界断裂；仅请求 `sampleRate: 16000` 不能保证所有设备按这个值工作。

`MediaRecorder` 通常输出 WebM/Opus 等编码容器，不能直接发送给本协议。更改文件后缀或 MIME 类型也不会把它变成 PCM。复用本仓库模块时不需要另写 MediaRecorder 流程。

自行实现采集时应满足：

1. 用户操作触发权限请求和 `AudioContext.resume()`；尚未 ready 时不上传。界面只有在真正开始采集后才显示“正在录音”，避免用户开头的话被丢掉。
2. 限制本地音频队列，监控 `WebSocket.bufferedAmount`。网络拥塞时明确中断；不要无限累积音频，也不要静默丢包后显示成功。
3. 点击停止后进入 `stopping`，阻止新的采集，令 Worklet flush 内部尾包并等待确认。
4. 将尾包通过同一 WebSocket 按顺序发完，再发送 `{"type":"stop"}`，保证 stop 不会越过音频。
5. 保持 WebSocket 接收开启，继续处理 transcript，直到 done 或 error。等待过程中界面应说明“正在等待最后一句定稿”。
6. 完成、失败、取消或组件卸载时释放 `MediaStreamTrack`、`AudioContext`、Worklet、计时器和监听器。取消时忽略迟到事件；若权限对话框稍后才授予流，也应立即释放。

取消直接关闭会话，没有 `{"type":"cancel"}` 这个协议消息。它不保证已经发送的音频停止处理或撤销上游用量；产品应恢复原文，不把当前临时文字当最终结果。

## 页面状态与文本归属

| 状态 | 用户可做的操作 | 文本处理 |
| --- | --- | --- |
| `idle` | 开始 | 保存录音前的输入框原文 |
| `connecting` | 取消 | 不显示正在录音；锁定编辑 |
| `recording` | 停止、取消 | 原文固定，整体替换本次 `full_text` |
| `stopping` | 取消 | 等待尾句；继续替换快照；不允许再次开始 |
| `done` | 编辑、重新录音 | 使用 done 的最终全文，释放资源 |
| `error` | 编辑、重新录音 | 保留原文和 confirmed_text，舍弃未确认内容 |
| `cancelled` | 编辑、重新录音 | 恢复原文，忽略旧连接的所有后续事件 |

正常路径是 `idle → connecting → recording → stopping → done`；活动状态可进入 `error` 或 `cancelled`。`connecting` 阶段点击停止可在产品层统一处理为取消，不要发送 stop。终态后重新录音使用新实例、新 WebSocket。

不要根据“出现标点”“某段 is_final”“一段时间没出字”自行判定整个录音完成。临时快照可能纠错和重新排序。若产品允许用户在录音时编辑，必须单独维护语音插入范围和编辑冲突策略；上面的示例通过只读锁定避免覆盖。

## 错误与恢复

| 服务端错误码 | 典型原因 | 建议处理 |
| --- | --- | --- |
| `provider_unavailable` | 显式指定的 provider 未配置 | 刷新能力接口，选择 configured 的供应商，或省略 provider 让服务选择 |
| `server_busy` | 服务当前并发已满 | 显示“服务繁忙，请稍后重新开始”；释放当前会话，不自动重连重放音频 |
| `shutting_down` | 服务正在停止或重启 | 提示稍后重新开始，不自动重放本次音频 |
| `invalid_start` | start 字段、语言语法或固定音频格式不合法 | 检查完整 JSON，不在当前连接重试 |
| `invalid_language` | 已知模型不支持所选语言，或 FunASR 返回明确的语言拒绝码 | 读取当前供应商语言清单，重选语言后新建会话 |
| `pre_ready_overflow` | ready 前音频超过有界缓存上限 | 等 ready 后开始采集；建连中取消用关闭连接 |
| `invalid_message` | 非 JSON 对象、无效 JSON、非法控制消息或未知字段 | ready 后仅发送二进制音频或 stop |
| `invalid_audio` | 音频为空或字节数为奇数 | 检查 PCM16 分包与尾包 |
| `message_too_large` | 控制消息或音频分片超限 | 减小消息；音频以 ready 声明为准 |
| `audio_after_stop` | stop 后还在发送音频 | 等 flush 完成后再发 stop |
| `start_timeout` | 连接后未及时发送 start | WebSocket open 时立即发送 start |
| `idle_timeout` | ready 后长期没有音频或 stop | 检查采集与网络；正常静音也有 PCM 数据 |
| `audio_limit` | 累计音频超过时长上限 | 在上限前主动停止 |
| `session_limit` | ready 后实际经过时间超限 | 按墙钟时间提前停止，预留传输余量 |
| `slow_client` | 服务端向浏览器发消息超时 | 检查网络和页面接收处理 |
| `upstream_connection_error` | 无法连接供应商 | 提示服务暂不可用，检查后端网络 |
| `upstream_auth_error` | 上游 HTTP 401/403 拒绝连接 | 由后端检查供应商凭据和模型权限 |
| `upstream_rate_limited` | 上游 HTTP 429 限流 | 提示繁忙，稍后重新开始 |
| `upstream_unavailable` | 上游 HTTP 500/502/503/504 | 提示供应商暂不可用 |
| `upstream_rejected` | 上游其他 HTTP 拒绝连接 | 由后端检查模型与接入配置 |
| `upstream_timeout` | 连接或发送上游数据超时 | 保留已确认文本，稍后重新录音 |
| `upstream_closed` | 上游在正常完成前断开 | 提示转写中断 |
| `upstream_error` | 上游报告失败 | 检查服务端配置、模型权限与可用性 |
| `upstream_protocol_error` | 上游事件或文字顺序不合法 | 保留已确认文本，记录 code/session_id 供排查 |
| `incomplete_transcript` | 上游结束但仍有未定稿段 | 舍弃临时文字，不展示成功 |
| `finish_timeout` | stop 后等不到最终结果 | 保留已确认文本，提示未完整结束 |
| `provider_state_error` / `invalid_audio_format` | 供应商适配器状态或音频校验失败 | 对外协议校验通常会更早拦截，交由后端排查 |
| `internal_error` | 服务内部未预期异常 | 保留已确认文本，使用 session_id 排查 |

模块还可能报告本地错误，这些并非服务端协议码：

| 本地错误码 | 建议提示或处理 |
| --- | --- |
| `microphone_denied` | 在浏览器站点设置中允许麦克风后重试 |
| `microphone_not_found` / `microphone_unavailable` | 连接或选择麦克风，检查系统权限和设备占用 |
| `microphone_ended` / `audio_suspended` | 采集被中断，检查设备并重新开始 |
| `insecure_context` / `unsupported_browser` | 使用 localhost 或 HTTPS，以及支持 AudioWorklet 的浏览器 |
| `network_backpressure` / `audio_overflow` | 网络或页面处理跟不上音频速度，已中断，避免无限积压 |
| `connection_error` / `connection_closed` | 服务连接失败或提前断开，保留已确认文字 |
| `capabilities_error` | 省略 provider 时无法读取或解析同源能力响应；检查代理是否注入 Key 以及能力接口是否正常 |
| `initialization_timeout` / `ready_timeout` / `flush_timeout` / `done_timeout` | 对应阶段超时，释放资源并允许重新开始 |
| `audio_error` / `protocol_error` / `initialization_error` | 采集、协议或初始化失败，记录错误并显示通用提示 |

未知错误码也必须有通用提示，不能使页面停在“正在录音”。`cancelled` 与 `invalid_state` 还可能作为方法 Promise 的拒绝原因，由调用方 catch。

默认限制：start 等待 5 秒、单次上游建连 5 秒、整个上游准备阶段 12 秒（含有限重试及可选备用供应商）、ready 前音频缓存 3 秒、音频输入空闲 15 秒、stop 后完成等待 15 秒、单次发送等待 5 秒；录音实际时间与累计音频均为 120 秒；控制 JSON 最多 8192 字节；单包音频最多 16384 字节。部署可调整，时长和分片值优先读取能力接口及 ready。

进程默认最多 100 个连接，达到上限返回 `server_busy`，服务停止接入时返回 `shutting_down`，这两种错误的 `session_id` 为 null，随后以 1013 关闭连接。反向代理、网络故障或旧部署可能直接拒绝握手，仍需处理没有 JSON 错误的断开。CLI 的 WebSocket 传输层限制单消息 32768 字节，超大消息也可能直接关闭连接。

不自动重连重放本次音频：新连接没有旧会话的续传位置，重放可能重复出字和产生额外上游用量。恢复方式是保留已确认文字，由用户重新开始一次录音。

## 联调验收清单

下表是待执行的验收标准。模拟供应商测试能验证协议，但不能代替真实浏览器麦克风、真实设备重采样或真实上游识别测试。

| 场景 | 操作 | 通过条件 |
| --- | --- | --- |
| 能力读取 | 获取 capabilities，选择 configured provider | 页面显示可用项；start 显式包含所选 provider，或由模块读取 default_provider 后显式发送 |
| 正常中文 | 说两句，中间停顿，点击停止 | 出现文字更新；收到 done；最后一句完整；麦克风指示消失 |
| 多语言 | 分别用自动识别和受支持的语言提示测试 | 原语言文字有快照、有 done；界面不把提示冒充检测结果 |
| 切换供应商 | 先选语言，再切换到不支持它的供应商 | 保留选择并提示重选；明确选择受支持语言或自动后才能录音 |
| 快照纠错 | 连续说较长句子，观察多次 transcript | 输入框语音区域被替换，无重复整句、无逐次累计 |
| 原文保护 | 输入已有文字后开始录音 | 原文保留；录音期间不被手动编辑覆盖；完成后解锁 |
| 停止尾包 | 说完最后一个词立即点停止 | stopping 可见；最后音频在 stop 前；最后文字在 done 中 |
| 取消 | 建连中、录音中、停止等待中分别取消 | 原文恢复、设备释放、旧事件不再改写页面 |
| 权限拒绝 | 重置站点权限后拒绝 | 明确提示、可重试、输入框恢复可编辑、没有永久忙碌状态 |
| 无设备/设备占用 | 移除或禁止可用麦克风 | 提示设备问题并释放会话，不伪装成功 |
| 中途断网 | 录音中阻断网络或停止后端 | 无 done 时显示中断，仅保留 confirmed_text；不自动重放 |
| 空录音 | 开始后保持静音，再停止 | 能正常结束；允许最终全文为空；是否空文本由真实模型决定 |
| 达到时限 | 按 ready 的限制接近上限 | 前端提前停止；不会等到服务端超限再声称成功 |
| 多次录音/页面卸载 | 连续录音两次，并在录音中离开页面 | 新会话不继承旧快照；卸载后麦克风与 WebSocket 被释放 |
| 设备采样率 | 分别用可用的 44.1/48 kHz 设备测试 | 服务始终接收 16 kHz PCM，语速与音高正常，无边界杂音 |
| 手机浏览器 | 通过受信任 HTTPS 地址访问 | 权限可授予、WSS 可达、停止/取消行为与桌面一致 |

排障按层进行：先检查 `/healthz` 与能力接口，再看 start/ready，接着看二进制音频是否连续且大小正确，最后核对 stop/done 和最终文字。`configured: true`、健康检查通过或 WebSocket 成功握手，都不能单独证明识别链路已经跑通。
