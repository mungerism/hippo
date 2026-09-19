# [ADR-0004] 向量 Profile 严格隔离与断点续传迁移工作流

- **状态 (Status)**: 已通过 (Accepted)
- **决策者 (Deciders)**: Hippo Architecture Team
- **日期 (Date)**: 2026-09-19

---

## 1. 背景与问题陈述 (Context)

PR #27 引入了 Vertex AI 支持（`HIPPO_PROVIDER=vertexai`），不同 Provider 与模型（如 Gemini、OpenAI、Vertex AI）生成的向量在语义空间和维度上完全不兼容。

在规划该演进时，团队识别出两个核心安全隐患：
1. 原有命名解析器将 Gemini 与 OpenAI 均映射到未分型的 `hippo_memories` 集合，切换模型依然存在向量空间污染风险；
2. Mem0 在混合检索与实体链接中依赖伴生实体集合 `{collection}_entities`，若只迁移主集合，实体关联信号将丢失。

---

## 2. 决策结果 (Decision)

确立 **Embedding Profile** 为三元组 `(provider, model, dimensions)`，并建立完整的迁移与重建通道：

1. **确定性 Profile 命名**：
   - 废除通用默认集合，每个 Profile 拥有独立的 Qdrant Collection：
     - Gemini: `hippo_memories_gemini_gemini_embedding_2_768`
     - OpenAI: `hippo_memories_openai_text_embedding_3_small_1536`
     - Vertex AI: `hippo_memories_vertexai_gemini_embedding_2_768`
   - 历史集合 `hippo_memories` 严格视为 **Legacy Migration Source**。
2. **伴生实体集合同构迁移**：
   - 迁移主集合时，若检测到 `{source}_entities` 存在，自动同构迁移至 `{target}_entities`。
3. **源集合绝对只读 (Source Immutability)**：
   - 迁移过程中对源集合仅进行只读 `scroll` 扫描，绝不就地修改，天然保留回滚基准。
4. **批次边界断点续传与冲突拦截 (Batch-Bounded Resume & Conflict Gate)**：
   - 按处理批次调用 `retrieve(with_payload=True)` 比对已存 ID，内存开销保持在 $O(\text{batch\_size})$；
   - 目标端已存 ID 仅当 payload 完全一致时才跳过；一旦发生 payload 发散，判定为 `conflicted` 并阻断覆盖。
5. **Schema 事前校验与首批断言**：
   - 写入前校验目标集合向量维度；首批向量生成后严格断言向量长度。

---

## 3. 后果与影响 (Consequences)

### 收益
- 彻底消除了切换模型导致向量空间混乱的隐患；
- 使得数以万计的历史记忆在更换底层 Provider 时能够平滑、断点续传地安全转移；
- 伴生实体集合与主集合保持一致的生命周期。

### 代价
- 切换 Provider 时需要显式执行一次 `hippo reindex` 迁移命令，不再支持跨模型静默“共享”向量库。
