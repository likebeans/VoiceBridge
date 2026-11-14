# 贡献指南

感谢参与声桥（VoiceBridge）。提交前请先确认改动范围、测试方式和敏感信息处理。

## 环境

- Python 3.11+；项目默认使用 `.python-version` 中的 3.13。
- [uv](https://docs.astral.sh/uv/) 用于依赖和命令。
- Node.js >= 22.7，仅在运行浏览器模块测试时需要。
- 不需要真实模型密钥。自动化测试使用模拟供应商或本机 WebSocket 上游。

```bash
uv sync --extra dev --locked
cp -n .env.example .env
```

`.env` 已被 `.gitignore` 忽略。不要提交真实 `SERVICE_API_KEY`、上游 API Key、录音、转写文本或含敏感数据的抓包。

## 本地检查

在项目根目录运行：

```bash
uv run pytest -q
uv run ruff check .
uv run ruff format --check .
```

浏览器辅助模块测试：

```bash
node --test tests/browser/*.test.mjs
```

测试和格式检查都使用假密钥或模拟上游，不应访问真实 ASR 服务。若改动涉及真实上游行为，请在 Pull Request 中说明使用了哪个供应商、是否真实调用以及可复现的最小步骤；没有真实调用证据时不要声称“已验证”。

## 供应商扩展流程

新增供应商时保持客户端协议不变，把差异留在适配器和能力接口中。

1. **扩展枚举与配置**：在客户端消息校验中加入新的 provider 标识；在 `config.py` 增加密钥、地址和模型配置，并校验启动条件。省略 `provider` 时的选择逻辑应集中在一处，能力接口和会话启动共用，避免前端与后端默认值再次分叉。
2. **实现适配器**：在 `src/voice_bridge/providers/` 下实现 `ASRProvider` 的 `open`、`send_audio`、`finish`、`events` 和 `close`。把上游的增量文字、分段关联、顺序信息和结束事件归一为 `Transcript`、`SegmentOrder`、`Finished`；上游错误映射到现有错误类别。
3. **处理格式差异**：统一输入仍是 16 kHz、16 bit、单声道 PCM。若上游要求其他采样率，由适配器做连续重采样，不要改变客户端契约，也不要只替换 URL。增量/提交规则、重连和结束确认同样由适配器负责。
4. **注册与能力**：在 `main.py` 的供应商工厂和 `/v1/asr/capabilities` 中加入新供应商，返回是否已配置、准确的模型 ID、语言清单和 `default_provider` 规则。不要返回密钥。
5. **语言支持**：在 `languages.py` 按**准确模型 ID**维护语言清单和别名；未知模型返回 `language_support_known: false`，不要宣称支持全部语言。若支持启动兜底，补上语言兼容判断。
6. **测试**：覆盖显式选择、省略/`null` 默认选择、显式未配置报错、上游鉴权失败/限流/超时/提前断开、语言拒绝、结束未定稿、取消和资源释放。若涉及浏览器模块，补充 Node 测试；若涉及部署或代理，补充相应的 HTTP/WebSocket 行为测试。
7. **文档**：同步更新英文首页 `README.md`、中文版 `README.zh-CN.md`、`docs/reference.md`、`docs/frontend-integration.md`、`src/voice_bridge/web/guide.html`、`.env.example` 和 `CHANGELOG.md`。说明真实验证范围，不要把协议测试写成真实上游通过。

## 代码约定

- 遵循现有模块边界；优先复用已有适配器、文本处理、指标和连接管理代码。
- 保持类型标注和简洁命名；Ruff 行宽为 100。
- 不引入无限队列、无界重试或静默降级。超时、错误映射和清理路径要与现有路径一致。
- 不把密钥写入日志、指标标签、异常消息、测试快照或文档。
- 错误信息面向调用方保持稳定，内部细节记录在允许列表内的日志字段。

## Pull Request

请包含：

- 改动目的和用户可见行为变化，尤其是默认值变化；
- 运行过的检查命令和结果；
- 若使用真实上游，注明范围与限制；
- 文档和测试是否同步更新。

安全漏洞不要通过公开 Pull Request 或 issue 披露，见 [SECURITY.md](SECURITY.md)。

## 许可证

贡献内容按 [Apache License 2.0](LICENSE) 授权。
