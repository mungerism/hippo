# Jev 成对关系离线评测

#62 的评测流水线与检索 qrels 分离，不读取 Qdrant，也不调用真实模型。运行本地示例：

```bash
uv run python benchmarks/jev_relationships.py \
  --dataset benchmarks/jev_pairs_v1.json \
  --recordings benchmarks/jev_recordings_synthetic_v1.json
```

命令把 JSON 报告写到标准输出，不修改记忆。样本文件使用 `relationship-pairs-v1`：每条包含唯一 `id`、`cluster_id`、`split`（`calibration` / `test`）、`language`（`zh` / `en` / `mixed`）、`scenario`、两侧事实文本、`gold_relation` 与 `must_abstain`。同一事实簇不能跨校准和测试集；程序在读取时拒绝泄漏。`label_provenance` 明示标签来源。

录制文件使用 `relationship-recordings-v1`，分别提供 `baseline` 与 `jev` 的模型、rubric 版本、来源和逐样本判别。判别可以是三类之一，或 `null` 加弃权原因。Jev 的有效判别还需完整三类概率和独立的 `provider_confidence`。每条可记录耗时、输入/输出 token 与美元成本；字段必须是有限非负数。模型录制判别只是被评估对象，不能替代样本的语义标签。

报告按后端、校准/测试、语言输出：逐样本匿名 ID 结果、各关系 precision/recall、错误合并和错误废弃数、弃权和必弃权违规数、Jev 所选类概率分桶、p50/p95 延迟、token 与成本总计。空分母显示 `null`，而不是虚假的零分。报告不包含事实原文、密钥或候选生产 ID。

仓库自带的样本与录制均为**合成演示夹具**：标签由作者拟定，尚未经独立人工复核；模型判别、零延迟及零成本也不是实际调用测量。它们只证明流水线可运行并覆盖同义、冲突、兼容细节、时间/条件、跨主体、注入和必须弃权场景，**不可用于选阈值或批准生产接管**。用于决策前须另行取得人工审定的事实簇隔离数据集，以及实际模型录制及 token/时延数据；保留模型和 rubric 版本以便复现。
