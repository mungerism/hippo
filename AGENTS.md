# Hippo Agent Guidelines

## 🌟 核心原则 (Core Tenet)

> **Hippo 是 Mem0 的轻量工程封装：对齐官方规范，补齐开源短板。**
> - 原生支持的（数据结构、基础存储、基础检索），100% 保持对齐，不重复造轮子；
> - 开源版缺失或不完善的（如检索防污染门禁、瞬态短语清洗、多端自动路由），在 Hippo 网关层做必要补齐。

## 📋 开发与接口规范

1. **Agent 安全子集**：面向 Agent 的 MCP 仅暴露经过安全审计的极简工具（`add_memory` 事实文本限 2000 字符；`search_memories` 由部署端把控安全门禁与注入预算，绝不对 Agent 暴露底层微调参数）。
2. **轻量依赖与服务**：Python 核心全量采用 `uv`，文档站点全量采用 `pnpm`，禁止引入冗余重依赖（如重型本地 Cross-Encoder）；底层依赖单二进制 Qdrant (`127.0.0.1:6333`) 本地自愈与保活。
3. **Docs as Code 协同约定**：
   - 优先阅读 [`docs/index.md`](docs/index.md) 与 [`docs/architecture/`](docs/architecture/) 获取最新系统架构与数据流；
   - 新的重要技术选型写入 ADR（[`docs/adr/`](docs/adr/)），尚未确定的方案编写 RFC（[`docs/rfcs/`](docs/rfcs/)）；
   - 代码变更涉及架构、接口或关键行为变化时，同一 PR 必须同步更新相关文档；
   - 严禁直接 push 到 `main` 分支，必须在独立分支（如 `feat/...`、`docs/...`）上开发并通过 PR 提交审查；
   - Git Commit Message 与 PR 标题必须使用英文规范（Conventional Commits，如 `feat(mcp): ...`）；PR 正文推荐英文，亦可附带中文说明。


## Agent skills

### Issue tracker

GitHub Issues (`mungerism/hippo`) via the `gh` CLI. See `docs/agents/issue-tracker.md`.

### Triage labels

Canonical 5-role taxonomy (`needs-triage`, `needs-info`, `ready-for-agent`, `ready-for-human`, `wontfix`). See `docs/agents/triage-labels.md`.

### Domain docs

Single-context layout (`docs/adr/` and lazy `CONTEXT.md` at root). See `docs/agents/domain.md`.


<!-- hippo:memory:start -->
## 记忆检索约定 (Hippo)

- 开始处理任务前，先调用 `search_memories` 检索当前项目记忆与个人偏好（scope: 'all'），不要只依赖当前对话。
- 用户表达偏好、做出值得保留的决策、纠正你的行为或明确要求记住某事时，调用 `add_memory` 沉淀；跨项目的个人习惯用 scope='global'。
<!-- hippo:memory:end -->
