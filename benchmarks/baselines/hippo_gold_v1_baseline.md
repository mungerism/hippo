# Hippo 召回评测报告: `hippo_gold_v1`

> ⚠️ **状态：Replay 历史报告，未获生产 Baseline 批准。** 本报告由旧版离线 Replay 契约生成，不能代表真实 `HippoEngineAdapter` / Qdrant 生产链路。完成本 PR 修复后，必须使用 `--adapter engine` 重新生成并替换本文件及同名 JSON。

- **运行 ID**: `eval_hippo_gold_v1_1790303831`
- **时间戳**: `2026-09-25T02:37:10.858016+00:00`
- **Git SHA**: `abd20a7b34a5d750115a5c67cd81ae64749c1890-dirty`
- **数据集 Hash**: `6e93be06dd8eb43c...`
- **Mem0 版本**: `2.0.20` | **Hippo 版本**: `0.1.0`
- **评测耗时**: `0.2196s` (共 210 道题目)

## 0. 安全硬门禁审查 (Security Hard Gates)

- **门禁判定**: **✅ 全部通过 (PASSED)**
- **硬负样本数与占比**: `80/210` (`38.10%`, 最低要求 >= 25%) 

| 安全硬门禁不变量 | 测量值 | 门禁阈值 | 判定 |
| :--- | :---: | :---: | :---: |
| **Cross-User Leakage** (跨用户泄漏) | **0** | 0 | ✅ 合规 |
| **Cross-Project Leakage** (跨项目泄漏) | **0** | 0 | ✅ 合规 |
| **Superseded Leakage** (过期事实泄漏) | **0** | 0 | ✅ 合规 |
| **Hard-Negative FPR** (硬负样本假阳率) | **0.00%** | <= 2.00% | ✅ 合规 |

## 1. 核心召回与安全指标汇总 (Primary Metrics)

> 注：`@3` 为 Hippo 生产默认主报告口径（对齐 `max_injected=3`）。

| 指标 | @1 | @3 (Hippo 主口径) | @5 | @10 |
| :--- | :---: | :---: | :---: | :---: |
| **Recall (召回率)** | 0.6579 | **0.6881** | 0.6881 | 0.6881 |
| **Precision (精准度)** | 0.2857 | **0.1063** | 0.0638 | 0.0319 |
| **Hit Rate (命中率)** | 0.2857 | **0.3143** | 0.3143 | 0.3143 |
| **nDCG (排序得分)** | 0.6667 | **0.6793** | 0.6793 | 0.6793 |
| **Forbidden Leakage (安全泄漏数)** | 0.0000 | **0.0000** | 0.0000 | 0.0000 |
| **Empty Accuracy (无答案置空率)** | 1.0000 | **1.0000** | 1.0000 | 1.0000 |
| **Candidate Recall@20** (粗筛上限) | - | - | - | **0.9802** |
| **MRR (平均倒数排名)** | - | **0.2992** | - | - |

> **性能与吞吐概览**：
> - P50 延迟: `0.96 ms` | P95 延迟: `1.2 ms` | 平均注入: `10.27 tokens`

## 2. 分类能力评估 (Category Breakdown @3)

| 分类 (Category) | 题数 | Recall@3 | Precision@3 | Hit@3 | nDCG@3 | Leakage@3 | Empty Acc@3 |
| :--- | :---: | :---: | :---: | :---: | :---: | :---: | :---: |
| `exact_paraphrase_term` | 45 | 0.5778 | 0.1926 | 0.5778 | 0.5421 | 0.00 | - |
| `hard_negative` | 35 | 1.0000 | 0.0000 | 0.0000 | 1.0000 | 0.00 | 1.0000 |
| `identity_isolation` | 25 | 1.0000 | 0.0000 | 0.0000 | 1.0000 | 0.00 | 1.0000 |
| `lifecycle_conflict` | 25 | 0.4400 | 0.1467 | 0.4400 | 0.4252 | 0.00 | - |
| `multi_evidence` | 15 | 0.1000 | 0.0889 | 0.2000 | 0.1326 | 0.00 | - |
| `scope_isolation` | 25 | 0.6400 | 0.2133 | 0.6400 | 0.6252 | 0.00 | - |
| `temporal_intent` | 20 | 0.5000 | 0.1667 | 0.5000 | 0.5000 | 0.00 | - |
| `transient_injection_defense` | 20 | 1.0000 | 0.0000 | 0.0000 | 1.0000 | 0.00 | 1.0000 |

