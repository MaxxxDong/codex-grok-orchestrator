# 简体中文说明（兼容入口）

完整中文指南已合并到同文件双语入口：

**→ [README.md 中文部分](README.md#中文)**

- 英文：[README.md English](README.md#english)
- 站点：[https://maxxxdong.github.io/codex-grok-orchestrator/](https://maxxxdong.github.io/codex-grok-orchestrator/)
- 发布说明：[docs/releases/release-notes.md](docs/releases/release-notes.md)

Python 包与 CLI 名称仍为 **`grok-worker`**。公共仓库身份为 **`codex-grok-orchestrator`**。

Worker 的标准外部证据由 `changes.patch`、`worker.log` 和 `verification.txt` 三个文件组成。

0.7.0 默认使用 Grok Build 原生 Headless + JSON Schema 收口结果，并加入精确任务合同、
24 小时有界同任务续跑、任务级工具裁剪、有效进展告警和缓存 A/B 指标。分离运行后应阻塞等待
同一个 `grok-worker watch` 终端会话；若终端工具返回 `session_id`，只需继续该会话一次，
不要丢下内层 watch 后改成五分钟轮询。
