# 调研：如何从 Session 中蒸馏记忆到 Mem0

> 调研日期：2026-09-06。一手来源：mem0 官方仓库源码（main 分支）与 mem0-plugin、Codex 官方文档（后者已于 codex-memory-research.md 核实，此处引用结论）。

## 一、Mem0 原生蒸馏管线：`add(messages, infer=True)` 就是官方机制

来源：[mem0/memory/main.py](https://github.com/mem0ai/mem0/blob/main/mem0/memory/main.py)、[mem0/memory/utils.py](https://github.com/mem0ai/mem0/blob/main/mem0/memory/utils.py)

`add()` 接受 `str | dict | list[dict]` 形式的 `messages`（如 `[{"role": "user", "content": "..."}, {"role": "assistant", "content": "..."}]`），蒸馏与否由 `infer` 决定：

**`infer=True`（默认）→ V3 Phased Batch Pipeline，LLM 蒸馏：**

1. **Phase 0 上下文收集**：`parse_messages()` 把会话扁平化为 `role: content\n` 纯文本（跳过无 content 的 tool-call 消息），并从数据库取同一 session scope 最近 10 条消息作为上下文。
2. **Phase 1 已有记忆检索**：用 filters（user_id/agent_id/run_id）向量检索已有记忆 top-10，UUID 映射为整数序号（防 LLM 幻觉引用）。
3. **Phase 2 单次 LLM 蒸馏**：系统提示为 `ADDITIVE_EXTRACTION_PROMPT`（agent 域时追加 `AGENT_CONTEXT_SUFFIX`），产出对事实的 **ADD / UPDATE / DELETE / NOOP** 决策，而非盲目追加。返回 `{"results": [{"id", "memory", "event"}]}`。
4. **自定义蒸馏指令**：单次调用用 `add(prompt=...)` 参数；全局用 `MemoryConfig.custom_instructions`（`prompt or self.custom_instructions`）。

**`infer=False` → 不蒸馏**：逐条原样入库（跳过 system 消息，metadata 记录 role/actor_id），适合确知内容的精确写入。

官方 README 的聊天示例即「**对话每轮结束时把整轮 `messages` 直传 `add()`**」——messages 直传是官方主路径；"先总结再 add" 不是必需步骤。

## 二、Mem0 官方插件的 Session 自动捕获模式（hooks）

来源：[integrations/mem0-plugin/hooks.json](https://github.com/mem0ai/mem0/blob/main/integrations/mem0-plugin/hooks.json)、`scripts/capture_session_summary.py`、`scripts/capture_compact_summary.py`、`scripts/on_stop.sh`

hooks 事件面：`SessionStart`（依赖检查 + 记忆 bootstrap + 导入 AGENTS.md/CLAUDE.md 等项目文件）、`UserPromptSubmit`、`PreToolUse`（拦截非 mem0 的记忆写入 + 元数据规范）、`PostToolUse`、**`Stop`（蒸馏主战场）**。

**Stop hook 的蒸馏流程**（`capture_session_summary.py`，每轮 assistant 回答结束触发）：

1. 读 transcript JSONL 尾部（最多 3000 行），**不自己做 LLM 总结**，而是抽两条原材料：最后一条 assistant 消息（模型第一人称的成果陈述）+ 本次会话触碰过的文件清单（从 tool_use 的 file_path/command 正则提取，≤20 个）。
2. 剥离 `<system-reminder>` 等标签防污染，用 `build_summary_prompt()` 包一层结构化指令："Extract and remember: what was requested, what was investigated, key decisions made, what was completed, and what needs to happen next."
3. 以 `messages=[{"role": "assistant", "content": summary_prompt}]` + **infer=True** 调 mem0 API——**蒸馏交给 mem0 的抽取 LLM**，hook 只负责选材与包装。
4. 关键参数：`run_id=session_id`（把 infer 去重 scope 到本会话，多轮触发后最终落库的是最新一轮摘要，而非第一轮）；`expiration_date=今天+90天`（session_summary 有保质期）；`metadata.type=session_summary`。
5. 兜底守卫：跳过子代理会话、无 API key/无 transcript 直接退出、marker 文件去重、永远 `exit 0` 不阻塞会话。

**PreCompact 补录**（`capture_compact_summary.py`）：上下文压缩（/compact）前的摘要在压缩后新会话的 SessionStart(source=compact) 时回读 transcript 补录，`metadata.type=compact_summary`——防止上下文压缩丢掉"刚发生的事"。

## 三、Codex 内置 memories 的后台蒸馏模式

来源：[Codex Memories 官方文档](https://learn.chatgpt.com/docs/customization/memories?surface=app)（详见 docs/codex-memory-research.md）

会话结束后**不立即蒸馏**：等聊天空闲足够久才在后台生成，跳过活跃/过短会话，脱敏密钥，rate-limit 剩余低于阈值跳过；`memories.disable_on_external_context=true` 时用过 MCP 工具（含 Hippo）的会话被排除。与 Mem0 插件"每轮 Stop 即蒸馏"相比是**懒蒸馏**。

## 四、Hippo 现状与架构演进决策

**现状与修正（2026-09 最新）**：
1. **引擎层完善**：`HippoEngine.add()` 已通过 `build_conversation` 解决了 `text` 与 `messages` 共存问题（text 作为助手事实追加，不被静默吞掉），并补齐透传了 Mem0 原生参数（`prompt`, `infer`, `expiration_date`）。
2. **长期事实与 run_id 隔离**：经架构审议，长期项目事实**严禁绑定 `run_id=session_id`**。Mem0 会将 `run_id` 作为硬检索过滤条件，一旦绑定会导致跨会话无法对旧会话决策执行 UPDATE/DELETE 冲突消解，形成数据孤岛。会话标识作为元数据 `metadata.session_id` 存储。
3. **认知面与系统面物理隔离**：废弃“让 Agent 在对话结束时主动调 MCP 上报 messages”的反模式（会导致 Agent 尝试将数万 Token 序列化进单次 tool call，引发 Token 灾难、Schema 污染与幻觉）。Hippo MCP 专供 Agent 单句事实沉淀与检索；会话级宏观蒸馏移至宿主切面。
4. **定稿落地架构**：
   - 采用 **宿主生命周期 Hook（Stop 主检查点 + SessionEnd 对账兜底）+ 异步 Spool 状态机**；
   - Hook 极速入队（< 50ms 退出，释放宿主），后台 Worker 单进程通过 `worker.lock` 互斥消费；
   - 采用 `mkdir` 原子占位避免 TOCTOU 竞态，基于 `Semantic Cursor` 实现精准幂等去重与 Delta Skip，最后透传专用 Prompt（`SESSION_DISTILLATION_PROMPT_V1`）调用 `HippoEngine.add()`。

## 来源

- mem0 add() 与 V3 蒸馏管线：https://github.com/mem0ai/mem0/blob/main/mem0/memory/main.py （add / _add_to_vector_store）
- parse_messages 扁平化：https://github.com/mem0ai/mem0/blob/main/mem0/memory/utils.py
- mem0-plugin hooks 接线：https://github.com/mem0ai/mem0/blob/main/integrations/mem0-plugin/hooks.json
- Stop 蒸馏脚本：https://github.com/mem0ai/mem0/blob/main/integrations/mem0-plugin/scripts/capture_session_summary.py
- PreCompact 补录：https://github.com/mem0ai/mem0/blob/main/integrations/mem0-plugin/scripts/capture_compact_summary.py
- Codex Memories：https://learn.chatgpt.com/docs/customization/memories?surface=app
