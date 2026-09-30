# Hippo 记忆召回评测框架 (Hippo Retrieval Evaluation Harness)

> 本模块为 Hippo 开发与 CI 侧独立评测套件，不属于生产包运行时，不增加 Agent MCP 工具参数，不扩大生产安全攻击面。

---

## 🌟 核心设计原则

1. **与具体 Benchmark 解耦**：以统一的 `BenchmarkDataset` Schema（Corpus、Queries、Qrels、Forbidden）抽象所有评测集，统一接入、统一产出。
2. **确定性指标与安全门禁优先**：
   - 遵循 Hippo 核心原则：**防污染与零泄漏优先于盲目刷高召回率**；
   - 严格统计 `Forbidden Leakage`（过期/跨项目/跨用户记忆泄漏）与 `Empty Accuracy`（硬负样本与置空拒答率）；
   - 对空 qrels、重复结果、未知 ID 和 tie score 给出确定性数学定义与保序去重。
3. **四阶段全链路可观测性 (Evaluation Trace)**：
   - `candidate`：底层向量/全文检索返回的原始候选（ID、分数、score_details）；
   - `lifecycle_scope`：活跃状态过滤（排斥 `status="superseded"`）与 scope/identity 校验；
   - `gate`：Relevance Gate 信号感知门禁（绝对门槛、纯稠密阈值、相对动态门槛）过滤原因；
   - `final`：最终截断的 Top-N 结果。
4. **存储严格物理隔离**：评测必须使用独立临时集合（前缀 `eval_`），**严禁触碰或读取用户真实生产记忆库**。
5. **生产零开销与 MCP 隔离**：生产环境下 `engine.search()` 保持无额外开销的极速路径；evaluation trace 绝不通过 MCP 暴露给 Agent。

---

## 📊 评测指标与确定性定义

| 指标 | 口径说明 | 边界处理与安全定义 |
| :--- | :--- | :--- |
| **Recall@k** | 命中正例数 / 真实正例总数 | 若无正例（负样本 query）：检索返回空则为 1.0，否则为 0.0。 |
| **Precision@k** | 命中正例数 / $k$ | 分母严格为 $k$；未知 ID 视为 0 相关度。 |
| **Hit Rate@k** | Top-k 中是否存在至少一个正例 | 存在 $\ge 1$ 记 1.0，否则 0.0。 |
| **MRR** | 首个命中正例的倒数排名 ($1/\text{rank}$) | 未命中记 0.0。 |
| **nDCG@k** | 分级相关度折扣增益 ($\text{DCG}/\text{IDCG}$) | 支持 $grade \ge 1$ 分级打分；理想全负时记 1.0。 |
| **Forbidden Leakage** | Top-k 中命中的禁用记忆数量 | **Hippo 硬安全指标，必须恒等于 0**。 |
| **Empty Accuracy** | 负样本与置空正确率 | Top-k 完全为空记 1.0，返回任何候选记 0.0。 |

> **口径约定**：`@3` 为 Hippo 默认主报告口径（与生产 `max_injected=3` 保持一致）。

---

## 🚀 快速起步

### 1. 运行内置 Smoke 评测 (Replay 离线模式)

不需要网络或 Qdrant 守护进程，秒级完成验证：

```bash
uv run python -m benchmarks.runner
```

输出示例：
```text
Evaluation succeeded!
JSON report: benchmarks/reports/report_hippo_smoke_fixture.json
Markdown summary: benchmarks/reports/report_hippo_smoke_fixture.md
--------------------------------------------------
Recall@3: 0.5000
Precision@3: 0.1667
Forbidden Leakage@3: 0
Empty Accuracy@3: 0.0000
--------------------------------------------------
```

### 2. 与已批准 Baseline 进行对比 (Regression Diff)

```bash
uv run python -m benchmarks.runner --baseline benchmarks/reports/report_hippo_smoke_fixture.json
```

若当前变更发生指标退化（Recall 下降）或安全违规（Forbidden Leakage > 0），命令将自动生成 `diff_*.md` 并返回退出码 2。

### 3. 运行 LongMemEval-S 双 Profile 基准评测 (ICLR 2025)

正式运行使用官方 **longmemeval-cleaned / LongMemEval-S**。数据源固定到 revision
`98d7416c24c778c2fee6e6f3006e7a073259d48f`，并校验 SHA-256
`d6f21ea9d60a0d56f34a05b609c79c88a451d2ae03597821ea3d5a9678c3a442`。
首次使用 `--dataset longmemeval-s` 会下载约 277 MB 数据到
`~/.hippo/benchmarks/data/`；hash 不匹配时拒绝运行。

支持官方五类能力（Information Extraction、Multi-Session、Temporal Reasoning、Knowledge Update、Abstention）和两个 ingest profile。正式 benchmark 必须使用隔离的真实 Hippo engine：

- **Direct-Facts Profile**：按 gold evidence session 写入提炼后的事实，评测 retrieval/gate，不把 gold answer 偷渡成 evidence。
  ```bash
  uv run python -m benchmarks.runner \
    --dataset longmemeval-s \
    --adapter engine \
    --profile direct-facts \
    --tier retrieval
  ```

