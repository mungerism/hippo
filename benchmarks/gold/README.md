# Hippo Gold v1 评测黄金集与 Baseline 规范指南

> **模块定位**：本模块为 Hippo 域内召回质量黄金集（Gold v1）及评测 Baseline。Replay 仅用于离线确定性回归；生产 Baseline 必须由 `HippoEngineAdapter` 真实写入/搜索链路生成。
> 隶属于 GitHub Issue [#54](https://github.com/mungerism/hippo/issues/54) 与 Epic [#52](https://github.com/mungerism/hippo/issues/52)。

---

## 🌟 一、数据规范与场景矩阵

### 1. 基础规模与配比指标
- **总题目数**：210 条精心标注的评估 Query（满足 Issue #54 $\ge 200$ 条要求）；
- **主检索题目**：200 条，其中 70 条 retrieval hard-negative（**35.00%**，满足 $\ge 25\%$ 要求）；
- **Persistence 诊断题目**：10 条，仅诊断“这类内容是否应该进入长期记忆”，不参与 retrieval hard gate 与主 Recall/nDCG/MRR；
- **语料规模**：114 条覆盖多用户、多项目、多生命周期状态的脱敏工程记忆；
- **脱敏承诺**：100% 合成或工程脱敏语料，绝不包含真实用户密钥、Token 或个人隐私；
- **时间契约**：每条 Query 必须带结构化 `query_time`；时间场景的 corpus 使用绝对 `event_time`，避免把 `today/yesterday` 同时写入问题和答案造成词面自证。

### 2. 九类场景覆盖矩阵
| 场景标识 (Category) | 题数 | 场景特征与验证目标 |
| :--- | :---: | :--- |
| `exact_paraphrase_term` | 45 | 精确技术术语、近义词改写、中英文技术词汇互查（如 Qdrant/Port, Python/uv, VitePress/pnpm）。 |
| `scope_isolation` | 25 | 验证 `project`、`global` 与 `all` 作用域的正交隔离，防止项目内检索跨到全局或反之。 |
| `identity_isolation` | 25 | 跨用户（Alice vs Bob/Carol）与跨项目（Hippo vs Zebra/Octopus）身份物理隔离与机密防泄漏。 |
| `lifecycle_conflict` | 25 | 新旧技术版本冲突（如废弃 SQLite 升级 Qdrant），验证 Active 正例召回与 Superseded 严格拦截。 |
| `temporal_intent` | 20 | 时间窗口意图检索（今天、昨天、过去24小时、上周、最近升级等）。 |
| `multi_evidence` | 15 | 多跳/多证据支撑题（单一 Query 需联合命中 2~4 条证据片段）。 |
| `hard_negative` | 35 | 语义诱导型硬负样本（问及未引入的技术如 Django, React, HDFS），验证门禁拒答与完全置空。 |
| `retrieval_safety` | 10 | 已污染存储中的 raw log、control-tag、instruction override 等，验证 retrieval defense-in-depth。 |
| `persistence_quality` | 10 | 确认语、闲聊、占位文本、无关食谱等，诊断 session distillation / persistence quality；不作为 direct-facts retrieval 的硬门禁。 |

---

## 🛡️ 二、四大安全硬门禁不变量

根据 Issue #54 与 ADR 0006，每次评测运行必须通过以下四项产品级安全硬门禁，**任何一项不合规即判定为回归阻断（CI Exit Code 2）**：

| 安全硬门禁不变量 | 门禁阈值 | 测量口径与违规判定 |
| :--- | :---: | :--- |
| **Cross-User Leakage** (跨用户泄漏) | **恒等于 0** | 任何属于其他 `user_id` 的私密记忆进入最终 Top-k 均视为违规。 |
| **Cross-Project Leakage** (跨项目泄漏) | **恒等于 0** | 任何属于其他 `project_id` 的专有记忆进入最终 Top-k 均视为违规。 |
| **Superseded Leakage** (过期事实泄漏) | **恒等于 0** | 状态为 `superseded` 的记忆进入最终 Top-k 均视为违规。 |
| **Hard-Negative FPR** (硬负样本假阳率) | **$\le 2.00\%$** | 在 retrieval-scored 的预期置空题目中，返回任何候选记忆的比例必须 $\le 2\%$；`persistence_quality` 不计入该分母。 |

---

## 🚀 三、命令行执行与审计

### 1. 校验数据集完整性与脱敏合规
```bash
uv run python -m benchmarks.gold.validator --dataset benchmarks/data/hippo_gold_v1.json
```

### 2. 重新构建生成 Gold v1 数据集
```bash
uv run python -m benchmarks.gold.builder
```

### 3. 运行评测并生成报告
```bash
# 离线 Replay：只用于 schema / gate / report 的确定性回归，不得作为生产 Baseline
uv run python -m benchmarks.runner --dataset benchmarks/data/hippo_gold_v1.json

# 生产 Baseline：必须使用真实 HippoEngine + 隔离 Qdrant collection
uv run python -m benchmarks.runner \
  --dataset benchmarks/data/hippo_gold_v1.json \
  --adapter engine \
  --output-dir benchmarks/baselines \
  --report-name hippo_gold_v1_baseline
```

### 4. 与核准 Baseline 进行对比 (Regression Diff)
```bash
uv run python -m benchmarks.runner \
  --dataset benchmarks/data/hippo_gold_v1.json \
  --adapter engine \
  --baseline benchmarks/baselines/hippo_gold_v1_baseline.json
```

---

## 🔬 四、错误样本分类学 (Error Taxonomy)

对 retrieval-scored 主口径中的失败题目，应按 trace 区分 candidate、lifecycle/scope 与 gate 损失；`persistence_quality` 单独作为 ingest/session-distillation 诊断，不与 retrieval recall 混算。

1. **纯中文抽象语义转述缺口 (Chinese Lexical Gap, 占比 ~50%)**：
   - *现象*：如英文语料记载 `"The Spool queue operates as an append-only WAL..."`，Query 为纯中文 `"本地后台入库 spool wal 目录路径"`。
   - *原因*：在缺乏本地多语言交叉重排器或高维稠密嵌入的离线纯词法仿真下，词面 overlap 较低，被 Gate 的 0.32 门槛过滤；实际生产通过 BGE 中英双语向量引擎即可覆盖。
2. **多证据截断 (Multi-evidence Truncation, 占比 ~30%)**：
   - *现象*：部分多证据题目需要召回 4 条事实（如全流程 4 阶段），但生产受限于 `max_injected=3`。
   - *分析*：Top-3 召回率天然受 `min(k, |rel|) / |rel|` 物理截断约束，符合预期设计。
3. **安全置信度保守抑制 (Conservative Suppression, 占比 ~20%)**：
   - *现象*：部分边缘题目候选得分处于 0.28 ~ 0.31 区间，被 Gate 严格拦截。
   - *分析*：遵循 Hippo 原则“**防污染与零泄漏优先于盲目刷高召回率**”，宁可漏召回，绝不放行低置信度噪点。

---

## 📋 五、Baseline 更新与回滚规则

### 1. 更新准入条件 (Promotion Criteria)
- 必须基于独立的 Feature 分支提交，严禁直接修改 `main` 分支上的 Baseline；
- Recall@3 / nDCG@3 / MRR 任一绝对下降超过 **2 个百分点**即阻断；P95 延迟同 profile 上升超过 **10%**先告警，连续确认后升级为阻断；
- 四大安全硬门禁必须全部保持绿色合格（Leakage 恒为 0，FPR $\le 2\%$）；
- Baseline manifest 必须记录 `adapter=HippoEngineAdapter`、embedding profile、gate 配置、dataset hash、`max_injected`、ingest profile 与可获得的索引体积；Replay/unknown adapter 的报告不得晋升为生产 Baseline；
- 生产 Baseline 必须从 **clean committed worktree** 生成；manifest `git_sha` 必须是精确的 40 位 commit SHA，禁止 `-dirty`，且评测 adapter 只能做 ID/trace 归一化，不得在生产结果之后追加 benchmark-only filtering/re-ranking；
- 同一 PR 中必须同步提交生成的 Markdown 差异对比报告。

### 2. 回滚机制 (Rollback Procedure)
若线上出现误召回或严重回归，通过 Git 恢复至上一个核准版本并执行 Diff 验证：
```bash
git checkout origin/main -- benchmarks/baselines/hippo_gold_v1_baseline.json
uv run python -m benchmarks.runner --baseline benchmarks/baselines/hippo_gold_v1_baseline.json
```
