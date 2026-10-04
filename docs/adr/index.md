# 架构决策记录 (ADR) 索引

架构决策记录（Architecture Decision Record，ADR）用于捕获 Hippo 项目中具有长期影响的重要技术与架构决策。

---

## 📋 ADR 决策清单

| 编号 | 决策标题 | 状态 | 决策日期 | 核心摘要 |
| :--- | :--- | :--- | :--- | :--- |
| [0001](/adr/0001-thin-wrapper-over-mem0) | 轻量封装 Mem0 与最小依赖 | 已通过 (Accepted) | 2026-09-06 | 确立 Hippo 作为 Mem0 工程封装的定位，拒绝重复造轮子与重型依赖。 |
| [0002](/adr/0002-session-memory-distillation) | 会话记忆异步蒸馏与双事件 | 已通过 (Accepted) | 2026-09-08 | 采用 Spool 队列与异步 Worker 实现对话蒸馏，保障前台交互零延迟。 |
| [0003](/adr/0003-cold-path-memory-consolidation) | Cold Path 记忆合并与生命周期 | 已通过 (Accepted) | 2026-09-13 | 引入离线候选发现、冲突消解分类器与行版本号幂等 Apply 机制。 |
| [0004](/adr/0004-embedding-profile-isolation-and-migration) | 向量 Profile 隔离与重建迁移 | 已通过 (Accepted) | 2026-09-19 | 按 (provider, model, dims) 严格隔离集合，伴生实体同构迁移与只读断点续传。 |
| [0005](/adr/0005-spool-governance-and-worker-service) | Spool 队列治理与 Worker 常驻保活 | 已通过 (Accepted) | 2026-09-22 | 建立 Worker LaunchAgent 双常驻架构、内核锁极速探测、Tombstone 幂等墓碑与原子修剪。 |
| [0006](/adr/0006-retrieval-evaluation-harness-and-trace) | 评测框架与分阶段 Evaluation Trace | 已通过 (Accepted) | 2026-09-22 | 确立解耦评测 Schema、确定性指标公式、四阶段 Trace 诊断、物理隔离与 Baseline 回归门禁。 |
| [0007](/adr/0007-scoped-jev-relation-classification-takeover) | Jev 关系分类受控范围接管 | 已通过 (Accepted) | 2026-10-03 | 建立白名单项目、校准门禁产物与预算约束下的 Jev 分类接管机制，具备 Fail-Closed 弃权与误合并定向回滚能力。 |
| [0008](/adr/0008-hide-limit-from-agent-mcp) | 从 Agent MCP 剥离 limit 参数 | 已通过 (Accepted) | 2026-10-04 | 面向 Agent 的 search_memories 彻底隐藏 limit，由部署端 max_injected 单一权威管控注入预算。 |

---

## 📝 编写与评审规范

- 决策草案遵循 [ADR 模板](/adr/template)；
- 新的重要技术选型或架构调整必须编写 ADR；
- 同一 PR 必须同时包含代码实现与对应的 ADR 沉淀。
