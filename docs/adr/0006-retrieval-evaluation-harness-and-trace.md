# [ADR-0006] 记忆召回评测框架与分阶段 Evaluation Trace

- **状态 (Status)**: 已通过 (Accepted)
- **决策者 (Deciders)**: munger, Antigravity Agent
- **日期 (Date)**: 2026-09-22

---

## 1. 背景与问题陈述 (Context and Problem Statement)

Hippo 作为面向 AI 智能体的长期记忆引擎，召回质量直接决定智能体上下文的准确度。过去的测试主要依赖端到端集成测试和黑盒语义断言，面临以下瓶颈：

1. **召回评估与具体 Benchmark 强绑定**：缺乏统一的协议抽象，无法将 LongMemEval、LoCoMo 或自有黄金集解耦复用；
2. **缺乏多阶段内部可观测性 (Evaluation Trace)**：搜索链路跨越候选召回（Candidate ANN/BM25）、生命周期与 Scope 过滤（Lifecycle/Scope）、相关性安全门禁（Relevance Gate）、最终截断（Final Top-N）。当召回失真或返回空时，无法区分是底层向量未排入、被 superseded 排除、还是被门禁误杀；
3. **缺少自动化防污染与安全回归门禁**：单纯追求高 Recall 会掩盖过时记忆（superseded）、跨用户/跨项目记忆泄漏的风险；
4. **测试与生产隔离风险**：评测运行如果触碰用户真实向量库，会造成严重的数据污染或隐私泄露。

需要建立一套轻量、解耦、物理隔离且具备四阶段 Trace 的评测基础设施。

---

## 2. 备选方案考量 (Considered Options)

- **方案 A：基于外部大模型 Judge 的端到端黑盒生成评测**
  - *优点*：直观评估最终 QA 答案质量。
  - *缺点*：成本极高、速度慢、无法在 PR CI 运行，且无法定位是检索层漏召回还是生成模型幻觉。
- **方案 B：直接引入重型通用评测库（如完整 MTEB/Ragas 依赖）**
  - *优点*：现成工具链。
  - *缺点*：引入巨大重型依赖与本地 PyTorch/Transformers 环境，违背 Hippo“极简依赖、基于 uv 轻量运行”的核心原则；且不支持 Hippo 的 scope、superseded 和门禁语义。
- **方案 C：独立 `benchmarks/` 架构、统一 Schema、分阶段 Trace 与 Replay/Engine 双适配器（入选）**
  - *优点*：
    - 与生产模块物理与接口完全解耦，不扩大生产 MCP/CLI 接口面；
    - 统一 Schema（Corpus, Query, Qrels, Forbidden, Trace, Manifest）；
    - 内部 `search_with_trace` 明确拆分候选、活跃性/作用域、门禁与最终阶段；生产 `search()` 默认零额外开销；
    - 严格强制评测临时集合（`eval_*`）隔离，禁止触碰生产库；
    - 纯内存 Replay 适配器允许在无网络与无 Qdrant 守护下秒级完成确定性 smoke 验证；
    - 支持逐题 JSON、Markdown 报告生成与 Baseline regression diff。
  - *缺点*：需要维护独立的评测数据定义与适配器。

---

## 3. 决策结果 (Decision Outcome)

所选方案：**方案 C**。

### 核心论据 (Positive Consequences)
1. **防污染优先原则落地**：确立以 `@3` 为主报告口径（对齐 Hippo 生产 `max_injected=3`），并将 `Forbidden Leakage == 0` 作为硬安全红线；
2. **全链路精准诊断**：四阶段 Trace（Candidate、Lifecycle/Scope、Gate、Final）可精确定位召回损失的原因与过滤细节；
3. **物理存储绝对隔离**：`HippoEngineAdapter` 校验集合名称，严禁匹配生产集合，前缀强制 `eval_`，并在退出时自动删除清理；
4. **Agent 安全子集保护**：Evaluation Trace 专供内部诊断与评测，严禁通过 MCP 工具暴露给 Agent。

### 负面影响与折衷 (Negative Consequences / Trade-offs)
- 引擎检索链路需要提供 `search_with_trace` seam，开发团队在修改检索阶段时需同步维护 trace 结构；
- 评测报告生成需保持 JSON 与 Markdown 严格同源与确定性输出。

---

## 4. 实施指导与不变式 (Implementation Guidelines & Invariants)

1. **MCP 不扩散不变式**：`hippo_memory/server.py` 的 MCP 工具签名严禁增加评测或 trace 参数，返回值必须保持为 Markdown untrusted context envelope；
2. **生产零开销不变式**：生产调用 `engine.search()` 时，不实例化任何 trace 数据结构，保持毫秒级吞吐；
3. **物理隔离不变式**：所有评测集合必须以 `eval_` 开头，禁止在未指定或生产集合上运行 runner；
4. **回归判定阈值**：
   - `forbidden_leakage > 0` 判定为阻断性安全违规；
   - 核心指标（Recall@3, nDCG@3, Precision@3）对比已批准 Baseline 下降超过容差（0.001）判定为指标退化（Regression）。
