## 变更描述 (Description)
<!-- 请简要阐明本 PR 的核心变更目的、修复的 Bug 或新增的功能 -->

## 关联 Issue (Related Issues)
<!-- 关联格式如：Fixes #123, Closes #456 或 Ref #789 -->

## 变更类型 (Type of Change)
- [ ] 🚀 新特性 (feat)
- [ ] 🐛 缺陷修复 (fix)
- [ ] 📝 文档更新 (docs)
- [ ] ♻️ 代码重构 (refactor)
- [ ] 🧪 测试用例 (test)
- [ ] 🔧 构建/工具链配置 (chore)

## 检查清单 (Checklist)
- [ ] 我的代码遵循了本项目的代码与架构规范
- [ ] 本地运行 `uv run pytest` 全量通过
- [ ] 本地运行 `uvx ruff check --select E9,F63,F7,F82 hippo_memory tests` 无报错
- [ ] 若涉及架构、接口或行为变化，已同步更新相关文档（`docs/`）
- [ ] 若涉及文档修改，本地运行 `pnpm --dir docs run build` 成功无断链
- [ ] 本次变更未包含任何未脱敏的凭证或私有网络地址
