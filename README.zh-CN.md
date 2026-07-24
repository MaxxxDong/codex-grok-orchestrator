# 简体中文说明（兼容入口）

完整中文指南已合并到同文件双语入口：

**→ [README.md 中文部分](README.md#中文)**

- 英文：[README.md English](README.md#english)
- 站点：[https://maxxxdong.github.io/codex-grok-orchestrator/](https://maxxxdong.github.io/codex-grok-orchestrator/)
- 发布说明：[docs/releases/release-notes.md](docs/releases/release-notes.md)

Python 包与 CLI 名称仍为 **`grok-worker`**。公共仓库身份为 **`codex-grok-orchestrator`**。

Worker 的标准外部证据由 `changes.patch`、`worker.log` 和 `verification.txt` 三个文件组成。

Windows 原生 0.8.0 删除模型轮次上限，预算型截断会在同一会话内有界续跑；
`watch --until-settled` 一次等待即可覆盖终态与清理。runner 自己执行 final gates，
每个 run 使用持久回执，多根目录健康检查、Windows 脏快照、嵌套 npm 依赖和门禁预检均已加固。
它继续保留 Grok Build 原生 Headless、JSON Schema 结果、High、插件、MCP 和严格三文件合同，
不引入 WSL、第二套运行时或第二份 provider 配置。
