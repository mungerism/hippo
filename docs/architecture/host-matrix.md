# 宿主适配与契约矩阵 (Host Contract Matrix)

Hippo 作为统一的跨 IDE 记忆中枢，需要无缝接入各种不同的 AI 编码环境。为了保证不同宿主环境的行为一致性与隔离性，Hippo 建立了统一的宿主契约矩阵。

---

## 📋 宿主适配矩阵

| 宿主 (Host) | 交互形态 | 捕获管道 (Ingestion) | 检索机制 (Recall) | 专属配置 / 约定 |
| :--- | :--- | :--- | :--- | :--- |
| **Codex** | CLI 终端工具 | `hippo hook capture --host codex` (Session End Hook) | 每次会话启动前通过 MCP `search_memories` 自动注入 | 自动识别当前 Git Repo 作为 `project_id` |
| **Pi** | 极简终端 Agent | 针对 `session_end` / `turn_end` 的专用适配器 | 通过 Tool Call 按需检索 | 内存隔离与会话标识注入 |
| **ZCode** | 终端编码套件 | 监听本地 `.zcode` 记忆变动或通过 CLI 迁移 | 统一注入 Prompt 上下文 | 支持通过 `hippo migrate-zcode` 迁移精细记忆 |
| **Antigravity** | Agentic IDE 助手 | Antigravity MCP Server | 启动任务前必须检索 `scope: 'all'` 记忆 | 遵循 `AGENTS.md` 约定的记忆安全与执行隔离规则 |

---

## 自动项目上下文（实现候选）

[RFC-0003](/rfcs/0003-automatic-project-context) 将 Codex、Claude Code、Antigravity、Pi 列为同一发布的兼容与验收范围。下表接线已实现，默认保持 legacy 命名空间，四宿主真实聊天验收仍待完成。部署以[自动项目路由运维](/runbooks/project-context)和 [ADR-0009](/adr/0009-request-scoped-mcp-project-routing) 为准。

| 宿主 | 项目来源 | 需验证的条件 |
| --- | --- | --- |
| Codex | 请求级 sandbox cwd / roots | capability 协商、路径与多环境语义 |
| Claude Code | stdio 的 `CLAUDE_PROJECT_DIR` / roots | 原始环境捕获与连接生命周期 |
| Antigravity | roots / 工作区专属部署绑定 | 实际 roots 能力、多窗口与多目录消歧；Hook 不取首个目录 |
| Pi | 工具与事件回调的 `ctx.cwd` | 逐调用传目录，安全桥接共用记忆逻辑，不依赖进程 cwd |

四宿主共用项目映射、scope 语义与检索门禁；缺少可靠上下文时保留显式项目选择。Antigravity 的多文件夹 Project 不自动合并成一个 Hippo 项目命名空间。

候选项目既可来自请求活动目录，也可来自专属启动绑定；有候选时允许多个 roots，只做成员校验。后台需在合并作业前固定身份并验证捕获段范围，新作业使用独立版本队列；升级先切消费者，回滚禁止旧 Worker 重新解释新格式。详细契约与验收见 RFC 第 4.2、6.1、6.2 节。

---

## 🪝 极速入队契约 (`hippo hook capture`)

为了保证用户在任何宿主中工作时完全无卡顿，宿主 Hook 脚本必须满足以下非功能性指标：

1. **退出耗时硬限制**：从标准输入读取会话 payload，序列化并写入本地 Spool 存储，全过程必须在 **50ms** 以内完成并退出；
2. **Fail-Safe 保障**：若本地存储异常或发生不可预期错误，必须静默吞掉异常并正常退出（exit 0），绝对不得阻断宿主本身的正常交互或终端退出；
3. **环境自愈与保活**：必要时由后台守护进程按需启动 Qdrant，不依赖宿主环境手工管理数据库进程。
