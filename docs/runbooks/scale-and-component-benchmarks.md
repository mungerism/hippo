# 规模评测与 Embedding 选型对比运维手册（BEAM & LMEB）

本手册说明 Hippo 如何运行 **BEAM 多规模检索/容量评测**与 **LMEB Dialogue/MemBench 组件评测**。两者都属于外部 benchmark，不替代 Hippo Gold v1 的生产安全门禁。

> [!IMPORTANT]
> 上游 BEAM 数据集文档把最小档描述为约 128K tokens，但 Mem0 当前公开 benchmark runner 与 Hugging Face split 名称使用 `100K`。Hippo 的正式命令因此使用 `beam-100k`；历史 `beam-128k` 仅保留为兼容别名，并在 manifest 中标记。

## 1. 安全与口径边界

- **Hippo Gold v1**：生产安全主门禁，覆盖跨用户/跨项目隔离、superseded、hard negative 与 forbidden leakage。
- **BEAM**：关注长历史规模增长下的检索质量、写入吞吐、P50/P95/P99、索引体积和成本。Hippo 当前接入的是 **retrieval/scale proxy**；BEAM 官方 answer + rubric judge 分数是另一层端到端指标，不能混为一谈。
- **LMEB**：只衡量 embedding 表征与候选召回组件，报告 nDCG@10、Recall@10、MRR；不得把组件高分视为生产端到端质量。

所有 engine 评测使用 `eval_*` 独立 collection，并在运行结束后清理。

## 2. 离线 Smoke

普通 PR/CI 不下载公共数据，不调用付费模型：

```bash
uv run python -m benchmarks.runner --dataset beam-fixture --adapter replay
uv run python -m benchmarks.runner --dataset lmeb-fixture --adapter replay
```

fixture 只能通过显式 `*-fixture` alias 使用。正式 alias 下载失败、revision 不匹配或 schema 变化时直接失败，**不会回退 fixture**。

## 3. BEAM 正式规模评测

### 3.1 固定数据源

Hippo 对齐 Mem0 `memory-benchmarks` 当前 BEAM 数据入口：

- Hugging Face：`Mohammadta/BEAM`
- 数据 revision：`8b4ddc477010c07a852752fe2f27a2722755ff2b`
- Mem0 BEAM runner revision：`4b61c5d31b9c668a12b4f5e78064248a02c82d2b`
- 支持首版规模：`100K`、`500K`、`1M`；10M 仍保持手工/后续扩展

正式数据依赖 Hugging Face `datasets`，不加入 Hippo 生产依赖：

```bash
uv run --with datasets --with huggingface_hub python -m benchmarks.runner \
  --dataset beam-100k \
  --adapter engine \
  --output-dir benchmarks/reports/scale \
  --report-name beam100k
```

`Recall@10` / `nDCG@10` 的检索深度至少为 Top-10；`max_injected=3` 仍只代表 Hippo Agent 的生产注入预算，不再截断 benchmark 的 @10 指标。

### 3.2 资源与成本 fail-closed

BEAM/LMEB 在真正 ingest 前先执行预算预检。默认：

- `--max-benchmark-items 250000`
- `--max-estimated-embedding-cost-usd 5.0`
- `--embedding-cost-per-million-tokens 0.02`

超过任一上限直接以非零退出码停止，不会部分执行后把结果当成完整 benchmark。运行 500K/1M 时必须显式提高资源上限，例如：

```bash
uv run --with datasets --with huggingface_hub python -m benchmarks.runner \
  --dataset beam-500k \
  --adapter engine \
  --max-benchmark-items 1000000 \
  --max-estimated-embedding-cost-usd 10 \
  --output-dir benchmarks/reports/scale \
  --report-name beam500k
```

预算参数与运行前估算值都会进入 manifest。

### 3.3 生成质量—规模曲线

至少运行两个不同规模后，合并报告：

```bash
uv run python -m benchmarks.runner \
  --beam-compare-report benchmarks/reports/scale/beam100k.json \
  --beam-compare-report benchmarks/reports/scale/beam500k.json \
  --output-dir benchmarks/reports/scale \
  --report-name beam_scale_curve
```

输出按 corpus size 排序的 JSON/Markdown 曲线表，包含：

