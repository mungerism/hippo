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

3. **异步 Spool 状态机与持久化原子发布 (修订 / Issue #80)**：
   - Hook 捕获层仅执行轻量级入队操作，必须在 `< 50ms` 内 `exit 0` 并保持 stdout 纯净，严禁阻塞宿主；
   - 彻底废弃早期易产生空目录半成品质押的 `mkdir` 占位设计，改为 **Durable Staging -> READY.json 标记 -> publish.lock 互斥锁 -> 原子 rename -> 父目录 fsync**：
     1. 作业先在 `staging/<job_id>.<token>` 完整落地 `payload.json`、`state.json` 及 `READY.json` 标记，并对文件与 staging 目录执行 `fsync`；
     2. 在进程间排他发布锁 `publish.lock` 保护下，检查终态与 Tombstone 后执行 POSIX 原子 `rename` 迁至 `jobs/<job_id>` 并对父目录执行 `fsync`；
     3. 崩溃或断电重启时由 `recover_spool_publication()` 自动恢复已完成的 staging，并将不完整的死 staging 移至 `quarantine/` 隔离，彻底消除 ghost job，支持 same-event redelivery；
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

## 2026-10-16：捕获隔离由 Hippo 自身执行

Antigravity 原生验收发现工作区禁用字段未阻止全局 Stop 捕获（Issue #106）。在捕获入口和 Worker 消费前检查工作区 `hippo-memory-distill.enabled=false`，任一工作区停用或配置损坏即拒绝整个捕获段，并持久阻断会话；已有冻结身份不能绕过后续围栏。重新启用需新会话，保持完整历史归属规则，既有作业不自动重放。

新增本机运维命令 `hippo hook fence`，允许验收在首次工具调用前持久落围栏并核验。它不属于 Agent MCP，也不撤销已完成或已进入后端的写入。与宿主的 Hook 合并/缓存行为解耦，正常会话及已有 durability、版本屏障和检索门禁保持不变。操作细节见 [Antigravity 插件运维](/runbooks/antigravity-plugin)。


## 捕获入口与后台依赖分离（Issue #102）

常用 `hippo hook capture` 和 `python -m hippo_memory.cli hook capture` 在 CLI 初始化前进入共用轻量捕获函数；正常发布不加载 Typer、Rich、后端配置或蒸馏依赖。仅在首次确需唤醒 Worker 时导入进程启动库；帮助、非法选项和同步调试仍交给完整 CLI。所有入口共用 Adapter、策略检查及原子 Spool 发布，保留 fsync、READY 标记、版本屏障与 fail-safe，不引入新的服务或依赖。后台的质量策略与提示词在消费时加载，历史 helper 导出保持兼容。


## 复用 Worker 的本机入队通道（Issue #102）

常驻 Worker 在持有对应版本 `worker.lock` 后启动同进程 Unix socket 监听线程，退出前关闭线程与 socket，再释放锁。它不引入新服务、第三方依赖或 TCP 端口；入口仅接受同用户、权限 `0600` 的 `capture.sock`，请求只有宿主、cwd、原始 Hook 输入和队列版本，大小上限 8 MiB。

Hook 首选本机通道，Worker 仍执行原有 Adapter、捕获停用策略、持久围栏、旧 Worker 屏障和原子入队；只有完整发布并完成 fsync 才返回成功确认。前台因此无需加载 Adapter 与 Spool 模块。无监听、版本不符、25ms socket 操作超时或协议错误时回退原来的本地发布；相对路径输入保留本地解释，`--sync` 保留原路径。确认丢失可能触发重复投递，继续依靠既有 transcript boundary / Semantic Cursor 防重，不宣称分布式 exactly-once；无转录的时间边界仍可能产生重复的空作业。

该通道优化常驻 Worker 场景；回退路径的 Python 冷启动和系统调度仍可能超过 50ms。基准新增 `--resident`，在独立 spool 内启动相同监听器并持有 Worker 锁，计入完整子进程启动、持久化确认与退出，逐条核对实际作业；默认模式仍测本地回退。原来的 50ms 门槛不变，保留每次采样与失败结果，最终以部署实测为准。
