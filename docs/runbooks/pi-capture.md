# Pi 会话捕获与验收

## 捕获契约

Pi 扩展在 `agent_settled` 和 `session_shutdown` 逐次读取 `ctx.cwd` 与 `sessionManager`，不使用扩展加载目录作为项目来源。

优先通过公开的 `buildSessionProjection().messages` 获取活动分支的模型可见消息，保留 Pi 对上下文编辑和压缩的处理；较旧 SDK 使用公开的 `getBranch()` 读取活动分支的 `message` 条目。`ctx.messages` / `getMessages()` 仅作为兼容路径，不能假设原生生命周期 Context 提供它们。只捕获 user/assistant 的 text 内容，不把 thinking、工具结果或其他分支文本作为事实。

入队数据包含最多 100 轮文本、活动 `leaf_id` 和 `turns_captured`。后者表示成功取得快照，包括有意为空的快照；Python 适配器不会因为助手回复为空或文件路径未提取，就用磁盘上的后续历史覆盖快照。

无法取得内存快照时，磁盘回退支持 Pi 的 `type="message"` / `message` 包装与旧版顶层 role/content。新捕获会保存文件字节边界，并沿所选 leaf 的 parentId 链读取消息；不存在的 leaf、错误 session ID、截断后的文件不用于导出其他历史。新字段为内部可选元数据，不改变 Agent MCP 工具签名、默认 v1 或部署端的 v2 开关。

## Issue #119 的历史分类

2026-11-02 的只读排查确认：88 个 skipped 作业涉及 6 个仓库目录下的 Pi 会话，未发现临时验收目录样本。对原生转录的消息时间戳与作业 `created_at` 比较，88 个作业对应的会话均已有早于捕获时刻的用户和助手文本，不能归为正常空事件。

两个独立缺陷共同造成了空提取：扩展依赖原生 Context 不提供的消息 API，磁盘回退又忽略了 `message` 外层包装。最小 Python 回归与真实 SessionManager 契约均复现了 `[] != [user, assistant]`。

旧 Pi 作业未保存精确字节边界或活动 leaf；上述时间戳分类不能还原当时的分支和完整快照，也不能将 88 个作业等同于 88 条独立漏记事实。不依据对话主题猜测哪些属于验收或应持久化，记忆价值仍由蒸馏质量门禁判断。保留旧作业的 skipped 状态，不批量 retry、不清除会话围栏。

## 可复跑验证

```bash
uv run python -m unittest tests.test_pi_capture -v
node tests/pi-extension-contract.mjs /absolute/path/to/@earendil-works/pi-coding-agent
uv run python -m tests.pi_native_acceptance /absolute/path/to/pi
```

2026-11-02 已验证 Pi 1.4.0（安装包元数据与 CLI 版本）、Node 26.12.1、Python 3.12.12：

- SessionManager 契约覆盖两个事件、v1/v2、活动分支、上下文编辑、空快照与 touched files。
- 真实 Pi CLI 使用隔离的 agent 目录、会话目录、临时 Git 仓库和 v2 Spool；关闭全局扩展、MCP、工具及上下文文件发现，仅显式加载被测扩展与本地确定性 provider。
- 一次原生用户输入产生两个生命周期捕获，每个包含两轮文本；真实 Mem0 使用确定性提取器和内存 Qdrant，Worker 完成一次沉淀，验证 canonical 项目身份、跨项目不召回和重复投递防重。
- 模型 provider 全程本地确定性返回，不使用真实凭据或远程模型；不把这项验收描述为生产远程模型验证，也不写生产记忆。

此验收脚本为本机 opt-in，需要现有 Pi 安装；Python CI 覆盖适配器与真实 Mem0/Worker 回归，不要求额外安装 Pi。

## 部署与检查

先升级兼容的 Hippo Python 包/Worker，再备份并更新 Pi 的 `~/.pi/agent/extensions/hippo-memory.ts`。新字段不能让已经运行的旧进程自动加载新代码。重启 Pi 或使用其 `/reload` 重载扩展，部署已启用 v2 时保留 `HIPPO_QUEUE_VERSION=2`；默认仍为 v1。

```bash
hippo hook status --queue-version 2
hippo hook audit-dead --queue-version 2
```

后续自然会话应出现非空 turns 和成功收据，或有明确质量过滤/协议失败原因。Hook 已挂载、进程存在或 pending 为零，都不能单独证明会话已沉淀。历史恢复另行审计，不能借部署修复重新归属旧快照。
