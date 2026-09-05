# Hippo (海马体) 🦛

> 面向 AI 编码智能体（Anti-Gravity, Codex, Z-code 等）的统一长短期记忆中枢，基于 Mem0 与 MCP (Model Context Protocol) 打造。

## 核心特性
- **跨 IDE/Agent 统一记忆**：通过标准 MCP 协议，无缝接入 Anti-Gravity、Codex/Cursor、Zed AI (Z-code) 等工具。
- **多层作用域隔离**：支持全局个人偏好（`global`）与当前 Git 工程专属经验（`project`）。
- **零外部服务依赖**：基于嵌入式 Qdrant 本地文件存储，无需 Docker 或独立数据库守护进程。
- **多模态与时序感知**：支持多模态视觉事实抽取与 Append-Only 时序事实累积。
- **命令行工具**：提供终端快速指令（`hippo add`, `hippo search`, `hippo list`）。

## 文档参考
- [Mem0 深度技术调研报告](./mem0_research.md)
