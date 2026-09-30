# 运维与排障手册 (Runbooks) 索引

运维手册（Runbooks）用于指导开发者与运维人员在日常部署、巡检维护、故障排查与数据迁移等场景下的具体操作步骤与排错路径。

---

## 📋 手册清单

| 手册分类 | 文档标题 | 适用场景 | 关键命令 / 工具 |
| :--- | :--- | :--- | :--- |
| **部署与常驻** | [Qdrant 本地常驻与部署运维](/runbooks/deployment) | 初次安装、LaunchAgent 服务托管、自愈管理 | `hippo service install/status` |
| **健康巡检** | [Doctor 巡检与环境排障](/runbooks/troubleshooting) | 端口未监听、凭据缺失、宿主集成异常排查 | `hippo doctor` |
| **数据迁移** | [记忆迁移与向量重建操作手册](/runbooks/migration) | 更换模型、升级向量维度、导入 Codex 记忆 | `hippo reindex`, `hippo migrate-codex` |
| **评测基线** | [评测 Baseline 治理与回归排障手册](/runbooks/eval-baseline-management) | 评测基线更新、CI 门禁阻断分析与异常回滚 | `uv run python -m benchmarks.runner` |
| **规模与选型** | [规模评测与 Embedding 选型对比手册](/runbooks/scale-and-component-benchmarks) | BEAM 多规模吞吐与延迟压测、LMEB 向量模型候选对比 | `uv run python -m benchmarks.runner --dataset beam-128k/lmeb` |
