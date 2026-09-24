# 技术方案提案 (RFC) 索引

技术方案提案（Request for Comments，RFC）用于在中大型功能开发、重构或破坏性变更前，进行充分的需求分析、架构设计与备选方案讨论。

---

## 📋 RFC 提案清单

| 编号 | 提案标题 | 状态 | 负责人 | 核心方向 |
| :--- | :--- | :--- | :--- | :--- |
| [RFC-0001](/rfcs/0001-jev-cold-path-classifier) | Jev 作为可选的 Cold Path 关系分类后端 | 草稿 | Codex | 先隔离评测和旁路观测，达标后限定接管 |
| [RFC 模板](/rfcs/template) | RFC 标准设计提案模板 | 模板 | Architecture | 规范中大型需求提案格式与评审要素 |
| [RFC-0002](/rfcs/0002-jev-adoption-gates) | Jev Cold Path 分阶段接入与接管门禁 | 草稿 | Codex | 隔离评测、旁路和限定接管的安全条件 |

---

## 💡 流程规范

1. **发起阶段**：复制 [RFC 模板](/rfcs/template)，完成背景、目标、非目标与方案设计；
2. **讨论阶段**：在 PR 或 Issue 中发起讨论，征集反馈并持续修订；
3. **裁决阶段**：达成共识后沉淀为 ADR 并编写具体的实施计划（Implementation Plan）。
