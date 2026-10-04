# [ADR-0008] 从 Agent MCP search_memories 接口中剥离 limit 参数 (Strip limit parameter from Agent-facing search_memories MCP tool)

- **状态 (Status)**: 已通过 (Accepted)
- **决策者 (Deciders)**: munger, munger-agent
- **日期 (Date)**: 2026-10-04
- **修订关系 (Amends)**: 修订 [ADR-0001](/adr/0001-thin-wrapper-over-mem0) 第 1 条（将面向 Agent 的“参数签名 100% 对齐”收敛为“核心工具命名对齐，参数集采用安全子集，由服务端管控注入预算”）

---

## 1. 背景与问题陈述 (Context and Problem Statement)

Hippo 的核心定位为 Mem0 的轻量工程封装。[ADR-0001](/adr/0001-thin-wrapper-over-mem0) 早期确立了“工具名与参数对齐 Mem0 官方规范”，但随着多 Agent 实践深入，系统确立了更高优先级的**Agent 安全子集**原则（`AGENTS.md`）：
> “面向 Agent 的 MCP 仅暴露经过安全审计的极简工具；`search_memories` 由部署端把控安全门禁与注入预算，绝不对 Agent 暴露底层微调参数。”

然而在早期的 MCP 实现中，由于延续了 Mem0 Python SDK 的函数签名惯性，`search_memories` 工具仍暴露了可选参数 `limit: int = 5`。在真实多 Agent 运行环境（如 OpenAI Codex CLI/Desktop）中，暴露出以下严重问题：

1. **注入预算被 Agent 任意推断击穿**：
   Agent 模型缺乏对底层记忆库容量和 Relevance Gate 门禁过滤率的全局认知，常在生成工具调用代码时“自作聪明”地显式传入保守的参数（例如 `limit: 3` 或 `limit: 1`），导致部署端配置的注入预算（`HIPPO_MAX_INJECTED=5`）被 Agent 人为截断，造成高相关有效记忆漏召回（False Negatives）。
2. **上下文惯性引发的脆弱性恶性循环**：
   在长会话（Rollout）中，只要 Agent 在早期的某次尝试中生成了 `{limit: 3}`，In-Context Learning 会导致后续所有轮次持续复制该参数，彻底固化错误行为，使得用户困惑为何系统配置升级后依然只召回 3 条。
3. **参数语义不对称与认知冗余**：
   服务端的硬安全门禁早已实现 `effective_limit = min(limit, max_injected)`。这意味着 Agent 传大值无效（被 clamp），传小值有害（误伤召回）。对 Agent 而言，该参数完全没有正面收益，仅徒增认知负荷与 Token 消耗。

因此，亟需重新厘清“开发者 SDK/CLI 调试接口”与“面向 Agent 的 MCP 意图工具”的边界。

---

## 2. 备选方案考量 (Considered Options)

- **方案 A（保持现状，仅在 prompt 中提醒 Agent 传 5）**：
  - *缺点*：无法阻止 LLM 在不同会话、不同模型或不同环境下的随机生成习惯；长上下文续写依然脆弱。
- **方案 B（从 MCP Schema 中彻底剥离 `limit`，由部署端 `HIPPO_MAX_INJECTED` 单一权威源管控）**：
  - *优点*：彻底杜绝 Agent 主动填入 `limit` 的可能性；完全对齐“部署端把控注入预算”的核心架构准则；接口表面积极致收敛；服务端向下兼容忽略残留的额外字段。
  - *缺点*：Agent 无法主动请求单条极简记忆（但该需求本就应由 Agent 接收上下文后自行挑选或由 Relevance Gate 评分截断，而非在检索阶段削减候选）。

---

## 3. 决策结果 (Decision Outcome)

所选方案：**方案 B**。

### 核心论据 (Positive Consequences)
1. **单一权威源（Single Source of Truth）**：
   记忆注入预算作为系统级上下文配额策略，全权由部署端环境变量 `HIPPO_MAX_INJECTED`（默认 5）统一决裁，消除了 Agent 运行时与服务端配置的冲突。
2. **零认知负荷与极简契约**：
   Agent 检索意图回归本质——“提供 query，获取最相关的记忆”。Tool Schema 彻底移除 `limit`，杜绝任何生成偏置。
3. **分层清晰**：
   开发者与调试链路（Python `HippoEngine.search(limit=...)` 和 CLI `hippo search --limit 10`）仍保留精细控制；仅面向 Agent 的 MCP 协议层实施安全子集收窄。
4. **无缝平滑兼容**：
   MCPServer 原生支持忽略未声明的额外传入参数（extra arguments ignored），旧会话或历史缓存中偶尔附带的 `limit` 不会引发报错，但不再生效截断。

### 负面影响与折衷 (Negative Consequences / Trade-offs)
- 需要更新相关契约测试与集成扩展（如 Pi 扩展）的 Tool Schema 定义；
- 本地 MCP Schema 缓存（如 Antigravity）需同步刷新。

---

## 4. 实施指导与不变式 (Implementation Guidelines & Invariants)

1. **MCP Schema 洁净不变式**：
   `mcp_server.list_tools()` 返回的 `search_memories` inputSchema properties 中绝对不得出现 `limit` 参数；
2. **服务端预算决裁不变式**：
   `search_memories` 服务端内部检索与渲染必须严格以 `engine.config.max_injected`（默认 5）作为上限执行截断；
3. **引擎与 CLI 独立性**：
   `HippoEngine.search()` 与 Typer CLI `hippo search` 保持对 `limit` 参数的完备支持。
