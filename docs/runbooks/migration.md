# 记忆迁移与向量重建操作手册

本手册介绍如何将已有其他工具的历史记忆导入 Hippo，以及如何在更换 Embedding 模型/Provider 时进行安全的向量空间重建。

---

## 1. 跨向量模型迁移 (`hippo reindex`)

当在 `.env` 中切换 Provider（例如从默认 Gemini 切换至 Vertex AI）或更换向量模型维度时，由于向量空间不互通，历史记忆必须通过 `hippo reindex` 安全迁移。

### 核心安全准则
- **只读保护**：源 Collection 绝对不会在原地被修改或删除，随时可作为原子回滚基准；
- **静默要求**：执行实际写入迁移时，请确保源端与目标端写入端处于静止状态（quiescent）；`--dry-run` 预览支持在线执行。

### 操作步骤

```bash
# 1. 预览待迁移规模与目标配置 (零写入零调用)
hippo reindex --target-provider vertexai --dry-run

# 2. 执行正式迁移 (默认断点续传，跳过目标端已存在记录)
hippo reindex --target-provider vertexai --batch-size 32

# 3. 强制覆盖重算 (若需强制重新生成目标端向量)
hippo reindex --target-provider vertexai --recompute-existing
```

---

## 2. 导入 Codex 历史记忆 (`hippo migrate-codex`)

如果此前使用过 OpenAI Codex CLI 本地记忆：

```bash
# 从 ~/.codex/memories_1.sqlite 导入历史项目记忆
hippo migrate-codex --concurrency 3
```

- 该命令会自动解析原始对话提炼出偏好信号（Preference signals）、项目经验与避坑教训；
- 按照不同项目分别沉淀，并注入 `source="codex_migration"` 元数据。

---

## 3. 导入 ZCode 历史记忆 (`hippo migrate-zcode`)

如果此前使用过 ZCode CLI 本地记忆：

```bash
# 从 ~/.zcode/cli/memories/ 导入结构化记忆
hippo migrate-zcode
```
