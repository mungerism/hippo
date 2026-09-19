# 调研：Codex 什么时候会查询 memory

> 调研日期：2026-09-06。结论基于 OpenAI 官方一手来源（learn.chatgpt.com 上的 Codex 官方文档、openai/codex GitHub 仓库讨论），已核对原文。

## 核心结论

Codex 有**两条互相独立的记忆通路**，调用时机不同：

### 1. MCP memory 工具（如 Hippo）——模型自主决定，无自动检索

- Codex 把 MCP server 的工具列表连同 server instructions 一起暴露给模型，**调用时机完全由模型在对话中自主判断**（agentic tool call）。官方文档没有描述任何定时检索、会话开始自动查询 MCP 工具的机制；唯一的"自动"行为只出现在连接层（如 401/403 后刷新 header helper），不涉及工具调用。
- 可以通过每个 server 的 `enabled_tools` / `disabled_tools` 过滤工具，用 `default_tools_approval_mode`（auto / prompt / writes / approve）控制调用是否需要人工批准，用 `tools.<tool>.approval_mode` 对单个工具覆盖。
- 也就是说：**Hippo 的 `search_memories` 只在 Codex 模型认为"历史记忆可能有助于当前任务"时才会被调用**；会话开始时不会自动检索。想让 Codex 每次会话先查记忆，目前只能靠 AGENTS.md 指令引导（官方也提醒：必须始终生效的规则应放 AGENTS.md，不要只依赖记忆）。
- 另一个反向关联：`memories.disable_on_external_context = true` 时，**用过 MCP 工具的会话会被排除在 Codex 内置记忆的生成之外**（旧配置键 `memories.no_memories_if_mcp_or_web_search` 仍被接受为别名）。

### 2. Codex 内置 Memories 功能——自动捕获 + 自动注入，但默认关闭

来源：[官方 Memories 文档](https://learn.chatgpt.com/docs/customization/memories?surface=app)、[openai/codex discussion #12567](https://github.com/openai/codex/discussions/12567)

- **捕获（写入）**：不是会话一结束就写。Codex 会跳过活跃或过短的会话，等聊天"空闲足够久"后在后台生成记忆，并做密钥脱敏；rate-limit 剩余额度低于阈值时也会跳过。存放在本地 `~/.codex/memories/`（纯本地文件存储，与 MCP 无关）。
- **检索（注入）**：默认自动。`memories.use_memories` 控制是否把已有记忆注入未来会话；`memories.generate_memories` 控制新会话是否作为记忆生成输入。
- **开关**：本地 Codex 的 memories **默认关闭**，需在 Settings > Personalization 或 `config.toml` 中 `[features] memories = true` 开启；也可用 `/memories` 按会话级选择是否使用/贡献记忆。
- 该功能处于演进期（2026 年 2 月起的讨论显示 exec 会话曾不生成记忆等限制），维护者当时也警告过 rate limit 消耗问题。

## 对 Hippo 的启示

- Codex 侧 Hippo 记忆的读取时机不可控、不可保证——完全取决于模型意愿 + 工具描述质量。想提升触发率，应打磨 `search_memories` 的 tool description（让"查项目技术选型/踩坑/偏好"类意图更容易命中）。
- 可以在 Codex 项目的 AGENTS.md 中加入"会话开始先 `search_memories` 检索项目记忆"的指令，作为人工兜底。
- 若 Codex 用户开启了内置 memories 并设置 `disable_on_external_context = true`，用了 Hippo 的会话将不会进入内置记忆——两条通路互补而非冲突。

## 来源

- Codex MCP 官方文档：https://learn.chatgpt.com/docs/extend/mcp?surface=cli
- Codex Memories 官方文档：https://learn.chatgpt.com/docs/customization/memories?surface=app
- Memories 设计讨论（openai/codex #12567）：https://github.com/openai/codex/discussions/12567
