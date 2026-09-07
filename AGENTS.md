# Hippo Agent Guidelines

## 🌟 核心架构原则 (Core Tenet)

> **Hippo 只是对 Mem0 的轻量工程封装，绝不重新发明轮子。**

在修改、扩展或维护本项目代码时，所有 AI 智能体必须严格遵守以下原则：

1. **100% 对标 Mem0 官方规范**：
   - MCP Server 暴露的工具名称、参数类型、数据结构与交互范式必须与 Mem0 官方标准保持 100% 一致。
   - 专为自主 Agent 极致精简，仅向智能体暴露核心的检索与沉淀工具：`search_memories` 与 `add_memory`。记忆演化与冲突消解完全交由 Mem0 底层自动处理，严禁向 Agent 暴露冗余脆弱的 ID 级增删改工具。
   - 严禁自行发明非标的自定义 API 或引入深重抽象层。

2. **Hippo 的唯一边界与职责**：
   - **多端协议桥接**：为 antigravity、Codex、ZCode、Zed AI、Cursor、pi 等提供零配置的 MCP stdio 桥接与轻量插件。
   - **自动化作用域路由**：根据执行目录自动探测 Git 根仓库，智能映射 `agent_id` 实现项目级记忆隔离，无需调用方手动传递。
   - **本地服务高可靠**：单二进制 Qdrant Server (`127.0.0.1:6333`) 按需自愈拉起；`hippo service install` 可升级为 LaunchAgent 常驻保活（dev.hippo.qdrant），`hippo doctor` 提供一键巡检。
   - **终端交互 CLI**：提供面向人类开发者的 `hippo` 极简命令行工具。

3. **依赖与模型选型**：
   - 依赖管理：全量使用 `uv`，禁止引入冗余的重型依赖。
   - 模型选型：优先兼容免费/高性价比模型（如 Google Gemini 免费层），内置优雅降级机制（429/503 自动容灾）。

<!-- hippo:memory:start -->
## 记忆检索约定 (Hippo)

- 开始处理任务前，先调用 `search_memories` 检索当前项目记忆与个人偏好（scope: 'all'），不要只依赖当前对话。
- 用户表达偏好、做出值得保留的决策、纠正你的行为或明确要求记住某事时，调用 `add_memory` 沉淀；跨项目的个人习惯用 scope='global'。
<!-- hippo:memory:end -->
