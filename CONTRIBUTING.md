# Contributing to Hippo (海马体) 🦛

感谢关注 Hippo！Hippo 是面向 AI 编码智能体（antigravity, Codex, ZCode, Zed AI, Cursor, pi 等）的统一长短期记忆中枢，基于 Mem0 与 MCP (Model Context Protocol) 打造。

我们欢迎任何形式的贡献，包括汇报 Bug、改进文档、提出 RFC 提案或提交代码。在开始贡献前，请仔细阅读以下规范。

---

## 🌟 核心工程原则 (Core Tenets)

1. **Mem0 轻量工程封装**：对齐官方规范，补齐开源短板。
   - 原生支持的（数据结构、基础存储、基础检索），100% 保持对齐，不重复造轮子；
   - 开源版缺失或不完善的（如检索防污染门禁、瞬态短语清洗、多端自动路由），在 Hippo 网关层做必要补齐。
2. **轻量与最小依赖**：
   - Python 核心全量采用 `uv`，文档站点全量采用 `pnpm`；
   - 禁止随意引入大型本地依赖（如重型本地 Cross-Encoder）；
   - 底层存储默认依赖单二进制嵌入式 Qdrant 本地自愈与保活。
3. **Docs as Code**：
   - 任何涉及架构、接口或关键行为变更的代码提交，同一 PR 必须同步更新相关文档；
   - 新的重要技术选型写入 ADR（[`docs/adr/`](docs/adr/)），中大型新方案编写 RFC（[`docs/rfcs/`](docs/rfcs/)）。

---

## 🛠 本地开发环境准备

### 前置依赖
- **Python**: `>= 3.12`，包管理工具使用 [`uv`](https://docs.astral.sh/uv/)
- **Node.js**: `>= 22`，包管理工具使用 [`pnpm`](https://pnpm.io/)（仅用于构建文档站）
- **Git**

### 初始化项目
```bash
# 1. 克隆仓库
git clone https://github.com/mungerism/hippo.git
cd hippo

# 2. 安装 Python 核心依赖
uv sync

# 3. 配置本地开发环境变量
cp .env.example ~/.hippo/.env
# 根据需要填入 GOOGLE_API_KEY 或配置 gcloud ADC
```

---

## 🧪 质量验证与测试规范

在提交代码或发起 PR 之前，请确保本地通过以下所有质量门禁：

```bash
# 1. 语法与字节码编译检查
uv run python -m compileall hippo_memory tests
uvx ruff check --select E9,F63,F7,F82 hippo_memory tests

# 2. 运行全量单元测试
uv run pytest

# 3. 验证构建包完整性
uv build

# 4. 验证 VitePress 文档站构建
pnpm --dir docs install
pnpm --dir docs run build
```

---

## 🌿 Git 分支与 Pull Request 流程

1. **禁止直接推送到 `main` 分支**：所有修改必须在特性分支或修复分支上进行。
2. **分支命名规范**：
   - 新特性：`feat/<feature-name>`
   - Bug 修复：`fix/<issue-number>-<fix-name>`
   - 文档改进：`docs/<topic>`
   - 重构优化：`refactor/<topic>`
3. **提交规范 (Conventional Commits)**：
   - Git Commit Message 与 PR 标题必须使用**英文**，遵循 [Conventional Commits](https://www.conventionalcommits.org/) 格式：
     - `feat(...)`: new feature
     - `fix(...)`: bug fix
     - `docs(...)`: documentation changes
     - `test(...)`: adding or updating tests
     - `refactor(...)`: code refactoring without behavior change
     - `perf(...)`: performance improvement
     - `ci(...)`: CI/CD configuration
4. **发起 PR 与 Issue**：
   - **PR Title**: 必须使用英文规范（例如 `feat(mcp): support dynamic agent routing`），便于自动化 Changelog 生成。
   - **PR Body / Issue**: 推荐英文（English is preferred），亦完全欢迎中文反馈与讨论（Issues in Chinese are also welcome）。
   - 确认本地与 CI（Code Quality & Syntax, Tests, Build, Docs）全部通过。
   - PR 将在通过代码审查后由 Maintainer 合并至 `main`。


---

## 📜 开源协议

参与 Hippo 项目的贡献即视为您同意将您的贡献基于 [Apache License 2.0](LICENSE) 开源发布。