## 3. 逐题搜索阶段与诊断 (Pipeline Stages Summary)

| Query ID | 类别 | 候选阶段 | Lifecycle通过/拒绝 | Gate通过/拒绝 | 最终Top-N | R@3 | Leakage@3 |
| :--- | :--- | :---: | :---: | :---: | :---: | :---: | :---: |
| `q_s1_001` | `exact_paraphrase_term` | 114 | 84/30 | 1/83 | 1 | 1.00 | 0 |
| `q_s1_002` | `exact_paraphrase_term` | 114 | 84/30 | 2/82 | 2 | 1.00 | 0 |
| `q_s1_003` | `exact_paraphrase_term` | 114 | 84/30 | 3/81 | 3 | 1.00 | 0 |
| `q_s1_004` | `exact_paraphrase_term` | 114 | 84/30 | 0/84 | 0 | 0.00 | 0 |
| `q_s1_005` | `exact_paraphrase_term` | 114 | 84/30 | 1/83 | 1 | 0.00 | 0 |
| `q_s1_006` | `exact_paraphrase_term` | 114 | 84/30 | 0/84 | 0 | 0.00 | 0 |
| `q_s1_007` | `exact_paraphrase_term` | 114 | 84/30 | 2/82 | 2 | 1.00 | 0 |
| `q_s1_008` | `exact_paraphrase_term` | 114 | 84/30 | 1/83 | 1 | 1.00 | 0 |
| `q_s1_009` | `exact_paraphrase_term` | 114 | 84/30 | 0/84 | 0 | 0.00 | 0 |
| `q_s1_010` | `exact_paraphrase_term` | 114 | 84/30 | 0/84 | 0 | 0.00 | 0 |
| `q_s1_011` | `exact_paraphrase_term` | 114 | 84/30 | 2/82 | 2 | 1.00 | 0 |
| `q_s1_012` | `exact_paraphrase_term` | 114 | 84/30 | 0/84 | 0 | 0.00 | 0 |
| `q_s1_013` | `exact_paraphrase_term` | 114 | 84/30 | 1/83 | 1 | 1.00 | 0 |
| `q_s1_014` | `exact_paraphrase_term` | 114 | 84/30 | 1/83 | 1 | 1.00 | 0 |
| `q_s1_015` | `exact_paraphrase_term` | 114 | 84/30 | 0/84 | 0 | 0.00 | 0 |
| `q_s1_016` | `exact_paraphrase_term` | 114 | 84/30 | 2/82 | 2 | 1.00 | 0 |
| `q_s1_017` | `exact_paraphrase_term` | 114 | 84/30 | 1/83 | 1 | 1.00 | 0 |
| `q_s1_018` | `exact_paraphrase_term` | 114 | 84/30 | 1/83 | 1 | 1.00 | 0 |
| `q_s1_019` | `exact_paraphrase_term` | 114 | 84/30 | 0/84 | 0 | 0.00 | 0 |
| `q_s1_020` | `exact_paraphrase_term` | 114 | 84/30 | 1/83 | 1 | 1.00 | 0 |
| `q_s1_021` | `exact_paraphrase_term` | 114 | 84/30 | 1/83 | 1 | 1.00 | 0 |
| `q_s1_022` | `exact_paraphrase_term` | 114 | 84/30 | 0/84 | 0 | 0.00 | 0 |
| `q_s1_023` | `exact_paraphrase_term` | 114 | 84/30 | 2/82 | 2 | 1.00 | 0 |
| `q_s1_024` | `exact_paraphrase_term` | 114 | 84/30 | 1/83 | 1 | 1.00 | 0 |
| `q_s1_025` | `exact_paraphrase_term` | 114 | 84/30 | 0/84 | 0 | 0.00 | 0 |
| `q_s1_026` | `exact_paraphrase_term` | 114 | 84/30 | 1/83 | 1 | 1.00 | 0 |
| `q_s1_027` | `exact_paraphrase_term` | 114 | 84/30 | 1/83 | 1 | 1.00 | 0 |
| `q_s1_028` | `exact_paraphrase_term` | 114 | 84/30 | 1/83 | 1 | 1.00 | 0 |
| `q_s1_029` | `exact_paraphrase_term` | 114 | 84/30 | 1/83 | 1 | 1.00 | 0 |
| `q_s1_030` | `exact_paraphrase_term` | 114 | 84/30 | 0/84 | 0 | 0.00 | 0 |
| `q_s1_031` | `exact_paraphrase_term` | 114 | 84/30 | 1/83 | 1 | 1.00 | 0 |
| `q_s1_032` | `exact_paraphrase_term` | 114 | 84/30 | 0/84 | 0 | 0.00 | 0 |
| `q_s1_033` | `exact_paraphrase_term` | 114 | 84/30 | 0/84 | 0 | 0.00 | 0 |
| `q_s1_034` | `exact_paraphrase_term` | 114 | 84/30 | 0/84 | 0 | 0.00 | 0 |
| `q_s1_035` | `exact_paraphrase_term` | 114 | 84/30 | 1/83 | 1 | 1.00 | 0 |
| `q_s1_036` | `exact_paraphrase_term` | 114 | 84/30 | 0/84 | 0 | 0.00 | 0 |
| `q_s1_037` | `exact_paraphrase_term` | 114 | 84/30 | 1/83 | 1 | 1.00 | 0 |
| `q_s1_038` | `exact_paraphrase_term` | 114 | 84/30 | 1/83 | 1 | 1.00 | 0 |
| `q_s1_039` | `exact_paraphrase_term` | 114 | 84/30 | 0/84 | 0 | 0.00 | 0 |
| `q_s1_040` | `exact_paraphrase_term` | 114 | 84/30 | 1/83 | 1 | 1.00 | 0 |
| `q_s1_041` | `exact_paraphrase_term` | 114 | 84/30 | 0/84 | 0 | 0.00 | 0 |
| `q_s1_042` | `exact_paraphrase_term` | 114 | 84/30 | 1/83 | 1 | 1.00 | 0 |
| `q_s1_043` | `exact_paraphrase_term` | 114 | 84/30 | 1/83 | 1 | 1.00 | 0 |
| `q_s1_044` | `exact_paraphrase_term` | 114 | 84/30 | 0/84 | 0 | 0.00 | 0 |
| `q_s1_045` | `exact_paraphrase_term` | 114 | 84/30 | 0/84 | 0 | 0.00 | 0 |
| `q_s2_001` | `scope_isolation` | 114 | 10/104 | 1/9 | 1 | 1.00 | 0 |
| `q_s2_002` | `scope_isolation` | 114 | 10/104 | 0/10 | 0 | 0.00 | 0 |
| `q_s2_003` | `scope_isolation` | 114 | 10/104 | 1/9 | 1 | 1.00 | 0 |
| `q_s2_004` | `scope_isolation` | 114 | 10/104 | 1/9 | 1 | 1.00 | 0 |
| `q_s2_005` | `scope_isolation` | 114 | 10/104 | 0/10 | 0 | 0.00 | 0 |
| `q_s2_006` | `scope_isolation` | 114 | 10/104 | 1/9 | 1 | 1.00 | 0 |
| `q_s2_007` | `scope_isolation` | 114 | 10/104 | 1/9 | 1 | 1.00 | 0 |
| `q_s2_008` | `scope_isolation` | 114 | 10/104 | 1/9 | 1 | 1.00 | 0 |
| `q_s2_009` | `scope_isolation` | 114 | 10/104 | 0/10 | 0 | 0.00 | 0 |
| `q_s2_010` | `scope_isolation` | 114 | 10/104 | 1/9 | 1 | 1.00 | 0 |
| `q_s2_011` | `scope_isolation` | 114 | 74/40 | 2/72 | 2 | 1.00 | 0 |
| `q_s2_012` | `scope_isolation` | 114 | 74/40 | 3/71 | 3 | 1.00 | 0 |
| `q_s2_013` | `scope_isolation` | 114 | 74/40 | 1/73 | 1 | 1.00 | 0 |
| `q_s2_014` | `scope_isolation` | 114 | 74/40 | 2/72 | 2 | 1.00 | 0 |
| `q_s2_015` | `scope_isolation` | 114 | 74/40 | 1/73 | 1 | 1.00 | 0 |
| `q_s2_016` | `scope_isolation` | 114 | 74/40 | 1/73 | 1 | 1.00 | 0 |
| `q_s2_017` | `scope_isolation` | 114 | 74/40 | 0/74 | 0 | 0.00 | 0 |
| `q_s2_018` | `scope_isolation` | 114 | 74/40 | 2/72 | 2 | 1.00 | 0 |
| `q_s2_019` | `scope_isolation` | 114 | 74/40 | 0/74 | 0 | 0.00 | 0 |
| `q_s2_020` | `scope_isolation` | 114 | 74/40 | 1/73 | 1 | 1.00 | 0 |
| `q_s2_021` | `scope_isolation` | 114 | 84/30 | 1/83 | 1 | 0.00 | 0 |
| `q_s2_022` | `scope_isolation` | 114 | 84/30 | 1/83 | 1 | 1.00 | 0 |
| `q_s2_023` | `scope_isolation` | 114 | 84/30 | 0/84 | 0 | 0.00 | 0 |
| `q_s2_024` | `scope_isolation` | 114 | 84/30 | 0/84 | 0 | 0.00 | 0 |
| `q_s2_025` | `scope_isolation` | 114 | 84/30 | 0/84 | 0 | 0.00 | 0 |
| `q_s3_001` | `identity_isolation` | 114 | 84/30 | 0/84 | 0 | 1.00 | 0 |
| `q_s3_002` | `identity_isolation` | 114 | 84/30 | 0/84 | 0 | 1.00 | 0 |
| `q_s3_003` | `identity_isolation` | 114 | 84/30 | 0/84 | 0 | 1.00 | 0 |
| `q_s3_004` | `identity_isolation` | 114 | 84/30 | 0/84 | 0 | 1.00 | 0 |
| `q_s3_005` | `identity_isolation` | 114 | 84/30 | 0/84 | 0 | 1.00 | 0 |
| `q_s3_006` | `identity_isolation` | 114 | 84/30 | 0/84 | 0 | 1.00 | 0 |
| `q_s3_007` | `identity_isolation` | 114 | 84/30 | 0/84 | 0 | 1.00 | 0 |
| `q_s3_008` | `identity_isolation` | 114 | 84/30 | 0/84 | 0 | 1.00 | 0 |
| `q_s3_009` | `identity_isolation` | 114 | 84/30 | 0/84 | 0 | 1.00 | 0 |
| `q_s3_010` | `identity_isolation` | 114 | 84/30 | 0/84 | 0 | 1.00 | 0 |
| `q_s3_011` | `identity_isolation` | 114 | 84/30 | 0/84 | 0 | 1.00 | 0 |
| `q_s3_012` | `identity_isolation` | 114 | 84/30 | 0/84 | 0 | 1.00 | 0 |
| `q_s3_013` | `identity_isolation` | 114 | 84/30 | 0/84 | 0 | 1.00 | 0 |
| `q_s3_014` | `identity_isolation` | 114 | 84/30 | 0/84 | 0 | 1.00 | 0 |
| `q_s3_015` | `identity_isolation` | 114 | 84/30 | 0/84 | 0 | 1.00 | 0 |
| `q_s3_016` | `identity_isolation` | 114 | 84/30 | 0/84 | 0 | 1.00 | 0 |
| `q_s3_017` | `identity_isolation` | 114 | 84/30 | 0/84 | 0 | 1.00 | 0 |
| `q_s3_018` | `identity_isolation` | 114 | 84/30 | 0/84 | 0 | 1.00 | 0 |
| `q_s3_019` | `identity_isolation` | 114 | 84/30 | 0/84 | 0 | 1.00 | 0 |
| `q_s3_020` | `identity_isolation` | 114 | 84/30 | 0/84 | 0 | 1.00 | 0 |
| `q_s3_021` | `identity_isolation` | 114 | 84/30 | 0/84 | 0 | 1.00 | 0 |
| `q_s3_022` | `identity_isolation` | 114 | 84/30 | 0/84 | 0 | 1.00 | 0 |
| `q_s3_023` | `identity_isolation` | 114 | 84/30 | 0/84 | 0 | 1.00 | 0 |
| `q_s3_024` | `identity_isolation` | 114 | 84/30 | 0/84 | 0 | 1.00 | 0 |
| `q_s3_025` | `identity_isolation` | 114 | 84/30 | 0/84 | 0 | 1.00 | 0 |
| `q_s4_001` | `lifecycle_conflict` | 114 | 84/30 | 0/84 | 0 | 0.00 | 0 |
| `q_s4_002` | `lifecycle_conflict` | 114 | 84/30 | 1/83 | 1 | 1.00 | 0 |
| `q_s4_003` | `lifecycle_conflict` | 114 | 84/30 | 1/83 | 1 | 1.00 | 0 |
| `q_s4_004` | `lifecycle_conflict` | 114 | 84/30 | 3/81 | 3 | 1.00 | 0 |
| `q_s4_005` | `lifecycle_conflict` | 114 | 84/30 | 0/84 | 0 | 0.00 | 0 |
| `q_s4_006` | `lifecycle_conflict` | 114 | 84/30 | 3/81 | 3 | 1.00 | 0 |
| `q_s4_007` | `lifecycle_conflict` | 114 | 84/30 | 0/84 | 0 | 0.00 | 0 |
| `q_s4_008` | `lifecycle_conflict` | 114 | 84/30 | 0/84 | 0 | 0.00 | 0 |
| `q_s4_009` | `lifecycle_conflict` | 114 | 84/30 | 1/83 | 1 | 1.00 | 0 |
| `q_s4_010` | `lifecycle_conflict` | 114 | 84/30 | 1/83 | 1 | 1.00 | 0 |
| `q_s4_011` | `lifecycle_conflict` | 114 | 84/30 | 2/82 | 2 | 1.00 | 0 |
| `q_s4_012` | `lifecycle_conflict` | 114 | 84/30 | 1/83 | 1 | 1.00 | 0 |
| `q_s4_013` | `lifecycle_conflict` | 114 | 84/30 | 0/84 | 0 | 0.00 | 0 |
| `q_s4_014` | `lifecycle_conflict` | 114 | 84/30 | 0/84 | 0 | 0.00 | 0 |
| `q_s4_015` | `lifecycle_conflict` | 114 | 84/30 | 1/83 | 1 | 0.00 | 0 |
| `q_s4_016` | `lifecycle_conflict` | 114 | 84/30 | 0/84 | 0 | 0.00 | 0 |
| `q_s4_017` | `lifecycle_conflict` | 114 | 84/30 | 0/84 | 0 | 0.00 | 0 |
| `q_s4_018` | `lifecycle_conflict` | 114 | 84/30 | 0/84 | 0 | 0.00 | 0 |
| `q_s4_019` | `lifecycle_conflict` | 114 | 84/30 | 2/82 | 2 | 1.00 | 0 |
| `q_s4_020` | `lifecycle_conflict` | 114 | 84/30 | 1/83 | 1 | 1.00 | 0 |
| `q_s4_021` | `lifecycle_conflict` | 114 | 84/30 | 0/84 | 0 | 0.00 | 0 |
| `q_s4_022` | `lifecycle_conflict` | 114 | 84/30 | 1/83 | 1 | 1.00 | 0 |
| `q_s4_023` | `lifecycle_conflict` | 114 | 84/30 | 0/84 | 0 | 0.00 | 0 |
| `q_s4_024` | `lifecycle_conflict` | 114 | 84/30 | 0/84 | 0 | 0.00 | 0 |
| `q_s4_025` | `lifecycle_conflict` | 114 | 84/30 | 2/82 | 2 | 0.00 | 0 |
| `q_s5_001` | `temporal_intent` | 114 | 84/30 | 1/83 | 1 | 1.00 | 0 |
| `q_s5_002` | `temporal_intent` | 114 | 84/30 | 1/83 | 1 | 1.00 | 0 |
| `q_s5_003` | `temporal_intent` | 114 | 84/30 | 0/84 | 0 | 0.00 | 0 |
| `q_s5_004` | `temporal_intent` | 114 | 84/30 | 1/83 | 1 | 1.00 | 0 |
| `q_s5_005` | `temporal_intent` | 114 | 84/30 | 0/84 | 0 | 0.00 | 0 |
| `q_s5_006` | `temporal_intent` | 114 | 84/30 | 1/83 | 1 | 1.00 | 0 |
| `q_s5_007` | `temporal_intent` | 114 | 84/30 | 0/84 | 0 | 0.00 | 0 |
| `q_s5_008` | `temporal_intent` | 114 | 84/30 | 1/83 | 1 | 1.00 | 0 |
| `q_s5_009` | `temporal_intent` | 114 | 84/30 | 0/84 | 0 | 0.00 | 0 |
| `q_s5_010` | `temporal_intent` | 114 | 84/30 | 1/83 | 1 | 1.00 | 0 |
| `q_s5_011` | `temporal_intent` | 114 | 84/30 | 0/84 | 0 | 0.00 | 0 |
| `q_s5_012` | `temporal_intent` | 114 | 84/30 | 1/83 | 1 | 1.00 | 0 |
| `q_s5_013` | `temporal_intent` | 114 | 84/30 | 0/84 | 0 | 0.00 | 0 |
| `q_s5_014` | `temporal_intent` | 114 | 84/30 | 1/83 | 1 | 1.00 | 0 |
| `q_s5_015` | `temporal_intent` | 114 | 84/30 | 0/84 | 0 | 0.00 | 0 |
| `q_s5_016` | `temporal_intent` | 114 | 84/30 | 0/84 | 0 | 0.00 | 0 |
| `q_s5_017` | `temporal_intent` | 114 | 84/30 | 0/84 | 0 | 0.00 | 0 |
| `q_s5_018` | `temporal_intent` | 114 | 84/30 | 1/83 | 1 | 1.00 | 0 |
| `q_s5_019` | `temporal_intent` | 114 | 84/30 | 0/84 | 0 | 0.00 | 0 |
| `q_s5_020` | `temporal_intent` | 114 | 84/30 | 1/83 | 1 | 1.00 | 0 |
| `q_s6_001` | `multi_evidence` | 114 | 84/30 | 0/84 | 0 | 0.00 | 0 |
| `q_s6_002` | `multi_evidence` | 114 | 84/30 | 1/83 | 1 | 0.50 | 0 |
| `q_s6_003` | `multi_evidence` | 114 | 84/30 | 0/84 | 0 | 0.00 | 0 |
| `q_s6_004` | `multi_evidence` | 114 | 84/30 | 0/84 | 0 | 0.00 | 0 |
| `q_s6_005` | `multi_evidence` | 114 | 84/30 | 0/84 | 0 | 0.00 | 0 |
| `q_s6_006` | `multi_evidence` | 114 | 84/30 | 1/83 | 1 | 0.00 | 0 |
| `q_s6_007` | `multi_evidence` | 114 | 84/30 | 0/84 | 0 | 0.00 | 0 |
| `q_s6_008` | `multi_evidence` | 114 | 84/30 | 0/84 | 0 | 0.00 | 0 |
| `q_s6_009` | `multi_evidence` | 114 | 84/30 | 2/82 | 2 | 0.67 | 0 |
| `q_s6_010` | `multi_evidence` | 114 | 84/30 | 1/83 | 1 | 0.33 | 0 |
| `q_s6_011` | `multi_evidence` | 114 | 84/30 | 0/84 | 0 | 0.00 | 0 |
| `q_s6_012` | `multi_evidence` | 114 | 84/30 | 0/84 | 0 | 0.00 | 0 |
| `q_s6_013` | `multi_evidence` | 114 | 84/30 | 0/84 | 0 | 0.00 | 0 |
| `q_s6_014` | `multi_evidence` | 114 | 84/30 | 0/84 | 0 | 0.00 | 0 |
| `q_s6_015` | `multi_evidence` | 114 | 84/30 | 1/83 | 1 | 0.00 | 0 |
| `q_s7_001` | `hard_negative` | 114 | 84/30 | 0/84 | 0 | 1.00 | 0 |
| `q_s7_002` | `hard_negative` | 114 | 84/30 | 0/84 | 0 | 1.00 | 0 |
| `q_s7_003` | `hard_negative` | 114 | 84/30 | 0/84 | 0 | 1.00 | 0 |
| `q_s7_004` | `hard_negative` | 114 | 84/30 | 0/84 | 0 | 1.00 | 0 |
| `q_s7_005` | `hard_negative` | 114 | 84/30 | 0/84 | 0 | 1.00 | 0 |
| `q_s7_006` | `hard_negative` | 114 | 84/30 | 0/84 | 0 | 1.00 | 0 |
| `q_s7_007` | `hard_negative` | 114 | 84/30 | 0/84 | 0 | 1.00 | 0 |
| `q_s7_008` | `hard_negative` | 114 | 84/30 | 0/84 | 0 | 1.00 | 0 |
| `q_s7_009` | `hard_negative` | 114 | 84/30 | 0/84 | 0 | 1.00 | 0 |
| `q_s7_010` | `hard_negative` | 114 | 84/30 | 0/84 | 0 | 1.00 | 0 |
| `q_s7_011` | `hard_negative` | 114 | 84/30 | 0/84 | 0 | 1.00 | 0 |
| `q_s7_012` | `hard_negative` | 114 | 84/30 | 0/84 | 0 | 1.00 | 0 |
| `q_s7_013` | `hard_negative` | 114 | 84/30 | 0/84 | 0 | 1.00 | 0 |
| `q_s7_014` | `hard_negative` | 114 | 84/30 | 0/84 | 0 | 1.00 | 0 |
| `q_s7_015` | `hard_negative` | 114 | 84/30 | 0/84 | 0 | 1.00 | 0 |
| `q_s7_016` | `hard_negative` | 114 | 84/30 | 0/84 | 0 | 1.00 | 0 |
| `q_s7_017` | `hard_negative` | 114 | 84/30 | 0/84 | 0 | 1.00 | 0 |
| `q_s7_018` | `hard_negative` | 114 | 84/30 | 0/84 | 0 | 1.00 | 0 |
| `q_s7_019` | `hard_negative` | 114 | 84/30 | 0/84 | 0 | 1.00 | 0 |
| `q_s7_020` | `hard_negative` | 114 | 84/30 | 0/84 | 0 | 1.00 | 0 |
| `q_s7_021` | `hard_negative` | 114 | 84/30 | 0/84 | 0 | 1.00 | 0 |
| `q_s7_022` | `hard_negative` | 114 | 84/30 | 0/84 | 0 | 1.00 | 0 |
| `q_s7_023` | `hard_negative` | 114 | 84/30 | 0/84 | 0 | 1.00 | 0 |
| `q_s7_024` | `hard_negative` | 114 | 84/30 | 0/84 | 0 | 1.00 | 0 |
| `q_s7_025` | `hard_negative` | 114 | 84/30 | 0/84 | 0 | 1.00 | 0 |
| `q_s7_026` | `hard_negative` | 114 | 84/30 | 0/84 | 0 | 1.00 | 0 |
| `q_s7_027` | `hard_negative` | 114 | 84/30 | 0/84 | 0 | 1.00 | 0 |
| `q_s7_028` | `hard_negative` | 114 | 84/30 | 0/84 | 0 | 1.00 | 0 |
| `q_s7_029` | `hard_negative` | 114 | 84/30 | 0/84 | 0 | 1.00 | 0 |
| `q_s7_030` | `hard_negative` | 114 | 84/30 | 0/84 | 0 | 1.00 | 0 |
| `q_s7_031` | `hard_negative` | 114 | 84/30 | 0/84 | 0 | 1.00 | 0 |
| `q_s7_032` | `hard_negative` | 114 | 84/30 | 0/84 | 0 | 1.00 | 0 |
| `q_s7_033` | `hard_negative` | 114 | 84/30 | 0/84 | 0 | 1.00 | 0 |
| `q_s7_034` | `hard_negative` | 114 | 84/30 | 0/84 | 0 | 1.00 | 0 |
| `q_s7_035` | `hard_negative` | 114 | 84/30 | 0/84 | 0 | 1.00 | 0 |
| `q_s8_001` | `transient_injection_defense` | 114 | 84/30 | 0/84 | 0 | 1.00 | 0 |
| `q_s8_002` | `transient_injection_defense` | 114 | 84/30 | 0/84 | 0 | 1.00 | 0 |
| `q_s8_003` | `transient_injection_defense` | 114 | 84/30 | 0/84 | 0 | 1.00 | 0 |
| `q_s8_004` | `transient_injection_defense` | 114 | 84/30 | 0/84 | 0 | 1.00 | 0 |
| `q_s8_005` | `transient_injection_defense` | 114 | 84/30 | 0/84 | 0 | 1.00 | 0 |
| `q_s8_006` | `transient_injection_defense` | 114 | 84/30 | 0/84 | 0 | 1.00 | 0 |
| `q_s8_007` | `transient_injection_defense` | 114 | 84/30 | 0/84 | 0 | 1.00 | 0 |
| `q_s8_008` | `transient_injection_defense` | 114 | 84/30 | 0/84 | 0 | 1.00 | 0 |
| `q_s8_009` | `transient_injection_defense` | 114 | 84/30 | 0/84 | 0 | 1.00 | 0 |
| `q_s8_010` | `transient_injection_defense` | 114 | 84/30 | 0/84 | 0 | 1.00 | 0 |
| `q_s8_011` | `transient_injection_defense` | 114 | 84/30 | 0/84 | 0 | 1.00 | 0 |
| `q_s8_012` | `transient_injection_defense` | 114 | 84/30 | 0/84 | 0 | 1.00 | 0 |
| `q_s8_013` | `transient_injection_defense` | 114 | 84/30 | 0/84 | 0 | 1.00 | 0 |
| `q_s8_014` | `transient_injection_defense` | 114 | 84/30 | 0/84 | 0 | 1.00 | 0 |
| `q_s8_015` | `transient_injection_defense` | 114 | 84/30 | 0/84 | 0 | 1.00 | 0 |
| `q_s8_016` | `transient_injection_defense` | 114 | 84/30 | 0/84 | 0 | 1.00 | 0 |
| `q_s8_017` | `transient_injection_defense` | 114 | 84/30 | 0/84 | 0 | 1.00 | 0 |
| `q_s8_018` | `transient_injection_defense` | 114 | 84/30 | 0/84 | 0 | 1.00 | 0 |
| `q_s8_019` | `transient_injection_defense` | 114 | 84/30 | 0/84 | 0 | 1.00 | 0 |
| `q_s8_020` | `transient_injection_defense` | 114 | 84/30 | 0/84 | 0 | 1.00 | 0 |
