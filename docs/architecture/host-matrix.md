# 宿主适配与契约矩阵 (Host Contract Matrix)

Hippo 作为统一的跨 IDE 记忆中枢，需要无缝接入各种不同的 AI 编码环境。为了保证不同宿主环境的行为一致性与隔离性，Hippo 建立了统一的宿主契约矩阵。

---

## 📋 宿主适配矩阵

| 宿主 (Host) | 交互形态 | 捕获管道 (Ingestion) | 检索机制 (Recall) | 专属配置 / 约定 |
| :--- | :--- | :--- | :--- | :--- |
| **Codex** | 桌面 / CLI | `hippo hook capture --host codex` | MCP 按需调用 `search_memories` | 启用 `--codex-context` 后使用请求目录；否则使用 roots 或显式选择 |
| **Claude Code** | CLI 终端工具 | 本方案未新增 Hippo 捕获协议 | MCP 按需检索 | 专属 stdio 启动时捕获 `CLAUDE_PROJECT_DIR`，结合 roots 校验 |
| **Pi** | 极简终端 Agent | 原生扩展的 `agent_settled` / `session_shutdown` 回调 | 通过 Tool Call 按需检索 | 工具与事件逐次读取 `ctx.cwd` |
| **ZCode** | 终端编码套件 | 监听本地 `.zcode` 记忆变动或通过 CLI 迁移 | 统一注入 Prompt 上下文 | 支持通过 `hippo migrate-zcode` 迁移精细记忆 |
| **Antigravity** | Agentic IDE 助手 | Stop Hook | MCP 按需检索，任务前遵循 `AGENTS.md` | 经验证的 roots 或显式项目选择；本机工作区绑定隔离未通过 |

---

## 自动项目上下文（已实现）

[RFC-0003](/rfcs/0003-automatic-project-context) 的四宿主接线已实现；2026-10-15 本机已完成四宿主真实召回验收并开启 canonical / v2。软件默认仍保持 legacy / v1。具体证据、宿主限制与未覆盖范围见[发布记录](/runbooks/project-context-acceptance)，切换步骤见[自动项目路由运维](/runbooks/project-context)和 [ADR-0009](/adr/0009-request-scoped-mcp-project-routing)。

| 宿主 | 项目来源 | 需验证的条件 |
| --- | --- | --- |
| Codex | 请求级 sandbox cwd / roots | capability 协商、路径与多环境语义 |
| Claude Code | stdio 的 `CLAUDE_PROJECT_DIR` / roots | 原始环境捕获与连接生命周期 |
| Antigravity | 经验证的 roots / 显式选择 | 2.23.1 同名连接跨 Project 复用，暂禁固定绑定；Hook 不取首个目录 |
| Pi | 工具与事件回调的 `ctx.cwd` | 逐调用传目录，安全桥接共用记忆逻辑，不依赖进程 cwd |

2026-10-16 [双项目扩展验收](/runbooks/project-context-extended-acceptance)确认 Codex、Claude、Pi 的实际 A/B 调用通过；Antigravity 隔离失败，当前退回显式选择。Hook 全程实测超过 50ms，目标保留为未达标项。

四宿主共用项目映射、scope 语义与检索门禁；缺少可靠上下文时保留显式项目选择。Antigravity 的多文件夹 Project 不自动合并成一个 Hippo 项目命名空间。

候选项目既可来自请求活动目录，也可来自专属启动绑定；有候选时允许多个 roots，只做成员校验。后台需在合并作业前固定身份并验证捕获段范围，新作业使用独立版本队列；升级先切消费者，回滚禁止旧 Worker 重新解释新格式。详细契约与验收见 RFC 第 4.2、6.1、6.2 节。

---

## 🪝 极速入队契约 (`hippo hook capture`)

为了保证用户在任何宿主中工作时完全无卡顿，宿主 Hook 脚本必须满足以下非功能性指标：

1. **退出耗时硬限制**：从标准输入读取会话 payload，序列化并写入本地 Spool 存储，全过程必须在 **50ms** 以内完成并退出；
2. **Fail-Safe 保障**：若本地存储异常或发生不可预期错误，必须静默吞掉异常并正常退出（exit 0），绝对不得阻断宿主本身的正常交互或终端退出；
3. **环境自愈与保活**：必要时由后台守护进程按需启动 Qdrant，不依赖宿主环境手工管理数据库进程。
