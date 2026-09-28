# 评测 Baseline 治理与回归排障手册

> **文档定位**：指导 Hippo 记忆召回评测基线（Baseline）的审批、版本更新、CI 门禁阻断分析与异常处置。  
> 关联规范：[ADR-0006: 记忆召回评测框架与 Evaluation Trace](/adr/0006-retrieval-evaluation-harness-and-trace)、`benchmarks/gold/README.md` 与 GitHub Issue [#57](https://github.com/mungerism/hippo/issues/57)。

---

## 🌟 一、核心原则与治理防线

Hippo 遵循 **“防污染与零泄漏优先于盲目刷高召回率”** 的核心安全哲学：

1. **分层防御与渐进阻断**：
   - **PR 级契约门禁 (PR Contract)**：零网络、零外部模型、零付费 API 密钥，在秒级时间内通过确定性 Replay 验证 Schema、四阶段 Trace、安全硬门禁与 Baseline Diff 逻辑；
   - **Nightly 真实回归门禁 (Nightly Regression)**：每日定时启动独立 Qdrant 容器服务，执行真实向量写入与检索，与已核准的生产 Baseline 精准对比，阻断指标退化；
   - **Release 全量基准**：发版前触发多轮端到端（包含 LongMemEval-S 与 LoCoMo-10）完整评测。
2. **四大安全硬门禁不变量 (Security Hard Invariants)**：
   - **Cross-User Leakage 恒等于 0**；
   - **Cross-Project Leakage 恒等于 0**；
   - **Superseded Fact Leakage 恒等于 0**；
   - **Hard-Negative FPR $\le 2.00\%$**；
   - 任何一项违反即判定为安全事故，**CI 立即阻断（Exit Code 2）**，严禁代码合并。
3. **Fail-Closed 契约兼容性**：
   - 当 Baseline 与当前运行的环境签名（如 `dataset_name`, `dataset_hash`, `embedding_profile`, `gate_thresholds`, `ingest_profile`）不匹配时，**拒绝静默比较**，立即抛错并阻断流水线。
4. **严禁自动批准 (No Silent Promotion)**：
   - 禁止任何 CI 脚本自动覆盖或重新提交 Baseline，所有 Baseline 变更必须通过人工审查的独立 PR 合入。

---

## 🛡️ 二、CI 门禁指标判定矩阵

| 门禁类型 | 监测指标 | 容忍阈值 | 触发行为 | 处置责任人 |
| :--- | :--- | :---: | :---: | :---: |
| **安全硬门禁** | Cross-User Leakage | $> 0$ | 阻断 (Exit 2) | PR 作者即时修复 |
| **安全硬门禁** | Cross-Project Leakage | $> 0$ | 阻断 (Exit 2) | PR 作者即时修复 |
| **安全硬门禁** | Superseded Leakage | $> 0$ | 阻断 (Exit 2) | PR 作者即时修复 |
| **安全硬门禁** | Hard-Negative FPR | $> 2.00\%$ | 阻断 (Exit 2) | PR 作者即时修复 |
| **召回质量门禁** | Recall@3 Delta | $< -0.02$ (-2%) | 阻断 (Exit 2) | 算法/引擎研发 |
| **排序质量门禁** | nDCG@3 Delta | $< -0.02$ (-2%) | 阻断 (Exit 2) | 算法/引擎研发 |
| **首位命中门禁** | MRR Delta | $< -0.02$ (-2%) | 阻断 (Exit 2) | 算法/引擎研发 |
| **契约完整性** | Manifest 关键字段不匹配 | 差异项 $> 0$ | 阻断 (Exit 1) | 基础架构研发 |

---

## 🚀 三、Baseline 更新准入与审批流程

当且仅当发生以下情况时，允许更新 Baseline：
- **算法或模型重大升级**：引入新的 Embedding 模型、升级分词器或改进 Relevance Gate 过滤算法，且指标有显著正向提升；
- **评测集版本升级**：经过架构委员会批准扩展了 Hippo Gold 数据集（如扩充至 v2.0）。

### 标准更新步骤 (Promotion Workflow)

1. **创建独立变更分支**：
   ```bash
   git checkout -b chore/update-gold-baseline
   ```
2. **在隔离临时集合上运行真实 HippoEngine 并生成新基线**：
   ```bash
   # 确保本地已启动单二进制 Qdrant (127.0.0.1:6333)
   uv run python -m benchmarks.runner \
     --dataset benchmarks/data/hippo_gold_v1.json \
     --adapter engine \
     --output-dir benchmarks/baselines \
     --report-name hippo_gold_v1_baseline
   ```
3. **审查生成结果**：
   - 检查 `benchmarks/baselines/hippo_gold_v1_baseline.json` 中的 `manifest` 包含正确的 Git SHA、Embedding Profile 和 Gate Thresholds；
   - 检查 `benchmarks/baselines/hippo_gold_v1_baseline.md` 报告，确认四大安全硬门禁全绿（`PASSED ✅`）；
4. **提交 PR 审查**：
   - Commit 标题必须使用 `chore(eval): update Hippo Gold v1 baseline`；
   - PR 正文中必须包含更新原因、指标变化 Diff 对比表以及环境 Manifest 摘要；
   - 严禁通过快速合并（Bypass），必须经过 Code Review 确认。

---

## 🔍 四、CI 阻断排障诊断树 (Troubleshooting)

```text
CI 失败 (Failed)
├── Exit Code 2: 安全硬门禁失败 / 指标退化
│   ├── Case 1: [SECURITY VIOLATION] cross-user / cross-project leakage > 0
│   │   └── 检查 HippoEngineAdapter 检索时 user_id / project_id 过滤条件是否被绕过。
│   ├── Case 2: [SECURITY VIOLATION] superseded memory leaked
│   │   └── 检查 lifecycle 过滤（hippo_memory/lifecycle.py）是否误将 status="superseded" 放行。
│   ├── Case 3: [SECURITY VIOLATION] hard-negative FPR > 2%
│   │   └── 检查 Relevance Gate 动态门槛（hippo_memory/gate.py）是否门槛被放宽。
│   └── Case 4: Recall@3 / nDCG@3 / MRR 下降超过 0.02
│       └── 下载 CI Artifacts 中的 diff_*.md，查阅 regressed_queries 逐题诊断词法或稠密分差。
└── Exit Code 1: 契约/环境异常
    ├── Case 5: Incompatible baseline report (Manifest mismatch)
    │   └── 检查是否修改了 embedding_profile 维度、模型名称或 gate 参数，导致基线不再兼容。
    └── Case 6: Qdrant service not ready
        └── 检查 GitHub Actions 容器服务配置或端口监听是否被防火墙阻断。
```

---

## 🔄 五、应急回滚机制 (Emergency Rollback)

若合入主干后在 Nightly 流水线中发生未预期的指标退化：
1. **立即拉取 Nightly Artifact**：查阅 `diff_hippo_gold_v1.md` 确认退化是由代码变更引起还是环境抖动；
2. **代码回滚**：使用 `git revert <commit-sha>` 撤销引发退化的代码提交，提交 Revert PR 阻断持续破坏；
3. **Baseline 恢复**：若误操作合入了不合格的 Baseline 文件，从 Git 历史恢复上一个稳定版本的 Baseline 文件：
   ```bash
   git checkout origin/main~1 -- benchmarks/baselines/hippo_gold_v1_baseline.json
   git commit -m "fix(eval): rollback compromised baseline"
   git push origin main
   ```
