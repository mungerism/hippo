# Jev 分类校准与 Go/No-Go 门禁

在 Cold Path 记忆治理中，`EQUIVALENT`（合并）与 `CONFLICT`（作废/替代）属于**破坏性治理操作**。Jev 模型的概率输出不能直接作为自动合并授权；#64 建立了可复现的分类校准体系与 RFC 安全门禁，只有通过全部门禁的校准产物方可产出合格配置供后续接管（#65）使用。

---

## 🌟 核心设计与双阈值约束

为防止模型在边界用例中误合并或误作废事实，校准流水线严格分成两阶段：

1. **Calibration split 选择阈值**：分别为 `EQUIVALENT` 与 `CONFLICT` 搜索“零误治理”的最低类别概率与第一/第二名概率差（Margin）组合。默认值（0.80/0.25 与 0.75/0.20）只是**阈值下限**，不是直接拿去测试集验收的固定生产配置。
2. **Test split 冻结验收**：阈值一旦由 calibration split 选定，test split 只用于 Go/No-Go，不再调整阈值，避免测试集泄漏。
3. **独立事实与弃权**：`DISTINCT` 不触发治理；并列最大概率、未达双阈值或模型显式弃权均视为弃权。Rule-of-Three 的分母只统计真正会触发治理的 `EQUIVALENT` / `CONFLICT` 接受样本。

---

## 🚦 RFC-0001 / RFC-0002 验收门禁 (Go / No-Go)

校准流水线依据 RFC 安全规范对测试集（Test Split）执行严格审查：

| 审查维度 | 准入标准 | 违规后果 |
| :--- | :--- | :--- |
| **误治理零容忍** | 测试集中的 `false_merge` 与 `false_supersede` 必须恒为 0 | 立即判定 `NO_GO`，阻断接管配置导出 |
| **语言切片独立达标** | 中文（`zh`）与中英混合（`mixed`）切片必须各自满足零误治理 | 禁止总平均掩盖中文/混合切片的误治理风险 |
| **必须弃权场景** | 注入文本（Prompt Injection）等标记为 `must_abstain` 的样本违规为 0 | 立即判定 `NO_GO` |
| **覆盖率底线** | 有效测试样本的接收率（Coverage）不得低于门槛（默认 50%） | 避免通过无限制弃权逃避安全校验 |
| **Recall 相对基线** | EQUIVALENT/CONFLICT 的总体及中文/混合切片 recall 相对真实基线下降不得超过 2 个百分点 | 防止“全弃权”刷安全指标 |
| **经济性与延迟** | 真实测量下 Jev 总成本不高于基线 50%，p95 不劣于基线 | 合成/缺失测量不得生成生产 GO |
| **校准可靠性** | 报告 Brier score、概率分桶、coverage 与破坏性接受数 | 让概率质量可审查，而非只看一个阈值 |
| **置信上界统计** | 在 $n$ 个真正放行的破坏性样本 0 错误时，依据 Rule of Three 报告 95% 置信上界 ($\approx 3/n$，上限封顶 1) | DISTINCT 不得稀释风险上界 |
| **样本量充足性** | 测试集样本数必须达到 RFC 门槛（默认 $\ge 30$） | 样本不足时严格生成 `NO_GO`，不产出活跃接管配置 |

> [!IMPORTANT]
> 仓库自带的合成夹具不仅样本量不足，而且其 baseline/Jev 均标记为 synthetic measurement。默认严格门禁会**确定性生成 `NO_GO`**，且 `active_configuration = null`。单元测试可以显式关闭 takeover-evidence 门禁来验证算法机械行为，但这种结果不能用于 #65。

---

## 🚀 运行校准

### 1. 离线 Replay 回放模式（零网络、零密钥）

无需真实 API Key 即可快速运行校准并生成产物报告：

```bash
uv run python benchmarks/jev_calibration.py \
  --dataset benchmarks/jev_pairs_v1.json \
  --recordings benchmarks/jev_recordings_synthetic_v1.json \
  --output benchmarks/reports/calibration_jev_v1.json
```

输出示例：
```text
=== Calibration Status: NO_GO ===
Disqualification reasons:
  - sample_size_insufficient: test split has 6 samples, minimum required by policy is 30
Test split: count=6, accepted=4, coverage=0.8, false_merge=0, false_supersede=0, error_upper_bound_95=0.9986
Wrote calibration artifact to benchmarks/reports/calibration_jev_v1.json
```

### 2. 真实 API 评测模式（需 TypeSafe 凭证）

有授权账号时，可对指定数据集进行真机调用。延迟由本地实测，Token 数直接读取 TypeSafe API 返回的 `usage.input_tokens/output_tokens`；成本基于实测 usage 与记录的单价计算，不再用字符数估算。该命令**只测 Jev**，不会把人工 gold label 伪装成 baseline；若要生成生产 GO，还必须提供单独录制的真实 baseline：

```bash
export TYPESAFE_API_KEY="your-api-key"
uv run python benchmarks/jev_calibration.py \
  --dataset benchmarks/jev_pairs_v1.json \
  --live-api \
  --output benchmarks/reports/calibration_live.json
```

---

## 🔒 校准产物校验与运行时保护

校准产物采用 `calibration-artifact-v1` Schema。在运行时加载校准产物（`load_calibration`）时，系统自动执行双重防呆验证：

1. **版本与模型匹配检查 (`CalibrationMismatchError`)**：
   - 验证产物中的 `model` 严格匹配当前运行的 `jev-1.13.0`；
   - recordings 在校准前必须携带与当前 Rubric 一致的 `rubric_hash`；运行时加载时再次比较 artifact 的 `rubric_version`、`rubric_hash` 与当前代码，并校验 active configuration 与 artifact 元数据/阈值完全一致。提示词内容改变但版本号忘记升级时也会 fail closed。
2. **资质与合格状态检查 (`CalibrationNotQualifiedError`)**：
   - 验证产物状态必须为 `GO` 且 `is_qualified_for_takeover` 为 `true`；
   - 验证 `active_configuration` 必须存在且有效；任何带有未达标原因的产物绝不被运行时载入。
