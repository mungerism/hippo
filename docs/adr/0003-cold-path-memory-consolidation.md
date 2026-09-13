# 0003. 离线记忆归约与冲突消解架构 (Cold Path Memory Consolidation)

**状态**: accepted,已实现落地 (#30 / #32 / #33 / #34 / #36)
**日期**: 2026-09-10（设计）/ 2026-09-13（随实现修订）

## 背景与上下文 (Context)

在 ADR-0001 (轻量封装) 与 ADR-0002 (会话蒸馏) 的基础上，Hippo 在 Epic #13 中确立了 Hot Path (Agent 显式直写, #14) 与 Warm Path (会话蒸馏, #15) 的双通道写入架构：
1. **前台追求亚秒级低延迟**：通过 `add_explicit(infer=False)` 绕过二次 LLM 抽取，将显式写入延迟压至极致；
2. **后台进行无感会话级提炼**：通过宿主生命周期 Hook 与异步 Spool 状态机，保持 `infer=True` 发现会话上下文事实；
3. **短期重复与潜在冲突**：双通道异步写入必然导致短期内存在表述略有差异的等价事实（如“项目使用 PostgreSQL”与“项目主数据库为 PostgreSQL”）或阶段演进冲突（如“使用 PostgreSQL”与“已迁移至 MySQL”）。

若将复杂的语义去重与冲突消解逻辑强行塞进前台同步链路，会再次引发数秒至数十秒的终端阻塞与性能退化。因此，必须确立独立的 **Cold Path Memory Consolidation** 离线治理架构。

## 决策 (Decision)

我们决定将长期记忆的等价合并、冲突消解与时效治理收敛至离线/异步 Cold Path，建立确定性的归约架构：

1. **确立“先捕获、后治理”（Capture First, Consolidate Later）核心架构**：
   - 前台负责不丢且够快，后台负责发现和提纯，Cold Path 负责长期最终一致；
   - Consolidation 逻辑绝不在前台 `add_memory` 或 `search_memories` 链路中同步执行，完全与前台交互解耦。

2. **定义独立的 Consolidation Seam**：
   - 暴露公共接口 `MemoryConsolidator.consolidate(scope="project", project_id=None, since=None, dry_run=False, *, user_id=None) -> ConsolidationResult`；
   - 支持通过 CLI 命令 (`hippo consolidate`)、定时守护进程或离线 Job 调用；
   - 并发互斥采用 **per-identity 共享写锁**（`~/.hippo/consolidation/locks/`，文件名为 identity 的 SHA-256 摘要）：进程内可重入（RLock），跨进程 flock；
   - 关键约束：**`HippoEngine.add / update / delete` 等所有可解析为单一 identity 的写入方都参与同一把锁**——Cold Path 的 re-read + 版本比对 + mutation 只在共享锁内才构成真正的临界区，仅 consolidator 之间互斥无法阻止 Hot/Warm 并发写入（TOCTOU）。显式例外：未提供 agent_id / scope 的 `delete_all(scope=all)` 是 admin 级批量操作，按 user 执行、不取 per-identity 锁。

3. **软删除治理与历史血统完整保留 (Soft-delete & Full Provenance)**：
   - 严禁物理硬删除被合并或被淘汰的记忆；
   - 采用元数据状态标记：被替代项标记 `status="superseded"`，并记录 `superseded_by="<winner_id>"`、`superseded_at` 与 `supersede_reason`（`equivalent_merged | conflict_overridden`）；
   - 等价合并时，Winner 记忆原地升级（**V1 不改写 winner 文本**），`merged_ids` / `merged_sources` 按 set-union 合并，`confirmation_count` 取 unique lineage 的确定性聚合（禁止不可重放的 `+=`），`last_confirmed_at` 取 lineage 最大值；
   - Agent-facing recall 的 superseded 过滤是**独立于 relevance gate 的 lifecycle invariant**（`hippo_memory/lifecycle.py`），在 gate 之前统一生效；`get(memory_id)` 与 history 保持 audit 语义，可读取完整血统。

4. **确定性胜者仲裁矩阵 (Deterministic Arbitration)**，规则顺序固定、显式可配置：
   - **时效优先 (Recency)**：`last_confirmed_at` 严格新于 `recency_margin_seconds`（默认 60s，可配置）者为胜；freshness 缺失（无 `last_confirmed_at`）则跳过本规则——`updated_at` 不是确认信号；
   - **权威优先 (Authority)**：时间接近时 `agent_explicit` > `session_distillation`；
   - **确认证据 (Confirmation)**：`confirmation_count` 高者胜（lineage 聚合值）；
   - **稳定性兜底 (Stability)**：更早 `created_at` 优先，最终以 memory ID 字典序保证全序。
   - 关系判定（EQUIVALENT / CONFLICT / DISTINCT）与 winner 仲裁严格两阶段：时间、来源、确认数不参与关系判定。

5. **幂等性与重试弹性**：
   - 每个 destructive 决策先固化稳定 Operation Plan（确定性 `operation_id`），持久化于 operation journal（`~/.hippo/consolidation/operations/<operation_id>.json`），状态机 `planned -> applying -> completed`（另有 `failed` / `stale`）；
   - apply 前对 winner / loser 双侧重验 observed 版本指纹（`updated_at/hash/status`），Hot/Warm 写入使 plan 失效时拒绝执行（`stale_plan`），由重规划收敛；
   - crash 后由 recovery pass 扫描 unfinished journal 补完，配合 lineage 去重保证“重试后最终状态与单次成功执行等价”；重复运行最终 mutation 为零。

6. **Cold Path 分类器隔离**：
   - `RelationshipClassifier` 使用独立 LLM 实例：deep-copy provider 配置并固定 `temperature=0`，绝不复用或修改 Hot/Warm 共享的 `engine.memory.llm`；
   - LLM 不可用时降级为确定性精确匹配模式（其余 fail-closed 为 DISTINCT），并在结果中显式告警。

## 收益与影响 (Consequences)

- **前台性能零损耗**：前台显式写入仅增加一次无竞争的文件锁获取（微秒级），无需承担去重与冲突判定开销；
- **知识状态最终一致收敛**：离线归约逐步消除陈旧与冗余事实，使长期知识库收敛为高价值、无矛盾的 Canonical 事实集；
- **全链路可审计与溯源**：operation journal + mem0 history + 软删除血统链构成完整审计故事，`ConsolidationResult` 按阶段（discovery / decision / planning / apply / recovery / classifier）报告失败，消除“黑盒静默删除”；
- **对齐 Mem0 原生架构**：基于现有 Qdrant / Mem0 元数据原地更新，不要求迁移底层主存储引擎。
