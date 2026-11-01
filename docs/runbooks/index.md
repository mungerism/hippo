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
| **项目路由** | [自动项目路由与版本切换](/runbooks/project-context) | 四宿主绑定、v2 队列和 canonical 启用/回滚 | `hippo project inspect/bind` |
| **发布验收** | [自动项目上下文发布与巡检记录](/runbooks/project-context-acceptance) | 2026-10-15 本机部署证据、范围限制与偏好变更回归 | 四宿主真实调用、v2 队列核查 |
| **扩展验收** | [双项目路由扩展验收](/runbooks/project-context-extended-acceptance) | 双项目并发、连接隔离失败与 Hook 时延 | 实际宿主调用、独立延迟基准 |
| **Antigravity 插件** | [自动项目路由插件](/runbooks/antigravity-plugin) | 逐调用补参、安装、停用和验收 | `hippo antigravity install/disable` |
| **捕获验收** | [捕获隔离与 Hook 入队延迟](/runbooks/hook-capture-acceptance) | #106 / #102 部署、原生围栏和 240 次计时证据 | `project_context_latency.py --resident --check` |
