# 0007. Jev 关系分类受控范围接管 (Scoped Jev Relation Classification Takeover)

**状态**: accepted, 已实现落地 (#65)
**日期**: 2026-10-03

## 背景与上下文 (Context)

在 [ADR-0003 (离线记忆归约与冲突消解)](/adr/0003-cold-path-memory-consolidation) 中，Hippo 确立了 Cold Path 的四层治理管道：候选发现 (Discovery) -> 关系分类 (Classification) -> 胜者仲裁 (Arbitration) -> 幂等执行 (Apply)。在既有实现中，关系分类默认使用独立的隔离 LLM 实例（`temperature=0`）。

针对结构化关系分类的轻量化演进，Hippo 先后推进了系列技术铺垫：
1. **#61 Jev 适配器**：建立了极简、强校验、脱敏失败码的 `JevClient`；
2. **#62 成对关系离线评测**：建立了成对数据集、物理隔离事实簇与无网络回放流水线；
3. **#63 Jev 旁路影子观察**：在不干扰主决策的前提下，引入了项目白名单、调用预算与匿名差异审计；
4. **#64 分类基准与校准门禁**：通过 Calibration Split 搜索独立的概率/Margin 阈值，并在 Test Split 上执行零误治理、语言切片独立达标与真实测量约束的 RFC Go/No-Go 门禁。

本 ADR 解决 Issue #65 提出的核心架构决策：**在允许的项目、匹配的合格校准产物以及可审计的运行预算同时具备时，如何安全受控地让 Jev 关系分类接管既有流程，同时确保任何异常均保留原记忆，绝不破坏既有数据。**

---

## 决策 (Decision)

我们决定为 Cold Path 引入受控的 Jev 关系分类接管机制（`JevTakeoverClassifier` 与 `JevTakeoverPolicy`），并贯彻以下架构原则：

### 1. 默认行为零侵入，按项目白名单显式准入
- **默认保持既有分类器**：未显式配置接管参数时，`MemoryConsolidator` 继续运行既有独立 LLM 分类器；
- **全要素强校验准入**：接管模式必须同时提供 `takeover_backend`（`JevClient`）与 `takeover_policy`（`JevTakeoverPolicy`）。在策略初始化时，严格校验：
  - 白名单项目集 `allowed_projects` 非空；
  - 模型必须固定为 `jev-1.13.0`；
  - Rubric 必须固定为 `memory-relation-v1`，且 Rubric 内容哈希与当前规范严格一致；
  - 校准产物必须状态为 `status == "GO"` 且 `is_qualified_for_takeover == True`；
- **非白名单范围安全隔离**：若运行范围为 `scope != "project"`（如全局范围）或 `project_id` 不在 `allowed_projects` 中，分类器直接判定为 `not_allowed` 并弃权为 `DISTINCT`，绝不调用 Jev，保留全部原始记忆。

### 2. 独立双阈值约束与 Fail-Closed 弃权机制
- **独立概率与 Margin 门槛**：分类器从合格校准产物中加载针对 `EQUIVALENT` 与 `CONFLICT` 的独立 `min_probability` 与 `min_margin`；
- **严格 Fail-Closed**：
  - 若所选关系的概率低于阈值，或第一与第二名概率差小于 Margin，或 Top-1 发生概率并列（Tie）：一律判定为 `DISTINCT` 并记录显式弃权证据；
  - 若 Jev 发生网络异常、超时、5xx 服务故障或协议解析错误：一律判定为 `DISTINCT` 并记录脱敏的失败代码；
  - 若达到单次运行的调用预算（`max_calls`）或时间预算（`max_elapsed_seconds`）：后续候选对一律判定为 `DISTINCT` 并标记运行状态为 `degraded`；
  - **绝不静默回退**：在接管模式下，任何未达标或异常情况均不得隐式回退调用其他远程 LLM 分类器。

### 3. 单 Seam 职责边界划分：Jev 仅负责关系判定
- Jev 的职能被严格限定在**纯语义关系分类**（`EQUIVALENT` / `CONFLICT` / `DISTINCT`）；
- 胜者必须且只能由 Hippo 既有的 `WinnerArbiter` 依据确认计数、确认时间与全序稳定性矩阵确定；
- 胜者事实文本绝不改写，败者软删除标记为 `status="superseded"`；
- 行版本号防并发覆写（Version Revalidation）与共享文件锁机制（Per-identity flock + RLock）保持完全一致。

### 4. 证据链前置冻结与崩溃恢复幂等性
- 完整的分类证据（模型、Rubric 版本与哈希、校准产物 ID、类别概率分布、Margin、选定概率与应用阈值）在构建计划阶段即冻结写入 `OperationPlan.evidence`；
- Apply 前持久化至磁盘日志 `~/.hippo/consolidation/operations/<operation_id>.json`；
- 崩溃恢复（`_recover_unfinished`）直接重放日志中冻结的 `OperationPlan`，**严禁在恢复阶段重新调用 Jev**，确保重放行为具有确定性，免受重放时外部服务可用性波动的干扰。

### 5. 定向误合并回滚与平滑停用演练
- 在 `ConsolidationApplier` 中提供原子回滚 Seam `revert(operation_id)`：
  - 校验操作是否为已完成状态；
  - 在 Per-identity 互斥锁下，将软删除败者恢复为 `status="active"` 并清除其 `superseded_*` 元数据；
  - 从胜者中剔除被合并项的引用与计票贡献；
  - 将操作日志状态更新为 `reverted`，使误合并事实即时重新具备召回可见性；
- 随时可通过移除接管参数安全停用 Jev；停用 Jev 不会影响或回退已成功落地的历史治理结果。

---

## 收益与影响 (Consequences)

### 긍 收益
- **低成本高质量离线治理**：在受控的垂直项目上利用 Jev 极低的推理成本与微秒级响应完成记忆整理，且不损失治理安全性；
- **全链路防泄漏与可审计性**：证据全量固化上链，检索层严格保持 `Forbidden Leakage = 0`；
- **运营高弹性与灾备完备**：具备一键停用、未完成幂等恢复以及单操作定向撤销的工程闭环。

### ⚠️ 权衡与约束
- 不支持跨项目或全局记忆的 Jev 接管（严格限制在项目级白名单内）；
- 依赖前序 #64 产生的符合决策级真实测量的 Go 产物，未通过门禁前无法强行开启。