- **Mem0-Session Profile**：将完整 timestamped sessions 通过 `infer=True` 摄入，评测真实记忆提炼与检索。
  ```bash
  uv run python -m benchmarks.runner \
    --dataset longmemeval-s \
    --adapter engine \
    --profile mem0-session \
    --tier retrieval
  ```

Retrieval 会至少取到 Top-10 用于正确计算 Recall/nDCG@5/10，但 Hippo 主口径和 End-to-End reader context 仍严格遵循 `max_injected=3`。

- **真实 QA / 三级评测**：正式数据禁止使用 CI mock reader/judge。以下示例固定 reader、judge、temperature 与 token budget，并记录到 report manifest。
  ```bash
  uv run python -m benchmarks.runner \
    --dataset longmemeval-s \
    --adapter engine \
    --profile direct-facts \
    --tier all \
    --qa-backend gemini \
    --reader-model gemini-3.5-flash-lite \
    --judge-model gemini-3.5-flash-lite \
    --qa-temperature 0 \
    --reader-token-budget 256 \
    --judge-token-budget 10
  ```

QA 运行会额外生成 `*_official.jsonl`（`question_id` + `hypothesis`），可直接交给 LongMemEval 官方 `src/evaluation/evaluate_qa.py` 做交叉验证。

> `Ingest Loss`、`Retrieval→QA Gap`、`Reader Loss` 是跨阶段的诊断差值，量纲/交互不同，**不可相加**；`Total Loss` 单独定义为 `1 - End-to-End Accuracy`。单次只跑一个 ingest profile 时不会伪造另一个 profile 的 baseline。

PR/CI 的零网络 smoke 使用与官方 schema 相同形状的 5 题 fixture：

```bash
uv run python -m benchmarks.runner --dataset longmemeval-fixture --tier all --qa-backend mock
```

### 4. 运行 LoCoMo-10 超长对话基准评测 (ACL 2024)

Snap Research 发布的 **LoCoMo-10** 包含 10 组跨越数月的大规模多轮对话，共 1,986 道评测题。Hippo 将官方数据固定到 commit
`3eb6f2c585f5e1699204e3c3bdf7adc5c28cb376`，并校验 `locomo10.json` 的 SHA-256
`79fa87e90f04081343b8c8debecb80a9a6842b76a7aa537dc9fdf651ea698ff4`。首次运行
`--dataset locomo` 会下载到 `~/.hippo/benchmarks/data/locomo10.json`；缓存 hash 不匹配会从固定 revision 重新下载，绝不会退化为 CI fixture。

官方 category ID 与报告名称严格按 upstream scorer 对齐：
1. **Multi-Hop (多跳推理，Category 1)**: 282 题 (14.2%)。
2. **Temporal (时间感知，Category 2)**: 321 题 (16.2%)。
3. **Open-Domain (开放域查询，Category 3)**: 96 题 (4.8%)。
4. **Single-Hop (单跳查询，Category 4)**: 841 题 (42.3%)。
5. **Adversarial (对抗样本，Category 5)**: 446 题 (22.5%)。

- **评测口径**：
  - **Retrieval**：实际检索至少 Top-10，用于正确计算 Hit/Recall/Precision/nDCG@3/10 与 MRR；生产注入/reader context 仍只取 Top-3。
  - **QA Categories 1-4**：复刻官方 `task_eval/evaluation.py` 的 Porter stemming、Category 1 逗号分隔 multi-answer、Category 3 分号截断等 F1 规则；EM 仅作为补充诊断。
  - **Category 5**：单独报告 `Adversarial Accuracy`，不再与 Categories 1-4 的 QA F1 混成一个总分。
  - 正式 QA 使用固定 reader prompt；model、temperature、token budget 与 prompt SHA256 全部写入 manifest。正式数据禁止 mock reader。

- **快速 Smoke / CI 离线评测**（显式使用内置 fixture，不读取或替代官方缓存）：
  ```bash
  uv run python -m benchmarks.runner \
    --dataset locomo-fixture \
    --tier all \
    --qa-backend mock
  ```

- **全量 Retrieval**：
  ```bash
  uv run python -m benchmarks.runner \
    --dataset locomo \
    --adapter engine \
    --tier retrieval \
    --collection eval_locomo_full
  ```

- **全量 End-to-End QA**：官方 scorer 使用 NLTK `PorterStemmer`，通过 `--with 'nltk==3.9.2'` 固定提供该评测依赖；实际 NLTK 版本也会写入 manifest。
  ```bash
  uv run --with 'nltk==3.9.2' python -m benchmarks.runner \
    --dataset locomo \
    --adapter engine \
    --tier end-to-end \
    --qa-backend gemini \
    --reader-model gemini-3.5-flash-lite \
    --qa-temperature 0 \
    --reader-token-budget 256 \
    --collection eval_locomo_full
  ```

### 5. 运行 BEAM-128K 规模与延迟基准评测 (Mem0 官方)

