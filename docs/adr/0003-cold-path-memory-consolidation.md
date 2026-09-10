# 0003. 离线记忆归约与冲突消解架构 (Cold Path Memory Consolidation)

**状态**: accepted (基于 Epic #13「先捕获、后治理」三层流水线架构，规范 Issue #17 落地)
**日期**: 2026-09-10

## 背景与上下文 (Context)

在 ADR-0001 (轻量封装) 与 ADR-0002 (会话蒸馏) 的基础上，Hippo 在 Epic #13 中确立了 Hot Path (Agent 显式直写, #14) 与 Warm Path (会话蒸馏, #15) 的双通道写入架构：
1. **前台追求亚秒级低延迟**：通过 `add_explicit(infer=False)` 绕过二次 LLM 抽取，将显式写入延迟压至极致；
2. **后台进行无感会话级提炼**：通过宿主生命周期 Hook 与异步 Spool 状态机，保持 `infer=True` 发现会话上下文事实；
3. **短期重复与潜在冲突**：双通道异步写入必然导致短期内存在表述略有差异的等价事实（如“项目使用 PostgreSQL”与“项目主数据库为 PostgreSQL”）或阶段演进冲突（如“使用 PostgreSQL”与“已迁移至 MySQL”）。

若将复杂的语义去重与冲突消解逻辑强行塞入前台同步链路，会再次引发数秒至数十秒的终端阻塞与性能退化。因此，必须确立独立的 **Cold Path Memory Consolidation** 离线治理架构。

## 决策 (Decision)

我们决定将长期记忆的等价合并、冲突消解与时效治理收敛至离线/异步 Cold Path，建立确定性的归约架构：

1. **确立“先捕获、后治理”（Capture First, Consolidate Later）核心架构**：
   - 前台负责不丢且够快，后台负责发现和提纯，Cold Path 负责长期最终一致；
   - Consolidation 逻辑绝不在前台 `add_memory` 或 `search_memories` 链路中同步执行，完全与前台交互解耦。

2. **定义独立的 Consolidation Seam**：
   - 暴露公共接口 `MemoryConsolidator.consolidate(scope, project_id=None, since=None, dry_run=False) -> ConsolidationResult`；
   - 支持通过 CLI 命令 (`hippo consolidate`)、定时守护进程或离线 Job 调用；
   - 基于文件互斥锁（`~/.hippo/consolidation.lock`）实现单项目安全互斥，防止并发竞态。

3. **软删除治理与历史血统完整保留 (Soft-delete & Full Provenance)**：
   - 严禁物理硬删除被合并或被淘汰的记忆；
   - 采用元数据状态标记：被替代项标记 `status="superseded"`，并记录 `superseded_by="<winner_id>"`、`superseded_at` 与 `supersede_reason`；
   - 等价合并时，Winner 记忆原地升级，记录 `merged_ids`、`merged_sources`，并累加 `confirmation_count`；
   - 前台检索门禁层（`hippo_memory/gate.py`）对 `status="superseded"` 进行主动过滤，保证 Agent 检索到的永远是纯净、规范的活跃记忆。

4. **确定性胜者仲裁矩阵 (Deterministic Arbitration)**：
   - **时效优先 (Recency Rule)**：依据 #14 / #15 统一注入的 `last_confirmed_at`，最新被可靠确认的事实优先；
   - **权威优先 (Authority Rule)**：当时间接近或相同时，显式写入 `agent_explicit` > 会话自动推断 `session_distillation`；
   - **创建优先 (Stability Rule)**：若权威与时间完全相同，保留最早创建的 `created_at`。

5. **幂等性与重试弹性**：
   - 扫描过程仅将未失效的 Active 记忆 (`status != "superseded"`) 作为候选集；
   - 已经完成归约的记忆不会重复触发合并或冲突操作，重复运行结果收敛为 `scanned=N, unchanged=N, merged=0, superseded=0`，保证任务重试与批处理幂等。

## 收益与影响 (Consequences)

- **前台性能零损耗**：前台显式写入与检索无需承担去重与冲突判定开销，维持亚秒级极致性能；
- **知识状态最终一致收敛**：离线定期归约逐步消除陈旧与冗余事实，使长期知识库收敛为高价值、无矛盾的 Canonical 事实集；
- **全链路可审计与溯源**：保留完整的软删除血统链与变更详情（`ConsolidationResult`），消除“黑盒静默删除”带来的调试困境；
- **对齐 Mem0 原生架构**：基于现有 Qdrant / Mem0 元数据原地更新，不要求迁移底层主存储引擎。