- Recall@3 / Recall@10 / nDCG@10
- P95 query latency
- estimated embedding cost
- index size
- corpus memories / estimated tokens

如果两个报告的 corpus size 相同，合并命令 fail-closed。

## 4. LMEB Embedding Profile 对比

### 4.1 固定数据源

首版选用 LMEB 的 **Dialogue / MemBench / single_hop** retrieval 子集：

- Hugging Face：`KaLM-Embedding/LMEB`
- 数据 revision：`9671811`
- corpus config：`MemBench_corpus`
- query config：`MemBench_queries`
- qrels：`Dialogue/MemBench/single_hop/qrels.tsv`

正式运行：

```bash
uv run --with datasets --with huggingface_hub python -m benchmarks.runner \
  --dataset lmeb-dialogue \
  --adapter engine \
  --lmeb-profile-name vertex-gemini-768 \
  --output-dir benchmarks/reports/lmeb \
  --report-name lmeb_vertex_gemini_768
```

> [!CAUTION]
> `--profile direct-facts|mem0-session` 是 **ingestion profile**，不是 embedding profile。Embedding provider/model/dimensions 由 Hippo 配置/环境决定，并写入报告。不要用 `--profile` 模拟 BGE/OpenAI/Vertex 等模型差异。

### 4.2 并列比较两个或更多 embedding profile

不同 provider 往往需要不同凭据与进程环境，因此先分别运行，再合并结果。示意：

```bash
# Profile A：按当前 Vertex 配置运行
HIPPO_PROVIDER=vertexai \
uv run --with datasets --with huggingface_hub python -m benchmarks.runner \
  --dataset lmeb-dialogue --adapter engine \
  --lmeb-profile-name vertex-gemini-768 \
  --output-dir benchmarks/reports/lmeb \
  --report-name lmeb_vertex

# Profile B：按当前 OpenAI 配置运行
HIPPO_PROVIDER=openai \
uv run --with datasets --with huggingface_hub python -m benchmarks.runner \
  --dataset lmeb-dialogue --adapter engine \
  --lmeb-profile-name openai-small-1536 \
  --output-dir benchmarks/reports/lmeb \
  --report-name lmeb_openai
```

生成真正的并列报告：

```bash
uv run python -m benchmarks.runner \
  --lmeb-compare-report benchmarks/reports/lmeb/lmeb_vertex.json \
  --lmeb-compare-report benchmarks/reports/lmeb/lmeb_openai.json \
  --output-dir benchmarks/reports/lmeb \
  --report-name lmeb_profile_comparison
```

合并器要求至少两个**不同 profile**且 dataset 一致；否则拒绝生成比较报告。

## 5. GitHub Actions

- PR CI：只跑 `beam-fixture` / `lmeb-fixture`，零网络、零付费。
- Release：默认执行 pinned BEAM 100K scale/retrieval gate。
- Manual release workflow：可关闭/开启 BEAM，并通过 `run_lmeb` + `lmeb_profile_name` 运行当前配置的 LMEB profile。不同 embedding profile 可分多次手工运行，再用上面的 merge 命令并列比较。
- 所有 JSON/Markdown 报告作为 workflow artifact 保留。

## 6. 生产选型终审

Embedding 变更完成 LMEB 对比后，仍必须重新运行 Hippo Gold v1：

```bash
uv run python -m benchmarks.runner \
  --dataset benchmarks/data/hippo_gold_v1.json \
  --adapter engine \
  --baseline benchmarks/baselines/hippo_gold_v1_baseline.json
```

必须满足安全硬门禁和 baseline regression gate 后，才能考虑更新生产配置。

## 7. 常见错误

**正式 BEAM/LMEB 使用 replay**：正式 alias 强制 `--adapter engine`；CI 请显式使用 `beam-fixture` / `lmeb-fixture`。

**缺少 Hugging Face 依赖**：正式公共集不会隐式安装依赖，也不会退化到 fixture；使用 `uv run --with datasets --with huggingface_hub ...`。

**预算超限**：提高预算前先确认预估成本与机器资源；不要为了“让 benchmark 跑完”无条件放宽上限。

**把 LMEB 高分当生产质量**：LMEB 是 component benchmark，生产上线仍由 Hippo Gold v1 的 scope/lifecycle/zero-leakage 门禁决定。