针对数万条记忆（128K tokens 上下文）下的系统容量与检索表现进行度量，包括写入吞吐率（items/s）、查询延迟分位数（P50/P95/P99 ms）、Qdrant 向量索引体积与成本估算。

- **快速 Smoke / CI 离线评测**：
  ```bash
  uv run python -m benchmarks.runner --dataset beam-fixture --adapter replay
  ```

- **全量规模评测 (Qdrant Engine 隔离集合)**：
  ```bash
  uv run python -m benchmarks.runner \
    --dataset beam-128k \
    --adapter engine \
    --collection eval_beam_128k
  ```

### 6. 运行 LMEB 对话记忆组件对比评测 (KaLM-Embedding)

用于对标 MTEB/KaLM 规范，度量不同 Embedding Profile 在长对话记忆中的候选召回与重排质量（`nDCG@10`、`Recall@10`、`MRR`）。

> ⚠️ **免责声明**：LMEB 仅评估向量表征与候选召回组件质量，不等于生产端到端安全。选型变更必须在 `hippo_gold_v1` 上通过安全硬门禁。

- **快速 Smoke / CI 离线评测**：
  ```bash
  uv run python -m benchmarks.runner --dataset lmeb-fixture --adapter replay
  ```

- **全量对话记忆组件对比**：
  ```bash
  uv run python -m benchmarks.runner \
    --dataset lmeb-dialogue \
    --adapter engine
  ```

### 7. 连接隔离临时集合运行真实引擎 (Engine Adapter)

```bash
uv run python -m benchmarks.runner --adapter engine --collection eval_bench_run_01
```

---

## 📂 扩展与接入新数据集

接入新的记忆评测集（如 LoCoMo、BEAM 或团队自有黄金集）只需构造符合 `BenchmarkDataset` 的 JSON 文件：

### 1. 数据集 JSON Schema 规范

```json
{
  "name": "my_benchmark_v1",
  "version": "1.0.0",
  "description": "自定义长对话与工程记忆评测集",
  "corpus": [
    {
      "id": "mem_001",
      "text": "Hippo 使用 Qdrant 6333 端口作为默认向量引擎。",
      "scope": "project",
      "project_id": "hippo",
      "user_id": "test_user",
      "status": "active",
      "category": "architecture"
    },
    {
      "id": "mem_002_old",
      "text": "Hippo 使用 SQLite 作为纯本地向量存储（已废弃）。",
      "scope": "project",
      "project_id": "hippo",
      "user_id": "test_user",
      "status": "superseded",
      "category": "architecture"
    }
  ],
  "queries": [
    {
      "query_id": "q_001",
      "query": "Hippo 默认使用的是什么向量引擎和端口？",
      "scope": "project",
      "project_id": "hippo",
      "user_id": "test_user",
      "expected_empty": false,
      "category": "architecture"
    }
  ],
  "qrels": {
    "q_001": {
      "mem_001": 2
    }
  },
  "forbidden": {
    "q_001": [
      "mem_002_old"
    ]
  }
}
```

### 2. 运行自定义数据集

```bash
uv run python -m benchmarks.runner --dataset path/to/my_benchmark_v1.json
```

---

## 📁 模块架构速览

```text
benchmarks/
├── schemas.py          # CorpusItem, EvaluationQuery, Qrels, RunManifest, EvaluationTrace
├── metrics.py          # Recall, Precision, Hit, MRR, nDCG, ForbiddenLeakage, EmptyAccuracy
├── adapter.py          # BenchmarkAdapter, ReplayFixtureAdapter, HippoEngineAdapter
├── runner.py           # BenchmarkRunner, JSON/Markdown 报告生成, Baseline 比对 diff
├── gold/               # 自研 Hippo Gold v1 黄金集规范、校验器与构建器
├── longmemeval/        # LongMemEval-S 评测适配器 (ICLR 2025)
│   ├── loader.py       # 数据加载与 direct-facts / mem0-session schema 转换
│   ├── ingest.py       # DirectFactsIngestStrategy 与 Mem0SessionIngestStrategy
│   └── evaluator.py    # 三级 Tier 评测器 (Retrieval, Oracle, E2E) 与误差归因
├── locomo/             # LoCoMo-10 超长对话基准评测适配器 (ACL 2024)
│   ├── loader.py       # 数据加载、Turn/Session 解析与 BenchmarkDataset 转换
│   └── evaluator.py    # SQuAD Token F1、Exact Match、对抗样本拒答判定与分类报告
├── beam/               # BEAM-128K 规模基准评测适配器 (Mem0)
│   ├── loader.py       # 规模数据加载、SHA256 校验与格式转换
│   └── evaluator.py    # 延迟分位数 (P50/P95/P99)、写入吞吐率、索引体积与成本测算
├── lmeb/               # LMEB 对话记忆组件对比评测适配器 (KaLM)
│   ├── loader.py       # 对话记忆子集加载与格式转换
│   └── evaluator.py    # nDCG@10、Recall@10、MRR 多 Profile 对比矩阵与免责声明
├── data/               # 评测轻量级 fixture 与数据集
└── README.md           # 本使用与扩展说明文档
```
