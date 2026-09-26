# Hippo 召回评测报告: `hippo_gold_v1`

- **运行 ID**: `eval_hippo_gold_v1_1790411628`
- **时间戳**: `2026-09-26T08:31:15.821766+00:00`
- **Git SHA**: `d2ae2b9b0f0d7bfcb82418d972433c92150fb574`
- **数据集 Hash**: `24bec7ba639f9058...`
- **Mem0 版本**: `2.0.20` | **Hippo 版本**: `0.1.0`
- **Adapter / Ingest**: `HippoEngineAdapter` / `direct-facts`
- **评测耗时**: `152.6881s` (共 210 道题目)
- **主检索口径 / Persistence 诊断**: `200 / 10`

## 0. 安全硬门禁审查 (Security Hard Gates)

- **门禁判定**: **✅ 全部通过 (PASSED)**
- **检索硬负样本数与占比**: `70/200` (`35.00%`, 最低要求 >= 25%) 

| 安全硬门禁不变量 | 测量值 | 门禁阈值 | 判定 |
| :--- | :---: | :---: | :---: |
| **Cross-User Leakage** (跨用户泄漏) | **0** | 0 | ✅ 合规 |
| **Cross-Project Leakage** (跨项目泄漏) | **0** | 0 | ✅ 合规 |
| **Superseded Leakage** (过期事实泄漏) | **0** | 0 | ✅ 合规 |
| **Hard-Negative FPR** (硬负样本假阳率) | **1.43%** | <= 2.00% | ✅ 合规 |

## 1. 核心召回与安全指标汇总 (Primary Metrics)

> 注：`@3` 为 Hippo 生产默认主报告口径（对齐 `max_injected=3`）。

| 指标 | @1 | @3 (Hippo 主口径) | @5 | @10 |
| :--- | :---: | :---: | :---: | :---: |
| **Recall (召回率)** | 0.6288 | **0.6891** | 0.6891 | 0.6891 |
| **Precision (精准度)** | 0.6615 | **0.2462** | 0.1477 | 0.0738 |
| **Hit Rate (命中率)** | 0.6615 | **0.7154** | 0.7154 | 0.7154 |
| **nDCG (排序得分)** | 0.6615 | **0.6800** | 0.6787 | 0.6787 |
| **Forbidden Leakage (安全泄漏数)** | 0.0000 | **0.0000** | 0.0000 | 0.0000 |
| **Empty Accuracy (无答案置空率)** | 0.9857 | **0.9857** | 0.9857 | 0.9857 |
| **Candidate Recall@20** (粗筛上限) | - | - | - | **0.9974** |
| **MRR (平均倒数排名)** | - | **0.6885** | - | - |

> **性能与吞吐概览**：
> - P50 延迟: `399.64 ms` | P95 延迟: `552.51 ms` | 平均注入: `20.73 tokens`
> - 索引体积: `unavailable`（当前向量后端未暴露可复现的磁盘字节数）

## 2. 分类能力评估 (Category Breakdown @3)

| 分类 (Category) | 题数 | Recall@3 | Precision@3 | Hit@3 | nDCG@3 | Leakage@3 | Empty Acc@3 |
| :--- | :---: | :---: | :---: | :---: | :---: | :---: | :---: |
| `exact_paraphrase_term` | 45 | 0.8667 | 0.2889 | 0.8667 | 0.8421 | 0.00 | - |
| `hard_negative` | 35 | 0.0000 | 0.0000 | 0.0000 | 0.0000 | 0.00 | 1.0000 |
| `identity_isolation` | 25 | 0.0000 | 0.0000 | 0.0000 | 0.0000 | 0.00 | 0.9600 |
| `lifecycle_conflict` | 25 | 0.4800 | 0.1600 | 0.4800 | 0.4652 | 0.00 | - |
| `multi_evidence` | 15 | 0.2389 | 0.2222 | 0.4667 | 0.3324 | 0.00 | - |
| `persistence_quality` | 10 | 0.0000 | 0.0000 | 0.0000 | 0.0000 | 0.00 | 0.1000 |
| `retrieval_safety` | 10 | 0.0000 | 0.0000 | 0.0000 | 0.0000 | 0.00 | 1.0000 |
| `scope_isolation` | 25 | 0.7200 | 0.2400 | 0.7200 | 0.6905 | 0.00 | - |
| `temporal_intent` | 20 | 0.8500 | 0.2833 | 0.8500 | 0.8315 | 0.00 | - |

