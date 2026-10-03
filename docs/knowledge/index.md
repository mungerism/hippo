# 领域知识库 (Knowledge Base) 索引

领域知识库汇集了 Hippo 在演进过程中对相关前沿系统（如 Codex、Mem0、Claude Code 等）的深度技术调研与核心设计思考。

---

## 📚 调研与设计专栏

| 文档标题 | 主题方向 | 核心价值 |
| :--- | :--- | :--- |
| [Jev 结构化决策后端调研](/knowledge/jev-research) | Jev API、能力边界与部署 | 核对官方协议、中文局限、成本与数据条款，评估离线关系分类接入 |
| [Codex 记忆机制深度调研](/knowledge/codex-memory-research) | Codex Local Memory | 剖析 Codex 的 Stage1/Stage2 记忆抽取与 SQLite 存储机制 |
| [会话记忆蒸馏工程调研](/knowledge/session-distill-research) | Warm Path 会话反思 | 对比前台实时提取与后台异步蒸馏的延迟、成本与质量权衡 |
| [Mem0 内部机制与边界剖析](/knowledge/mem0-deep-dive) | Mem0 源码机制 | 深入剖析 Mem0 的 Graph/VectorStore 架构与开源版工程短板 |
| [Cold Path 记忆合并与生命周期设计](/knowledge/cold-path-consolidation-design) | 离线记忆治理 | 详细推导基于 ANN 相似度、LLM 分类与行版本号的合并算法 |
| [Jev Cold Path 显式旁路观察](/knowledge/jev-shadow) | Jev 旁路 | allowlist、预算、匿名差异及默认零出网边界 |
| [Jev 成对关系离线评测](/knowledge/jev-evaluation) | Jev 离线评测流水线 | 成对样本规范、合成录制回放、事实簇物理隔离与多维度质量报告 |
| [Jev 分类校准与门禁](/knowledge/jev-calibration) | Jev 分类校准与安全门禁 | 破坏性关系双阈值独立约束、RFC Go/No-Go 判定与合格配置导出 |
| [Jev 关系分类受控接管](/knowledge/jev-takeover) | Jev 治理接管与排障 | 严格准入门禁、Fail-closed 保活、完整证据链与误合并定向回滚 |
