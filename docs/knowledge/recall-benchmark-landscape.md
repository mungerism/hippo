# 长期记忆与召回评测 Benchmark 调研

> 调研日期：2026-09-22。本文优先采用论文、官方仓库、官方文档和数据集卡等一手来源。

## 结论先行

**行业里没有一个覆盖长期记忆全链路的公认单一 benchmark。** 目前较成熟的是三组互补方法：

1. **检索层 benchmark**：LMEB、PrecisionMemBench、BEIR、MTEB，回答“相关记忆能否排进 Top-K，并且无关记忆是否被排除”；
2. **长期记忆系统端到端 benchmark**：LongMemEval、LoCoMo、MemBench，回答“系统经过写入、组织、检索和读取后，能否正确回答”；
3. **RAG 输出评测**：Ragas、RAGBench，回答“注入的上下文是否相关、回答是否忠实和完整”。

对 Hippo 最接近“标准答案”的组合是：

- 用 **LMEB** 作为公共的记忆 embedding / 排序基准；
- 用 **PrecisionMemBench** 直接检验召回精度、scope、supersession 与噪声隔离；
- 用 **LongMemEval + LoCoMo** 作为端到端主基准；
- 用 **MemBench** 补容量、读写时延和 reflective memory；
- 用 **Hippo 自有黄金集** 测 scope、生命周期、冲突、门禁、无答案置空和中文工程记忆；
- 用 **Ragas/TRACe 类指标** 做生成端补充诊断，而不是替代有 qrels 的召回指标。

换句话说，行业已经有相对标准化的**指标和若干常用数据集**，但还没有一个像 ImageNet 那样能够代表整个长期记忆系统质量的唯一分数。

## 评测对象必须先分层

| 层级 | 主要问题 | 合适的公共 benchmark | 不应混用的指标 |
| --- | --- | --- | --- |
| Embedding / 候选召回 | query 能否找到相关 memory item | LMEB；BEIR/MTEB 作通用能力补充 | 最终答案准确率不能定位检索问题 |
| 排序与门禁 | 正确项是否靠前；无关、过期、冲突项是否被挡住 | PrecisionMemBench；LMEB/LongMemEval 的 qrels；Hippo 自有负样本 | 单看 Recall 会掩盖污染注入 |
| 长期记忆系统 | 写入、压缩、更新、检索、读取组合后能否答对 | LongMemEval、LoCoMo、MemBench | 通用 IR 榜不能覆盖生命周期和更新 |
| RAG 生成 | 回答是否使用上下文且不幻觉 | Ragas、RAGBench/TRACe | Faithfulness 不是召回率 |
| 真实产品 | 中文、项目 scope、SUPERSEDED、权限、时延和成本 | Hippo 自有 replay/黄金集 | 公共合成数据不能替代域内测试 |

## 一、检索层 benchmark

### 1. LMEB：目前与 Hippo 最贴近的公共召回基准