## 3. 逐题搜索阶段与诊断 (Pipeline Stages Summary)

| Query ID | 类别 | 候选阶段 | Lifecycle通过/拒绝 | Gate通过/拒绝 | 最终Top-N | R@3 | Leakage@3 |
| :--- | :--- | :---: | :---: | :---: | :---: | :---: | :---: |
| `q_s1_001` | `exact_paraphrase_term` | 20 | 20/0 | 2/18 | 2 | 1.00 | 0 |
| `q_s1_002` | `exact_paraphrase_term` | 20 | 20/0 | 2/18 | 2 | 1.00 | 0 |
| `q_s1_003` | `exact_paraphrase_term` | 20 | 20/0 | 3/17 | 3 | 1.00 | 0 |
| `q_s1_004` | `exact_paraphrase_term` | 20 | 20/0 | 2/18 | 2 | 1.00 | 0 |
| `q_s1_005` | `exact_paraphrase_term` | 20 | 20/0 | 1/19 | 1 | 0.00 | 0 |
| `q_s1_006` | `exact_paraphrase_term` | 20 | 20/0 | 3/17 | 3 | 1.00 | 0 |
| `q_s1_007` | `exact_paraphrase_term` | 20 | 20/0 | 2/18 | 2 | 1.00 | 0 |
| `q_s1_008` | `exact_paraphrase_term` | 20 | 20/0 | 1/19 | 1 | 1.00 | 0 |
| `q_s1_009` | `exact_paraphrase_term` | 20 | 20/0 | 3/17 | 3 | 1.00 | 0 |
| `q_s1_010` | `exact_paraphrase_term` | 20 | 20/0 | 0/20 | 0 | 0.00 | 0 |
| `q_s1_011` | `exact_paraphrase_term` | 20 | 20/0 | 2/18 | 2 | 1.00 | 0 |
| `q_s1_012` | `exact_paraphrase_term` | 20 | 20/0 | 3/17 | 3 | 1.00 | 0 |
| `q_s1_013` | `exact_paraphrase_term` | 20 | 20/0 | 3/17 | 3 | 1.00 | 0 |
| `q_s1_014` | `exact_paraphrase_term` | 20 | 20/0 | 1/19 | 1 | 1.00 | 0 |
| `q_s1_015` | `exact_paraphrase_term` | 20 | 20/0 | 3/17 | 3 | 1.00 | 0 |
| `q_s1_016` | `exact_paraphrase_term` | 20 | 20/0 | 2/18 | 2 | 1.00 | 0 |
| `q_s1_017` | `exact_paraphrase_term` | 20 | 20/0 | 3/17 | 3 | 1.00 | 0 |
| `q_s1_018` | `exact_paraphrase_term` | 20 | 20/0 | 0/20 | 0 | 0.00 | 0 |
| `q_s1_019` | `exact_paraphrase_term` | 20 | 20/0 | 0/20 | 0 | 0.00 | 0 |
| `q_s1_020` | `exact_paraphrase_term` | 20 | 20/0 | 1/19 | 1 | 1.00 | 0 |
| `q_s1_021` | `exact_paraphrase_term` | 20 | 20/0 | 1/19 | 1 | 1.00 | 0 |
| `q_s1_022` | `exact_paraphrase_term` | 20 | 20/0 | 2/18 | 2 | 1.00 | 0 |
| `q_s1_023` | `exact_paraphrase_term` | 20 | 20/0 | 3/17 | 3 | 1.00 | 0 |
| `q_s1_024` | `exact_paraphrase_term` | 20 | 20/0 | 1/19 | 1 | 1.00 | 0 |
| `q_s1_025` | `exact_paraphrase_term` | 20 | 20/0 | 1/19 | 1 | 1.00 | 0 |
| `q_s1_026` | `exact_paraphrase_term` | 20 | 20/0 | 1/19 | 1 | 1.00 | 0 |
| `q_s1_027` | `exact_paraphrase_term` | 20 | 20/0 | 3/17 | 3 | 1.00 | 0 |
| `q_s1_028` | `exact_paraphrase_term` | 20 | 20/0 | 1/19 | 1 | 1.00 | 0 |
| `q_s1_029` | `exact_paraphrase_term` | 20 | 20/0 | 1/19 | 1 | 1.00 | 0 |
| `q_s1_030` | `exact_paraphrase_term` | 20 | 20/0 | 3/17 | 3 | 1.00 | 0 |
| `q_s1_031` | `exact_paraphrase_term` | 20 | 20/0 | 1/19 | 1 | 1.00 | 0 |
| `q_s1_032` | `exact_paraphrase_term` | 20 | 20/0 | 0/20 | 0 | 0.00 | 0 |
| `q_s1_033` | `exact_paraphrase_term` | 20 | 20/0 | 2/18 | 2 | 1.00 | 0 |
| `q_s1_034` | `exact_paraphrase_term` | 20 | 20/0 | 1/19 | 1 | 1.00 | 0 |
| `q_s1_035` | `exact_paraphrase_term` | 20 | 20/0 | 1/19 | 1 | 1.00 | 0 |
| `q_s1_036` | `exact_paraphrase_term` | 20 | 20/0 | 1/19 | 1 | 1.00 | 0 |
| `q_s1_037` | `exact_paraphrase_term` | 20 | 20/0 | 1/19 | 1 | 1.00 | 0 |
| `q_s1_038` | `exact_paraphrase_term` | 20 | 20/0 | 1/19 | 1 | 1.00 | 0 |
| `q_s1_039` | `exact_paraphrase_term` | 20 | 20/0 | 1/19 | 1 | 1.00 | 0 |
| `q_s1_040` | `exact_paraphrase_term` | 20 | 20/0 | 1/19 | 1 | 1.00 | 0 |
| `q_s1_041` | `exact_paraphrase_term` | 20 | 20/0 | 3/17 | 3 | 1.00 | 0 |
| `q_s1_042` | `exact_paraphrase_term` | 20 | 20/0 | 1/19 | 1 | 1.00 | 0 |
| `q_s1_043` | `exact_paraphrase_term` | 20 | 20/0 | 1/19 | 1 | 1.00 | 0 |
| `q_s1_044` | `exact_paraphrase_term` | 20 | 20/0 | 0/20 | 0 | 0.00 | 0 |
| `q_s1_045` | `exact_paraphrase_term` | 20 | 20/0 | 2/18 | 2 | 1.00 | 0 |
| `q_s2_001` | `scope_isolation` | 10 | 10/0 | 1/9 | 1 | 1.00 | 0 |
| `q_s2_002` | `scope_isolation` | 10 | 10/0 | 1/9 | 1 | 1.00 | 0 |
| `q_s2_003` | `scope_isolation` | 10 | 10/0 | 1/9 | 1 | 1.00 | 0 |
| `q_s2_004` | `scope_isolation` | 10 | 10/0 | 2/8 | 2 | 1.00 | 0 |
| `q_s2_005` | `scope_isolation` | 10 | 10/0 | 0/10 | 0 | 0.00 | 0 |
| `q_s2_006` | `scope_isolation` | 10 | 10/0 | 1/9 | 1 | 1.00 | 0 |
| `q_s2_007` | `scope_isolation` | 10 | 10/0 | 1/9 | 1 | 1.00 | 0 |
| `q_s2_008` | `scope_isolation` | 10 | 10/0 | 2/8 | 2 | 1.00 | 0 |
| `q_s2_009` | `scope_isolation` | 10 | 10/0 | 0/10 | 0 | 0.00 | 0 |
| `q_s2_010` | `scope_isolation` | 10 | 10/0 | 1/9 | 1 | 1.00 | 0 |
| `q_s2_011` | `scope_isolation` | 20 | 20/0 | 2/18 | 2 | 1.00 | 0 |
| `q_s2_012` | `scope_isolation` | 20 | 20/0 | 3/17 | 3 | 1.00 | 0 |
| `q_s2_013` | `scope_isolation` | 20 | 20/0 | 1/19 | 1 | 1.00 | 0 |
| `q_s2_014` | `scope_isolation` | 20 | 20/0 | 2/18 | 2 | 1.00 | 0 |
| `q_s2_015` | `scope_isolation` | 20 | 20/0 | 2/18 | 2 | 0.00 | 0 |
| `q_s2_016` | `scope_isolation` | 20 | 20/0 | 1/19 | 1 | 1.00 | 0 |
| `q_s2_017` | `scope_isolation` | 20 | 20/0 | 1/19 | 1 | 1.00 | 0 |
| `q_s2_018` | `scope_isolation` | 20 | 20/0 | 2/18 | 2 | 1.00 | 0 |
| `q_s2_019` | `scope_isolation` | 20 | 20/0 | 3/17 | 3 | 1.00 | 0 |
| `q_s2_020` | `scope_isolation` | 20 | 20/0 | 2/18 | 2 | 1.00 | 0 |
| `q_s2_021` | `scope_isolation` | 20 | 20/0 | 3/17 | 3 | 0.00 | 0 |
| `q_s2_022` | `scope_isolation` | 20 | 20/0 | 1/19 | 1 | 1.00 | 0 |
| `q_s2_023` | `scope_isolation` | 20 | 20/0 | 0/20 | 0 | 0.00 | 0 |
| `q_s2_024` | `scope_isolation` | 20 | 20/0 | 0/20 | 0 | 0.00 | 0 |
| `q_s2_025` | `scope_isolation` | 20 | 20/0 | 3/17 | 3 | 0.00 | 0 |
| `q_s3_001` | `identity_isolation` | 20 | 20/0 | 0/20 | 0 | 1.00 | 0 |
| `q_s3_002` | `identity_isolation` | 20 | 20/0 | 0/20 | 0 | 1.00 | 0 |
| `q_s3_003` | `identity_isolation` | 20 | 20/0 | 0/20 | 0 | 1.00 | 0 |
| `q_s3_004` | `identity_isolation` | 20 | 20/0 | 0/20 | 0 | 1.00 | 0 |
| `q_s3_005` | `identity_isolation` | 20 | 20/0 | 0/20 | 0 | 1.00 | 0 |
| `q_s3_006` | `identity_isolation` | 20 | 20/0 | 0/20 | 0 | 1.00 | 0 |
| `q_s3_007` | `identity_isolation` | 20 | 20/0 | 0/20 | 0 | 1.00 | 0 |
| `q_s3_008` | `identity_isolation` | 20 | 20/0 | 1/19 | 1 | 0.00 | 0 |
| `q_s3_009` | `identity_isolation` | 20 | 20/0 | 0/20 | 0 | 1.00 | 0 |
| `q_s3_010` | `identity_isolation` | 20 | 20/0 | 0/20 | 0 | 1.00 | 0 |
| `q_s3_011` | `identity_isolation` | 20 | 20/0 | 0/20 | 0 | 1.00 | 0 |
| `q_s3_012` | `identity_isolation` | 20 | 20/0 | 0/20 | 0 | 1.00 | 0 |
| `q_s3_013` | `identity_isolation` | 20 | 20/0 | 0/20 | 0 | 1.00 | 0 |
| `q_s3_014` | `identity_isolation` | 20 | 20/0 | 0/20 | 0 | 1.00 | 0 |
| `q_s3_015` | `identity_isolation` | 20 | 20/0 | 0/20 | 0 | 1.00 | 0 |
| `q_s3_016` | `identity_isolation` | 20 | 20/0 | 0/20 | 0 | 1.00 | 0 |
| `q_s3_017` | `identity_isolation` | 20 | 20/0 | 0/20 | 0 | 1.00 | 0 |
| `q_s3_018` | `identity_isolation` | 20 | 20/0 | 0/20 | 0 | 1.00 | 0 |
| `q_s3_019` | `identity_isolation` | 20 | 20/0 | 0/20 | 0 | 1.00 | 0 |
| `q_s3_020` | `identity_isolation` | 20 | 20/0 | 0/20 | 0 | 1.00 | 0 |
| `q_s3_021` | `identity_isolation` | 20 | 20/0 | 0/20 | 0 | 1.00 | 0 |
| `q_s3_022` | `identity_isolation` | 20 | 20/0 | 0/20 | 0 | 1.00 | 0 |
| `q_s3_023` | `identity_isolation` | 20 | 20/0 | 0/20 | 0 | 1.00 | 0 |
| `q_s3_024` | `identity_isolation` | 20 | 20/0 | 0/20 | 0 | 1.00 | 0 |
| `q_s3_025` | `identity_isolation` | 20 | 20/0 | 0/20 | 0 | 1.00 | 0 |
| `q_s4_001` | `lifecycle_conflict` | 20 | 20/0 | 1/19 | 1 | 1.00 | 0 |
| `q_s4_002` | `lifecycle_conflict` | 20 | 20/0 | 3/17 | 3 | 1.00 | 0 |
| `q_s4_003` | `lifecycle_conflict` | 20 | 20/0 | 2/18 | 2 | 1.00 | 0 |
| `q_s4_004` | `lifecycle_conflict` | 20 | 20/0 | 3/17 | 3 | 1.00 | 0 |
| `q_s4_005` | `lifecycle_conflict` | 20 | 20/0 | 0/20 | 0 | 0.00 | 0 |
| `q_s4_006` | `lifecycle_conflict` | 20 | 20/0 | 3/17 | 3 | 1.00 | 0 |
| `q_s4_007` | `lifecycle_conflict` | 20 | 20/0 | 3/17 | 3 | 1.00 | 0 |
| `q_s4_008` | `lifecycle_conflict` | 20 | 20/0 | 0/20 | 0 | 0.00 | 0 |
| `q_s4_009` | `lifecycle_conflict` | 20 | 20/0 | 1/19 | 1 | 1.00 | 0 |
| `q_s4_010` | `lifecycle_conflict` | 20 | 20/0 | 2/18 | 2 | 1.00 | 0 |
| `q_s4_011` | `lifecycle_conflict` | 20 | 20/0 | 2/18 | 2 | 1.00 | 0 |
| `q_s4_012` | `lifecycle_conflict` | 20 | 20/0 | 2/18 | 2 | 1.00 | 0 |
| `q_s4_013` | `lifecycle_conflict` | 20 | 20/0 | 0/20 | 0 | 0.00 | 0 |
| `q_s4_014` | `lifecycle_conflict` | 20 | 20/0 | 0/20 | 0 | 0.00 | 0 |
| `q_s4_015` | `lifecycle_conflict` | 20 | 20/0 | 1/19 | 1 | 0.00 | 0 |
| `q_s4_016` | `lifecycle_conflict` | 20 | 20/0 | 1/19 | 1 | 0.00 | 0 |
| `q_s4_017` | `lifecycle_conflict` | 20 | 20/0 | 0/20 | 0 | 0.00 | 0 |
| `q_s4_018` | `lifecycle_conflict` | 20 | 20/0 | 0/20 | 0 | 0.00 | 0 |
| `q_s4_019` | `lifecycle_conflict` | 20 | 20/0 | 0/20 | 0 | 0.00 | 0 |
| `q_s4_020` | `lifecycle_conflict` | 20 | 20/0 | 1/19 | 1 | 1.00 | 0 |
| `q_s4_021` | `lifecycle_conflict` | 20 | 20/0 | 0/20 | 0 | 0.00 | 0 |
| `q_s4_022` | `lifecycle_conflict` | 20 | 20/0 | 1/19 | 1 | 1.00 | 0 |
| `q_s4_023` | `lifecycle_conflict` | 20 | 20/0 | 1/19 | 1 | 0.00 | 0 |
| `q_s4_024` | `lifecycle_conflict` | 20 | 20/0 | 0/20 | 0 | 0.00 | 0 |
| `q_s4_025` | `lifecycle_conflict` | 20 | 20/0 | 2/18 | 2 | 0.00 | 0 |
| `q_s5_001` | `temporal_intent` | 20 | 20/0 | 1/19 | 1 | 1.00 | 0 |
| `q_s5_002` | `temporal_intent` | 20 | 20/0 | 2/18 | 2 | 1.00 | 0 |
| `q_s5_003` | `temporal_intent` | 20 | 20/0 | 0/20 | 0 | 0.00 | 0 |
| `q_s5_004` | `temporal_intent` | 20 | 20/0 | 1/19 | 1 | 1.00 | 0 |
| `q_s5_005` | `temporal_intent` | 20 | 20/0 | 3/17 | 3 | 0.00 | 0 |
| `q_s5_006` | `temporal_intent` | 20 | 20/0 | 1/19 | 1 | 1.00 | 0 |
| `q_s5_007` | `temporal_intent` | 20 | 20/0 | 1/19 | 1 | 1.00 | 0 |
| `q_s5_008` | `temporal_intent` | 20 | 20/0 | 1/19 | 1 | 1.00 | 0 |
| `q_s5_009` | `temporal_intent` | 20 | 20/0 | 3/17 | 3 | 1.00 | 0 |
| `q_s5_010` | `temporal_intent` | 20 | 20/0 | 1/19 | 1 | 1.00 | 0 |
| `q_s5_011` | `temporal_intent` | 20 | 20/0 | 3/17 | 3 | 1.00 | 0 |
| `q_s5_012` | `temporal_intent` | 20 | 20/0 | 1/19 | 1 | 1.00 | 0 |
| `q_s5_013` | `temporal_intent` | 20 | 20/0 | 3/17 | 3 | 1.00 | 0 |
| `q_s5_014` | `temporal_intent` | 20 | 20/0 | 1/19 | 1 | 1.00 | 0 |
| `q_s5_015` | `temporal_intent` | 20 | 20/0 | 1/19 | 1 | 1.00 | 0 |
| `q_s5_016` | `temporal_intent` | 20 | 20/0 | 0/20 | 0 | 0.00 | 0 |
| `q_s5_017` | `temporal_intent` | 20 | 20/0 | 3/17 | 3 | 1.00 | 0 |
| `q_s5_018` | `temporal_intent` | 20 | 20/0 | 1/19 | 1 | 1.00 | 0 |
| `q_s5_019` | `temporal_intent` | 20 | 20/0 | 1/19 | 1 | 1.00 | 0 |
| `q_s5_020` | `temporal_intent` | 20 | 20/0 | 1/19 | 1 | 1.00 | 0 |
| `q_s6_001` | `multi_evidence` | 20 | 20/0 | 3/17 | 3 | 0.75 | 0 |
| `q_s6_002` | `multi_evidence` | 20 | 20/0 | 1/19 | 1 | 0.50 | 0 |
| `q_s6_003` | `multi_evidence` | 20 | 20/0 | 0/20 | 0 | 0.00 | 0 |
| `q_s6_004` | `multi_evidence` | 20 | 20/0 | 1/19 | 1 | 0.50 | 0 |
| `q_s6_005` | `multi_evidence` | 20 | 20/0 | 0/20 | 0 | 0.00 | 0 |
| `q_s6_006` | `multi_evidence` | 20 | 20/0 | 1/19 | 1 | 0.00 | 0 |
| `q_s6_007` | `multi_evidence` | 20 | 20/0 | 0/20 | 0 | 0.00 | 0 |
| `q_s6_008` | `multi_evidence` | 20 | 20/0 | 0/20 | 0 | 0.00 | 0 |
| `q_s6_009` | `multi_evidence` | 20 | 20/0 | 2/18 | 2 | 0.67 | 0 |
| `q_s6_010` | `multi_evidence` | 20 | 20/0 | 1/19 | 1 | 0.33 | 0 |
| `q_s6_011` | `multi_evidence` | 20 | 20/0 | 0/20 | 0 | 0.00 | 0 |
| `q_s6_012` | `multi_evidence` | 20 | 20/0 | 2/18 | 2 | 0.33 | 0 |
| `q_s6_013` | `multi_evidence` | 20 | 20/0 | 1/19 | 1 | 0.50 | 0 |
| `q_s6_014` | `multi_evidence` | 20 | 20/0 | 0/20 | 0 | 0.00 | 0 |
| `q_s6_015` | `multi_evidence` | 20 | 20/0 | 1/19 | 1 | 0.00 | 0 |
| `q_s7_001` | `hard_negative` | 20 | 20/0 | 0/20 | 0 | 1.00 | 0 |
| `q_s7_002` | `hard_negative` | 20 | 20/0 | 0/20 | 0 | 1.00 | 0 |
| `q_s7_003` | `hard_negative` | 20 | 20/0 | 0/20 | 0 | 1.00 | 0 |
| `q_s7_004` | `hard_negative` | 20 | 20/0 | 0/20 | 0 | 1.00 | 0 |
| `q_s7_005` | `hard_negative` | 20 | 20/0 | 0/20 | 0 | 1.00 | 0 |
| `q_s7_006` | `hard_negative` | 20 | 20/0 | 0/20 | 0 | 1.00 | 0 |
| `q_s7_007` | `hard_negative` | 20 | 20/0 | 0/20 | 0 | 1.00 | 0 |
| `q_s7_008` | `hard_negative` | 20 | 20/0 | 0/20 | 0 | 1.00 | 0 |
| `q_s7_009` | `hard_negative` | 20 | 20/0 | 0/20 | 0 | 1.00 | 0 |
| `q_s7_010` | `hard_negative` | 20 | 20/0 | 0/20 | 0 | 1.00 | 0 |
| `q_s7_011` | `hard_negative` | 20 | 20/0 | 0/20 | 0 | 1.00 | 0 |
| `q_s7_012` | `hard_negative` | 20 | 20/0 | 0/20 | 0 | 1.00 | 0 |
| `q_s7_013` | `hard_negative` | 20 | 20/0 | 0/20 | 0 | 1.00 | 0 |
| `q_s7_014` | `hard_negative` | 20 | 20/0 | 0/20 | 0 | 1.00 | 0 |
| `q_s7_015` | `hard_negative` | 20 | 20/0 | 0/20 | 0 | 1.00 | 0 |
| `q_s7_016` | `hard_negative` | 20 | 20/0 | 0/20 | 0 | 1.00 | 0 |
| `q_s7_017` | `hard_negative` | 20 | 20/0 | 0/20 | 0 | 1.00 | 0 |
| `q_s7_018` | `hard_negative` | 20 | 20/0 | 0/20 | 0 | 1.00 | 0 |
| `q_s7_019` | `hard_negative` | 20 | 20/0 | 0/20 | 0 | 1.00 | 0 |
| `q_s7_020` | `hard_negative` | 20 | 20/0 | 0/20 | 0 | 1.00 | 0 |
| `q_s7_021` | `hard_negative` | 20 | 20/0 | 0/20 | 0 | 1.00 | 0 |
| `q_s7_022` | `hard_negative` | 20 | 20/0 | 0/20 | 0 | 1.00 | 0 |
| `q_s7_023` | `hard_negative` | 20 | 20/0 | 0/20 | 0 | 1.00 | 0 |
| `q_s7_024` | `hard_negative` | 20 | 20/0 | 0/20 | 0 | 1.00 | 0 |
| `q_s7_025` | `hard_negative` | 20 | 20/0 | 0/20 | 0 | 1.00 | 0 |
| `q_s7_026` | `hard_negative` | 20 | 20/0 | 0/20 | 0 | 1.00 | 0 |
| `q_s7_027` | `hard_negative` | 20 | 20/0 | 0/20 | 0 | 1.00 | 0 |
| `q_s7_028` | `hard_negative` | 20 | 20/0 | 0/20 | 0 | 1.00 | 0 |
| `q_s7_029` | `hard_negative` | 20 | 20/0 | 0/20 | 0 | 1.00 | 0 |
| `q_s7_030` | `hard_negative` | 20 | 20/0 | 0/20 | 0 | 1.00 | 0 |
| `q_s7_031` | `hard_negative` | 20 | 20/0 | 0/20 | 0 | 1.00 | 0 |
| `q_s7_032` | `hard_negative` | 20 | 20/0 | 0/20 | 0 | 1.00 | 0 |
| `q_s7_033` | `hard_negative` | 20 | 20/0 | 0/20 | 0 | 1.00 | 0 |
| `q_s7_034` | `hard_negative` | 20 | 20/0 | 0/20 | 0 | 1.00 | 0 |
| `q_s7_035` | `hard_negative` | 20 | 20/0 | 0/20 | 0 | 1.00 | 0 |
| `q_s8_001` | `persistence_quality` | 20 | 20/0 | 1/19 | 1 | 0.00 | 0 |
| `q_s8_002` | `persistence_quality` | 20 | 20/0 | 3/17 | 3 | 0.00 | 0 |
| `q_s8_003` | `persistence_quality` | 20 | 20/0 | 1/19 | 1 | 0.00 | 0 |
| `q_s8_004` | `persistence_quality` | 20 | 20/0 | 1/19 | 1 | 0.00 | 0 |
| `q_s8_005` | `retrieval_safety` | 20 | 20/0 | 0/20 | 0 | 1.00 | 0 |
| `q_s8_006` | `retrieval_safety` | 20 | 20/0 | 0/20 | 0 | 1.00 | 0 |
| `q_s8_007` | `retrieval_safety` | 20 | 20/0 | 0/20 | 0 | 1.00 | 0 |
| `q_s8_008` | `retrieval_safety` | 20 | 20/0 | 0/20 | 0 | 1.00 | 0 |
| `q_s8_009` | `persistence_quality` | 20 | 20/0 | 1/19 | 1 | 0.00 | 0 |
| `q_s8_010` | `persistence_quality` | 20 | 20/0 | 1/19 | 1 | 0.00 | 0 |
| `q_s8_011` | `retrieval_safety` | 20 | 20/0 | 0/20 | 0 | 1.00 | 0 |
| `q_s8_012` | `persistence_quality` | 20 | 20/0 | 2/18 | 2 | 0.00 | 0 |
| `q_s8_013` | `retrieval_safety` | 20 | 20/0 | 0/20 | 0 | 1.00 | 0 |
| `q_s8_014` | `retrieval_safety` | 20 | 20/0 | 0/20 | 0 | 1.00 | 0 |
| `q_s8_015` | `retrieval_safety` | 20 | 20/0 | 0/20 | 0 | 1.00 | 0 |
| `q_s8_016` | `persistence_quality` | 20 | 20/0 | 0/20 | 0 | 1.00 | 0 |
| `q_s8_017` | `persistence_quality` | 20 | 20/0 | 1/19 | 1 | 0.00 | 0 |
| `q_s8_018` | `persistence_quality` | 20 | 20/0 | 1/19 | 1 | 0.00 | 0 |
| `q_s8_019` | `retrieval_safety` | 20 | 20/0 | 0/20 | 0 | 1.00 | 0 |
| `q_s8_020` | `retrieval_safety` | 20 | 20/0 | 0/20 | 0 | 1.00 | 0 |
