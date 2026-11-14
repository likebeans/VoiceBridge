# 变更记录

本文件记录用户可见的变更。历史验证数字不代表本轮重新运行的结果。

## Unreleased

### 行为变化

- **统一省略 `provider` 的默认选择。** WebSocket `start`、WAV 示例脚本、浏览器录音模块和能力接口现在采用同一规则：省略或传 `null` 时选择已配置的供应商，两者都配置时优先 `qwen`，只配置 FunASR 时选择 `funasr`。此前 WebSocket 和脚本在省略时固定为 `qwen`，只配置 FunASR 的部署会失败。
- 显式指定一个未配置的供应商仍然返回 `provider_unavailable`，不会被静默改选；已有显式调用不受影响。
- 浏览器录音模块不再把 `funasr` 作为内置默认值；省略 `provider` 时先读取同源能力接口，再显式发送 `default_provider`。WAV 脚本省略 `--provider` 时同样读取受保护的能力接口。

### 新增

- 新增仅监听回环地址的本机开发代理 `scripts/demo_proxy.py`。默认从 `http://127.0.0.1:8000` 代理到 `127.0.0.1:8080`，读取 `.env` 中的 `SERVICE_API_KEY` 并注入能力查询与 WebSocket，方便在浏览器打开 `/demo` 试录。该代理不用于生产用户鉴权。
- 新增 GitHub Actions CI：Python 3.11 / 3.13 的锁定依赖安装、pytest、Ruff 检查与格式检查、Node 浏览器模块测试，以及构建 wheel/sdist 后的安装冒烟验证。
- 新增完整 [Apache License 2.0](LICENSE) 许可证文件及项目元数据链接。
- 新增 [详细参考文档](docs/reference.md)、[英文 README](README.en.md)、[贡献指南](CONTRIBUTING.md) 和 [安全策略](SECURITY.md)。
- 新增 Dependabot 每周检查 uv 与 GitHub Actions 依赖，并在 issue 入口引导私密安全报告。

### 修复与文档

- 修复前序段落仅被顺序事件引用时可能遗漏完整性检查的问题：缺少其最终结果将返回 `incomplete_transcript`；明确的空定稿和迟到定稿仍可正常完成。

- 修正默认供应商行为在 README、WebSocket 协议、前端文档、HTML 指南和示例脚本之间不一致的问题。
- 明确 Qwen 官方入口已实现但尚无真实 Qwen 调用验证；补充官方 API Key / 工作空间地址入口和当前推荐域名说明。
- 明确 FunASR 适配器面向指定 MaaS 网关的 DashScope 风格实时任务协议，不是任意自建开源 FunASR 的通用客户端。
- 明确服务不把音频或转写文本写入磁盘，但音频会发往所配置的上游，留存与处理受上游政策约束；应用日志与试录页事件面板的差异也已说明。
- 把原 README 中的协议、错误码、配置、部署、监控和完整测试说明迁移到 `docs/reference.md`，并保留历史验证数据及其日期。
- 记录 2026-11-14 指定 MaaS 网关的真实短音频调用范围；Qwen 因无可用 Key 仍未真实验证。
- 修正本机试录流程：通过仓库自带开发代理即可在浏览器完成录音，生产环境仍要求业务后端或网关做用户鉴权并注入服务密钥。
- 更新中英文 README 的项目说明、导航、CI 状态徽章和本地验证记录，包含浏览器经本机代理的真实 MaaS 流式转写结果。
