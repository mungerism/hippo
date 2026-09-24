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

### 3. 连接隔离临时集合运行真实引擎 (Engine Adapter)

```bash
uv run python -m benchmarks.runner --adapter engine --collection eval_bench_run_01
```

---

## 📂 扩展与接入新数据集

接入新的记忆评测集（如 LongMemEval、LoCoMo 或团队自有黄金集）只需构造符合 `BenchmarkDataset` 的 JSON 文件：

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
├── schemas.py      # CorpusItem, EvaluationQuery, Qrels, RunManifest, EvaluationTrace
├── metrics.py      # Recall, Precision, Hit, MRR, nDCG, ForbiddenLeakage, EmptyAccuracy
├── adapter.py      # BenchmarkAdapter, ReplayFixtureAdapter, HippoEngineAdapter
├── runner.py       # BenchmarkRunner, JSON/Markdown 报告生成, Baseline 比对 diff
└── README.md       # 本使用与扩展说明文档
```
