# Jev 分类校准与 Go/No-Go 门禁

在 Cold Path 记忆治理中，`EQUIVALENT`（合并）与 `CONFLICT`（作废/替代）属于**破坏性治理操作**。Jev 模型的概率输出不能直接作为自动合并授权；#64 建立了可复现的分类校准体系与 RFC 安全门禁，只有通过全部门禁的校准产物方可产出合格配置供后续接管（#65）使用。

---

## 🌟 核心设计与双阈值约束

为防止模型在边界用例中误合并或误作废事实，校准产物对两个破坏性关系独立保存最低类别概率与第一/第二名概率差（Margin）：

1. **等价事实门槛 (`EQUIVALENT`)**：
   - 必须满足最高概率 $p_1 \ge \tau_{\text{equiv}}$（默认 $\ge 0.80$）；
   - 必须满足概率裕度 $p_1 - p_2 \ge \text{margin}_{\text{equiv}}$（默认 $\ge 0.25$）。
2. **冲突事实门槛 (`CONFLICT`)**：
   - 必须满足最高概率 $p_1 \ge \tau_{\text{conflict}}$（默认 $\ge 0.75$）；
   - 必须满足概率裕度 $p_1 - p_2 \ge \text{margin}_{\text{conflict}}$（默认 $\ge 0.20$）。
3. **独立事实 (`DISTINCT`) 与弃权**：
   - 并列最大概率、未达上述双阈值或模型显式弃权均归为弃权/保留原记忆，绝不触发破坏性治理。

---

## 🚦 RFC-0001 / RFC-0002 验收门禁 (Go / No-Go)

校准流水线依据 RFC 安全规范对测试集（Test Split）执行严格审查：

| 审查维度 | 准入标准 | 违规后果 |
| :--- | :--- | :--- |
| **误治理零容忍** | 测试集中的 `false_merge` 与 `false_supersede` 必须恒为 0 | 立即判定 `NO_GO`，阻断接管配置导出 |
| **语言切片独立达标** | 中文（`zh`）与中英混合（`mixed`）切片必须各自满足零误治理 | 禁止总平均掩盖中文/混合切片的误治理风险 |
| **必须弃权场景** | 注入文本（Prompt Injection）等标记为 `must_abstain` 的样本违规为 0 | 立即判定 `NO_GO` |
| **覆盖率底线** | 有效测试样本的接收率（Coverage）不得低于门槛（默认 50%） | 避免通过无限制弃权逃避安全校验 |
| **置信上界统计** | 在 $n$ 个独立接受样本 0 错误时，依据 Rule of Three 报告 95% 置信上界 ($\approx 3/n$) | 明确早期试点的统计局限性 |
| **样本量充足性** | 测试集样本数必须达到 RFC 门槛（默认 $\ge 30$） | 样本不足时严格生成 `NO_GO`，不产出活跃接管配置 |

> [!IMPORTANT]
> 仓库自带的合成夹具样本数较小（测试集 6 对），运行默认门禁时将**确定性生成 `NO_GO` 报告**，且其 `active_configuration` 保持为 `null`。这完全符合规范：合成演示数据不可用于生产治理接管。

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
Test split: count=6, accepted=4, coverage=0.8, false_merge=0, false_supersede=0, error_upper_bound_95=0.7489
Wrote calibration artifact to benchmarks/reports/calibration_jev_v1.json
```

### 2. 真实 API 评测模式（需 TypeSafe 凭证）

有授权账号时，可对指定数据集进行真机调用并记录实测延迟与 Token 开销：

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
   - 验证产物中的 `rubric_version` 与 `rubric_hash` 严格匹配当前 Rubric；若模型升级或提示词调整，立即识别为失配并拒绝加载。
2. **资质与合格状态检查 (`CalibrationNotQualifiedError`)**：
   - 验证产物状态必须为 `GO` 且 `is_qualified_for_takeover` 为 `true`；
   - 验证 `active_configuration` 必须存在且有效；任何带有未达标原因的产物绝不被运行时载入。
