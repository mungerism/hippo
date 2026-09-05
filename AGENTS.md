# Hippo Agent Guidelines

## 🌟 核心架构原则 (Core Tenet)

> **Hippo 只是对 Mem0 的轻量工程封装，绝不重新发明轮子。**

在修改、扩展或维护本项目代码时，所有 AI 智能体必须严格遵守以下原则：

1. **100% 对标 Mem0 官方规范**：
   - MCP Server 暴露的工具名称、参数类型、数据结构与交互范式必须与 Mem0 官方标准 MCP Server 保持 100% 一致。
   - 工具名必须为：`add_memory`、`search_memories`、`get_memories`、`get_memory`、`update_memory`、`delete_memory`、`delete_all_memories`、`list_entities`。
   - 严禁自行发明非标的自定义 API 或引入深重抽象层。

2. **Hippo 的唯一边界与职责**：
   - **多端协议桥接**：为 antigravity、Codex、ZCode、Zed AI、Cursor、pi 等提供零配置的 MCP stdio 桥接与轻量插件。
   - **自动化作用域路由**：根据执行目录自动探测 Git 根仓库，智能映射 `agent_id` 实现项目级记忆隔离，无需调用方手动传递。
   - **本地服务高可靠**：通过单二进制常驻 Qdrant Server (`127.0.0.1:6333`) + LaunchAgent 保活彻底规避并发文件锁。
   - **终端交互 CLI**：提供面向人类开发者的 `hippo` 极简命令行工具。

3. **依赖与模型选型**：
   - 依赖管理：全量使用 `uv`，禁止引入冗余的重型依赖。
   - 模型选型：优先兼容免费/高性价比模型（如 Google Gemini 免费层），内置优雅降级机制（429/503 自动容灾）。
