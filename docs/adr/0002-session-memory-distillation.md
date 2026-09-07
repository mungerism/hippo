# 0002. 基于双事件容灾与异步 Spool 的会话记忆蒸馏 (Session Memory Distillation)

**状态**: accepted (修订 ADR-0001 中的 MCP 参数开放范围，确立 Agent-safe 安全子集)
**日期**: 2026-09-07

## 背景与上下文 (Context)
随着 AI 编码智能体（Codex、pi、ZCode、Antigravity 等）在大型项目中的深度使用，多轮长会话中的关键架构决策、规范变更、踩坑排错经验难以自动沉淀。原有方案依赖 Agent 自主调用 MCP 工具沉淀，存在严重缺陷：
1. **Token 爆炸与 Schema 污染**：若在 MCP `add_memory` 中暴露 `messages` 列表参数，Agent 会尝试将数万 Token 的完整对话历史序列化进单次 tool call，极易引发上下文超限、JSON 解析错误及幻觉；
2. **终端阻塞与同步耗时**：若在宿主切面 Hook 中同步调用 Qdrant/LLM 蒸馏，单次耗时数秒至数十秒，严重破坏开发者终端交互流畅性；
3. **数据孤岛隐患**：若将长期项目事实直接绑定 `run_id=session_id`，Mem0 会将其作为硬过滤条件，导致跨会话完全无法对旧会话决策执行 UPDATE/DELETE 冲突消解。

## 决策 (Decision)

我们决定将会话级宏观蒸馏移至宿主生命周期切面，并构建基于异步 Spool 状态机的提纯管线：

1. **认知面与系统面物理隔离 (修订 ADR-0001)**：
   - 底层 HippoEngine 继续保持 100% 对齐 Mem0 官方参数；
   - 面向自主 Agent 的 Hippo MCP Server 专供 Agent 交互，确立 Agent-safe 安全子集：彻底剔除 `messages` 参数，仅保留单句事实（`text`，限制 2000 字符），严禁 Agent 序列化对话历史；
   - 会话级宏观蒸馏完全由外部宿主生命周期 Hook 触发并移至后台异步 Spool 状态机。

2. **双事件容灾机制 (Dual-Event Resilience)**：
   - 以 `Stop`（一轮回答完成）为主检查点，防止终端意外关闭、锁屏休眠、Ctrl+C 或进程崩溃导致记忆丢失；
   - 以 `SessionEnd`（会话结束）为对账兜底，捕获最终状态；
   - 宿主能力矩阵按实机实事求是划分：Codex 与 pi 启用双事件；ZCode 与 Antigravity 走 Stop-only 降级模式。

3. **异步 Spool 状态机与极速响应**：
   - Hook 捕获层仅执行轻量级入队操作，必须在 `< 50ms` 内 `exit 0` 并保持 stdout 纯净，严禁阻塞宿主；
   - 作业入队采用 `mkdir(~/.hippo/spool/jobs/<job_id>)` 系统原子调用作为 `create-if-absent` 占位，彻底消灭 TOCTOU 竞态；
   - 后台 Worker 通过 `worker.lock` 内核级排他锁单进程消费，具备 Lease 超时回收、3 次指数退避重试与死信兜底。

4. **两级游标与语义防重 (Semantic Cursor)**：
   - Raw ID 负责 Hook 投递级物理防重（结合转录文件 bytes offset boundary 保证单次会话长历史快照不漂移）；
   - Semantic Cursor 基于 `sha256(project_id, session_id, last_user_goal, last_assistant_final, touched_files, turns_digest)` 计算，并自动归一化回溯终端退出指令（如 `/exit`、`quit`）。既精准防范长会话滑动窗口跨度下的误去重，又彻底消除退出元数据追加引发的游标漂移，对同一语义状态瞬间拦截跳过，零额外 Token 消耗。

5. **长期事实与 run_id 彻底隔离**：
   - 会话蒸馏产生的长期事实不设置 Mem0 `run_id`，确保跨会话能被 Mem0 Additive 单程抽取管线检索并执行 ADD/UPDATE/DELETE/NOOP 冲突消解；
   - `session_id`、`host`、`cursor` 仅记入 `metadata` 作为追溯审计。

## 收益与影响 (Consequences)
- **极度轻量与高响应**：开发者在终端感受不到任何停顿（< 50ms 释放），后台单 Worker 默默完成蒸馏；
- **免维护自愈**：利用 Qdrant 常驻机制与原子文件状态机，即使终端异常中止或机器断电，重启后自动续跑，不丢作业、不重复消费；
- **符合 Mem0 原生哲学**：无缝复用 Mem0 原生批处理抽取管线与自定义 Prompt（`SESSION_DISTILLATION_PROMPT_V1`），无需重新发明抽取器。