[LMEB（Long-horizon Memory Embedding Benchmark）论文](https://arxiv.org/abs/2603.12572)专门评估长期记忆检索，而不是一般网页或段落检索。它包含 **22 个数据集、193 个零样本检索任务**，覆盖四类记忆：

- episodic memory：事件级、时间相关的经历；
- dialogue memory：跨轮次/会话的对话记忆；
- semantic memory：被内化或持久保存的事实知识；
- procedural memory：工具、工作流、经验和轨迹。

其统一格式是 `queries + corpus + qrels + optional candidates`，默认主指标为 **nDCG@10**，并报告 Precision、capped Recall、MAP、MRR 等 Top-K 指标。对于 dialogue 任务，可以把候选集限制在对应会话历史，避免不现实的全局串库检索。官方实现见 [KaLM-Embedding/LMEB](https://github.com/KaLM-Embedding/LMEB)，评测框架建立在 MTEB 上。

LMEB 的一个关键结果是：模型在 LMEB 与 MTEB 英文检索子集上的排名相关性接近零，Pearson 为 **-0.115**、Spearman 为 **-0.130**；dialogue memory 与 MTEB 的相关性更低。这是直接证据：**普通 MTEB/BEIR 表现不能替代长期记忆召回评测。**

对 Hippo 的适用性：

- 很适合比较 embedding 模型、query instruction、chunk 粒度和第一阶段检索器；
- 已包含 LoCoMo、LongMemEval、MemBench 等数据的检索化版本，便于统一运行；
- 不能直接覆盖 Hippo 的写入抽取、生命周期状态、project/global scope、防污染门禁和最终回答生成；
- 当前 22 个数据集均为英文，Hippo 仍需自建中文和中英混合数据。

**建议定位：公共候选召回主 benchmark。**

### 1.1 PrecisionMemBench：最贴近 Hippo 防污染门禁的公共测试

[PrecisionMemBench 论文](https://arxiv.org/abs/2605.11325)刻意拿掉 reader/generator，直接对 memory backend 的返回结果做断言。它有 **89 个 case**，基于 35 条 seed belief，覆盖 alias resolution、scope disambiguation、supersession chain exclusion、cross-user isolation、budget/capacity、ranking stability 和多轮 topic drift 后的噪声隔离。官方 artifact 见 [tenurehq/precisionmembench](https://github.com/tenurehq/precisionmembench)。

它不只要求召回目标项，还通过 `mustExclude` / `shouldOnlyInclude` 一类断言把多余记忆视为硬失败；报告 retrieval precision/recall、结构断言 pass、session drift score、读写延迟等。这个设计与 Hippo 的“防污染优先、fail closed、scope 硬隔离、`SUPERSEDED` 不得泄漏”高度一致，也揭示了只报 Recall 的典型盲区：返回全库可以得到高 Recall，却完全不具备可接受的 Precision。

它也不是成熟的唯一标准：规模只有 89 case 和 35 条种子记忆，语料与提出者的 structured belief schema 紧密相关，论文同时评测并推广作者自己的 Tenure 系统。因此应把它当作**很有价值的精度回归套件和测试设计参考**，而不是取代 LMEB、LongMemEval 或 Hippo 自有黄金集。

**建议定位：公共门禁/精度专项；优先移植其 must-include、must-exclude 与 drift 断言。**

### 2. BEIR：通用零样本 IR 标准，但不是记忆 benchmark

[BEIR 论文](https://arxiv.org/abs/2104.08663)选择了 **18 个英文零样本评测数据集、9 类检索任务**，覆盖生物医学、问答、新闻、论证检索、重复问题、实体检索、引文预测和事实核查等领域；MS MARCO 作为域内结果另报。主指标是 **nDCG@10**，官方框架也支持 Recall、Precision、MAP、MRR。代码与数据入口见 [BEIR 官方仓库](https://github.com/beir-cellar/beir)。

它适合回答：一个检索模型能否跨领域泛化，词法、稀疏、稠密、late-interaction 或 reranker 谁更强。

它不适合直接回答：

- 多会话事实能否被正确写入和更新；
- 相对时间、知识更新和冲突事实如何处理；
- 是否会跨用户、跨项目或跨 scope 泄漏；
- 无答案时是否应该返回空；
- 记忆被压缩、去重或标记为 `SUPERSEDED` 后的行为。

**建议定位：embedding/retriever 的通用 OOD 冒烟测试，不作为 Hippo 总分。**

### 3. MTEB：embedding 选型框架，而非长期记忆系统评测

[MTEB 原始论文](https://arxiv.org/abs/2210.07316)包含 **58 个数据集、8 类任务、112 种语言**：bitext mining、classification、clustering、pair classification、reranking、retrieval、STS 和 summarization。retrieval 的主指标是 **nDCG@10**，reranking 的主指标是 MAP。项目仍在持续扩充，官方入口见 [MTEB 仓库](https://github.com/embeddings-benchmark/mteb)和[任务/榜单文档](https://docs.mteb.org/overview/)。

对 Hippo 最有价值的是 retrieval、reranking 和多语种子集，它们可用于：

- 选择中英文 embedding 模型；
- 比较维度、量化和部署成本；
- 避免只在 Hippo 小型域内集上过拟合。

但 MTEB 的多数任务没有“记忆随时间演化”的结构，不评估写入、遗忘、更新、作用域和回答。因此应把 MTEB 看成**组件资格考试**，不是产品验收。

## 二、长期记忆系统端到端 benchmark

### 4. LongMemEval：Hippo 端到端主基准的最佳候选

[LongMemEval 论文](https://arxiv.org/abs/2410.10813)提供 **500 个经人工整理的问题**，覆盖五项核心能力：

1. information extraction；
2. multi-session reasoning；
3. temporal reasoning；
4. knowledge updates；
5. abstention。

它提供两种标准历史规模：LongMemEval-S 约 **115K tokens / 约 40 个会话**，LongMemEval-M 为 **500 个会话 / 约 1.5M tokens**，另有只保留证据会话的 oracle 版本。每个样本带答案会话 ID，因此既能评估检索，也能评估最终 QA。官方实验报告 **Recall@5/10、nDCG@5/10** 和端到端 QA 准确率，代码及清洗数据见 [LongMemEval 官方仓库](https://github.com/xiaowu0162/LongMemEval)。

它比一般长上下文 benchmark 更适合 Hippo，因为评测明确拆成 indexing、retrieval、reading 三阶段，并包含更新时间和拒答。不过仍有局限：

- 历史是为问题构造的合成 user-assistant 对话，与真实工程记忆有域差；
- 最终 QA 分数会同时受 reader LLM 影响，必须锁定 reader、prompt 和 token budget；
- 它没有 Hippo 特有的 project/global scope、生命周期状态和恶意记忆门禁。

**建议定位：端到端首要公共 benchmark；同时单独报告 retrieval 与 QA。**

### 5. LoCoMo：跨会话、时间推理和 adversarial 的重要补充

[LoCoMo 最终 ACL 论文](https://aclanthology.org/2024.acl-long.747/)构造了很长的开放域对话。最终公开版有 **10 段对话**，平均每段 **27.2 个 session、588.2 turns、16,618 tokens**，最多 32 个 session。QA 共 **1,986 题**：single-hop 841、multi-hop 282、temporal 321、open-domain knowledge 96、adversarial 446。QA 使用归一化 token F1；每题还带 evidence dialog ID，可测 RAG 的 evidence Recall@K。它另有事件总结和多模态对话生成任务。

需要注意，早期 arXiv 版本曾报告 **50 段对话、7,512 道 QA**，而当前[官方 LoCoMo 仓库](https://github.com/snap-research/locomo)与最终 ACL 论文采用 `locomo10.json`。比较外部结果时必须记录版本口径，不能混用两组统计。

LoCoMo 对 Hippo 的价值在于：

- adversarial 问题天然适合检验“错误记忆比漏召回更危险”的门禁策略；
- temporal 与 multi-hop 子集可诊断时间索引、混合检索和跨记忆合成；
- evidence turn 可转成 qrels，分别测候选召回和最终回答。

局限是数据以长对话为中心、规模不大，并且开放域人物对话和代码项目记忆差异明显。

**建议定位：与 LongMemEval 并列的第二端到端集，重点看 temporal/adversarial 分桶。**

### 6. MemBench：补足容量、效率和 reflective memory

[MemBench 论文](https://aclanthology.org/2025.findings-acl.989/)把评测拆成：

- factual memory 与 reflective memory；
- participation（Agent 参与对话）与 observation（Agent 旁观并记录）场景；
- accuracy、retrieval Recall@10、capacity、读写效率四类指标。

数据规模包括 PS-RM 3.5K 题、PS-FM 39K 题、OS-RM 2K 题、OS-FM 8.5K 题；评测还插入无关新闻噪声并构造约 100K-token 的压力设置。官方实现见 [MemBench 仓库](https://github.com/import-myself/Membench)。

上述数字是完整合成数据规模；论文正式实验从不同子集中抽取了 **1,039 个 case**。报告复现结果时应区分“可用全量数据”和“论文实际测试子集”。

它很适合测 Hippo 的事实抽取、偏好归纳、写入/读取时延和容量退化曲线。但其内容大量由结构化 profile 和 LLM 合成，最终题型使用多选题；论文中的检索型 baseline 还固定使用特定 embedding，因此不能直接代表 Hippo 的真实工作负载。

**建议定位：容量和效率专项，不替代 LongMemEval/LoCoMo。**

### 7. MemoryBank：重要先驱，但不是成熟的通用标准集

[MemoryBank 论文](https://arxiv.org/abs/2305.10250)主要提出一种带遗忘曲线的长期记忆机制，并用 SiliconFriend 展示。其量化评测基于 15 个虚拟用户、10 天模拟对话以及 **194 个探测问题**（英文 97、中文 97），人工打分指标包括 memory retrieval accuracy、response correctness、contextual coherence 和三个系统输出的相对排名。代码和数据见 [MemoryBank-SiliconFriend 官方仓库](https://github.com/zhongwanjun/MemoryBank-SiliconFriend)。

它的意义更多是建立了早期评测范式，而不是形成了今天的标准 benchmark：数据小、问题围绕该系统构造、人工打分成本高，也没有像 BEIR/LMEB 那样稳定的 qrels 与统一 leaderboard。

**建议定位：可借鉴中英双语、遗忘和人工 correctness/coherence 的设计；不建议作为 Hippo 主回归集。**

### 8. Multi-Session Chat（MSC）：长期对话生成数据集，不是纯召回集

[MSC 论文](https://aclanthology.org/2022.acl-long.356/)与[ParlAI 官方项目页](https://parl.ai/projects/msc/)提供多次重逢式人类对话。训练集包含 4,000 个三 session episode 和 1,001 个四 session episode；验证/测试延伸到五个 session。训练部分共约 **237K utterances**，并附有会话摘要。

原始任务关注下一轮对话生成和摘要记忆，主要自动指标是 perplexity，并辅以人工 engagingness 等评价。它能够测试模型是否利用过往 session，但缺少面向检索的完整 query-qrels 设计，也只有最多五个 session。

**建议定位：记忆驱动对话生成/摘要的辅助集；若目标是 Hippo 召回质量，不应优先于 LongMemEval、LoCoMo 或 LMEB。**

### 9. 新近方向：LongMemEval-V2

[LongMemEval-V2 论文](https://arxiv.org/abs/2605.12493)把记忆对象从用户画像扩展到 Agent 的环境经验：451 道人工问题，覆盖 static state recall、dynamic state tracking、workflow knowledge、environment gotchas 和 premise awareness；历史可达约 500 条 web-agent trajectory、115M tokens。官方代码见 [LongMemEval-V2 仓库](https://github.com/xiaowu0162/LongMemEval-V2)。

它适合未来评估 Hippo 是否能保存“如何完成任务、哪里会失败”这类经验记忆，但成本高、工作负载偏 web agent，不适合作为最先落地的 CI benchmark。

### 9.1 统一 harness：AMB 与 OmniMemEval

[Agent Memory Benchmark（AMB）](https://github.com/vectorize-io/agent-memory-benchmark)和[OmniMemEval](https://github.com/MemTensor/OmniMemEval)提供统一 adapter、runner、结果格式或 leaderboard，可降低同时运行 LoCoMo、LongMemEval、PrecisionMemBench 等数据集的工程成本。它们的价值主要在**评测基础设施**，并不构成新的中立 ground truth：

- AMB 由 Hindsight/Vectorize 团队维护，聚合多种 benchmark 和 provider 结果；
- OmniMemEval 由 MemTensor/MemOS 团队维护，覆盖 user memory 与 agent memory 多条评测线；
- 两者都应锁定 commit、adapter、judge 模型和配置，并用原 benchmark 官方脚本抽样交叉验证。

**建议定位：可借用 harness，不把其 leaderboard 当作唯一权威标准。**

### 9.2 BEAM：百万到千万 token 的规模压力基准

[BEAM 论文](https://arxiv.org/abs/2510.27246)包含 **100 段连贯对话和 2,000 道经验证的问题**，提供约 128K、500K、1M 和 10M token 四个规模档。它覆盖 information extraction、multi-hop reasoning、knowledge update、temporal reasoning、abstention、contradiction resolution、event ordering、instruction following、preference following 和 summarization 十种能力，官方代码和数据见 [BEAM 仓库](https://github.com/mohammadtavakoli78/BEAM)。

BEAM 的优势是上下文规模、领域和能力覆盖都明显大于 LoCoMo，并能观察召回预算、稠密/稀疏检索器以及长历史增长后的退化曲线。局限是主要采用 LLM-as-a-judge 的端到端分数，运行成本高，不能单独定位 Hippo 门禁的 precision/false-positive 问题。

**建议定位：release/nightly 级规模压力测试；优先从 128K 档开始，不作为 PR 级硬门槛。**

### 9.3 MemoryAgentBench：覆盖增量式 memory agent 能力

[MemoryAgentBench 论文](https://arxiv.org/abs/2507.05257)将输入拆成连续的多轮片段，要求系统逐步吸收并更新记忆，评估 accurate retrieval、test-time learning、long-range understanding 和 selective forgetting / conflict resolution 四类能力。它整合 LongMemEval 等已有任务，并新增 EventQA 与 FactConsolidation；官方实现见 [HUST-AI-HYZ/MemoryAgentBench](https://github.com/HUST-AI-HYZ/MemoryAgentBench)。

它比单纯 QA benchmark 更接近持续运行的 Agent，但不同子任务混用 exact match、Recall@5 与 LLM judge，得到的不是一个纯检索分数。对当前 Hippo 来说，FactConsolidation 和 conflict-resolution 子集最有价值，完整套件可留到评估 Warm/Cold Path 与 test-time learning 时使用。

**建议定位：长期记忆治理专项；不是替代 PrecisionMemBench 或 LongMemEval 的单一总榜。**

## 三、RAG 生成评测：有用，但不能替代召回 benchmark

### 10. Ragas：指标框架，不是固定 benchmark

[Ragas 论文](https://arxiv.org/abs/2309.15217)提出 reference-free 的自动 RAG 评测。原论文集中在：

- faithfulness：答案中的陈述能否由检索上下文支持；
- answer relevance：答案是否直接回答问题；
- context relevance：检索上下文是否足够聚焦、少冗余。

当前[官方指标文档](https://docs.ragas.io/en/latest/concepts/metrics/available_metrics/)还提供 context precision、context recall 等带参考信息的指标。

它适合做 Hippo 每次改动后的快速回归和生产样本抽检；但其得分依赖 judge LLM、prompt 和版本，而且 context relevance 之类代理指标不能可靠替代基于人工 qrels 的 Recall/nDCG。评测时必须固定 evaluator 模型并保留少量人工复核集。

### 11. RAGBench：评测 RAG evaluator 和生成链，不是原生全库召回测试

[RAGBench 论文](https://arxiv.org/abs/2407.11005)汇集约 **100K 个 RAG 样本**、12 个组件数据集和五个行业域。其 TRACe 框架包含：

- Relevance：上下文中真正与问题相关的比例；
- Utilization：生成器实际使用了多少上下文；
- Adherence：答案是否有上下文支撑；
- Completeness：回答是否覆盖了上下文中的相关信息。

数据见 [RAGBench 官方 Hugging Face 数据集卡](https://huggingface.co/datasets/galileo-ai/ragbench)。它的输入通常已经是 `(query, retrieved documents, response)`，主要用途之一是训练或比较 RAG evaluator。因此它适合检查 Hippo 最终注入与回答的质量，但不是让 Hippo 从完整记忆库里检索的原生测试，也不覆盖长期更新与作用域。

## 四、为什么不存在单一“行业标准”

从这些一手资料可以看到，benchmark 之间的目标函数并不相同：

- BEIR/MTEB 优化跨域文本检索；
- LMEB 优化长期、碎片化和上下文相关的 memory retrieval；
- LongMemEval/LoCoMo 优化跨会话问答、时间推理和拒答；
- MemBench 进一步加入 reflective memory、容量和效率；
- Ragas/RAGBench 评估检索后的上下文和回答。

即使在“正确率”这一名称下，也可能分别指：retrieval hit、LLM judge 判定的 QA correctness、多选题 accuracy、人工 coherence 或回答事实性。它们不能安全地合并成一个不透明总分。

另外，公共集普遍缺少 Hippo 的几个关键产品语义：

- `global` / `project` / `all` scope；
- `SUPERSEDED`、冲突事实、事实有效期和删除；
- 无答案时的 fail-closed 门禁；
- Prompt 注入式记忆和跨用户污染；
- 中文工程术语、路径、错误码、PR/commit 等精确词项；
- 写入抽取是否丢失 qualifier、否定和时间信息。

因此，**公共 benchmark 用于可比较性，域内黄金集用于产品有效性，两者缺一不可。**

## 五、Hippo 推荐评测组合

### A. 公共基准层

| 优先级 | Benchmark | Hippo 运行方式 | 主报告指标 |
| --- | --- | --- | --- |
| P0 | LMEB | 先评 embedding；再接 Hippo 候选召回与 rerank | nDCG@10、capped Recall@3/10、MRR |
| P0 | PrecisionMemBench | 直接对 search 输出做 must-include / must-exclude 与 scope/lifecycle 断言 | Precision/Recall、case pass、drift score、P95 latency |
| P0 | LongMemEval-S | 真实走 add/search/reader；同时保存 evidence session 排名 | Recall/nDCG@5/10、QA Accuracy、Abstention Accuracy |
| P1 | LoCoMo-10 | 转换 evidence turn 为 qrels，按类别分桶 | Hit/Recall/nDCG@3/10、QA F1、adversarial accuracy |
| P1 | MemBench 子集 | 运行 10K 与 100K 噪声设置 | Accuracy、Recall@10、read/write latency、容量曲线 |
| P1 | BEAM-128K | release/nightly 运行，逐步扩展到 1M/10M | 分能力 judge score、上下文 token、P95 latency、成本 |
| P2 | MemoryAgentBench 子集 | 优先 FactConsolidation、EventQA，再扩展完整能力集 | Accuracy、Recall@5、conflict-resolution accuracy |
| P2 | MTEB/BEIR 子集 | embedding 升级前跑中英、多语和 reranking 任务 | nDCG@10、Recall@100、资源占用 |
| P2 | Ragas/TRACe 抽检 | 对最终注入上下文与回答评分 | context precision/recall、faithfulness/adherence、completeness |

### B. Hippo 自有黄金集：必须保留为最终门禁

公共 benchmark 之外，建议维护 200～500 条 query 的版本化测试集，其中至少 25% 是无相关记忆或 adversarial 硬负样本。每条样本至少包含：

- `relevant_memory_ids` 与分级相关度；
- `forbidden_memory_ids`：过期、`SUPERSEDED`、跨项目、跨用户、冲突、注入文本；
- query time、scope、语言、问题类型；
- 期望返回空与否；
- 端到端参考答案及证据。

建议把内部指标分成四组，不压成单一分数：

1. **召回**：Candidate Recall@20、Hit/Recall@n、MRR、nDCG@n；
2. **污染**：Precision@n、Forbidden Leakage、Negative False Positive Rate、Empty Accuracy；
3. **端到端**：Answer Accuracy、Temporal/Update/Abstention 分桶；
4. **工程**：P50/P95 延迟、token 注入量、写入/查询成本、索引体积。

Hippo 的安全门禁应采用约束式验收，而不是只最大化平均 Recall，例如：

```text
先满足：
  cross-user / cross-project leakage = 0
  superseded leakage = 0
  hard-negative false positive rate <= 2%

再优化：
  Recall@3、nDCG@3、MRR、QA Accuracy
```

### C. 推荐的落地顺序

1. **先接 LMEB-Dialogue 的 LoCoMo、LongMemEval、MemBench 任务**，用统一 MTEB 格式比较当前 embedding 与候选召回；
2. **移植 PrecisionMemBench**，让 Hippo adapter 直接返回 memory ID，优先跑 scope、supersession、cross-user 和 drift case；
3. **再跑 LongMemEval-S 端到端**，固定 reader LLM、prompt、Top-K 和 token budget，避免把生成模型变化误判成检索变化；
4. **加入 Hippo 自有门禁集**，覆盖 scope、生命周期、冲突与无答案；
5. **每夜跑 LoCoMo/LongMemEval，PR 级跑裁剪后的 smoke set**；
6. **embedding 或 reranker 变更时补跑 MTEB/BEIR 子集**；
7. **定期对真实匿名化流量做 Ragas/TRACe + 人工复核**，监测数据漂移。

## 最终判断

如果问题是“行业有没有可直接采用的标准 benchmark”，答案是：

- **有标准化的检索协议与指标**：qrels、nDCG、Recall、MRR；
- **有广泛使用的长期记忆数据集**：LongMemEval、LoCoMo；
- **有最新的记忆检索统一基准**：LMEB；
- **但没有单一 benchmark 能完整代表 Hippo 的召回质量。**

Hippo 最合理的评测基线不是选其中一个，而是采用 **LMEB（embedding/排序）+ PrecisionMemBench（精度/门禁）+ LongMemEval/LoCoMo（端到端）+ Hippo 域内门禁集（产品语义）+ Ragas/TRACe（回答诊断）** 的分层组合。

## 一手来源索引

- LMEB：[论文](https://arxiv.org/abs/2603.12572)；[官方仓库](https://github.com/KaLM-Embedding/LMEB)
- PrecisionMemBench：[论文](https://arxiv.org/abs/2605.11325)；[官方仓库](https://github.com/tenurehq/precisionmembench)
- LongMemEval：[论文](https://arxiv.org/abs/2410.10813)；[官方仓库](https://github.com/xiaowu0162/LongMemEval)
- LoCoMo：[ACL 论文](https://aclanthology.org/2024.acl-long.747/)；[官方仓库](https://github.com/snap-research/locomo)
- MemBench：[ACL 论文](https://aclanthology.org/2025.findings-acl.989/)；[官方仓库](https://github.com/import-myself/Membench)
- MemoryBank：[论文](https://arxiv.org/abs/2305.10250)；[官方仓库](https://github.com/zhongwanjun/MemoryBank-SiliconFriend)
- Multi-Session Chat：[ACL 论文](https://aclanthology.org/2022.acl-long.356/)；[ParlAI 项目页](https://parl.ai/projects/msc/)
- BEIR：[论文](https://arxiv.org/abs/2104.08663)；[官方仓库](https://github.com/beir-cellar/beir)
- MTEB：[论文](https://arxiv.org/abs/2210.07316)；[官方仓库](https://github.com/embeddings-benchmark/mteb)
- Ragas：[论文](https://arxiv.org/abs/2309.15217)；[官方指标文档](https://docs.ragas.io/en/latest/concepts/metrics/available_metrics/)
- RAGBench：[论文](https://arxiv.org/abs/2407.11005)；[数据集卡](https://huggingface.co/datasets/galileo-ai/ragbench)
- LongMemEval-V2：[论文](https://arxiv.org/abs/2605.12493)；[官方仓库](https://github.com/xiaowu0162/LongMemEval-V2)
- BEAM：[论文](https://arxiv.org/abs/2510.27246)；[官方仓库](https://github.com/mohammadtavakoli78/BEAM)
- MemoryAgentBench：[论文](https://arxiv.org/abs/2507.05257)；[官方仓库](https://github.com/HUST-AI-HYZ/MemoryAgentBench)
- 统一 harness：[AMB](https://github.com/vectorize-io/agent-memory-benchmark)；[OmniMemEval](https://github.com/MemTensor/OmniMemEval)
