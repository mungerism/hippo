# [RFC-0004] 非 Git 工作目录的显式绑定与自动路由

- **作者**：Codex
- **状态**：草稿，待评审；本 PR 仅设计，不启用生产功能
- **创建日期**：2026-10-16
- **关联**：[RFC-0003](/rfcs/0003-automatic-project-context)、[ADR-0009](/adr/0009-request-scoped-mcp-project-routing)

## 1. 问题与目标

canonical 模式目前只为本机 Git 仓库解析身份。发布后巡检发现 9 条 Codex 作业来自非 Git 目录，被隔离为 `PROJECT_CONTEXT_INVALID`。这种拒绝避免了串项目，但用户在资料目录、临时工作区中产生的项目记忆无法自动归属。

目标是让**已由部署者绑定的非 Git 目录**复用四宿主的自动路由：Agent 省略 `project_id`，宿主仍提供目录，统一解析 Module 决定 namespace。无可靠目录或无绑定时继续拒绝项目操作；个人跨项目偏好仍由调用者明确指定 global。

本方案不自动创建仓库、不把所有无仓库聊天归为 global、不扫描历史会话推测归属，也不自动解除现存的 9 条失败作业及其会话围栏。SSH、容器路径换算与多个目录自动聚成一个项目仍不在首版范围。

2026-10-16 的[扩展验收](/runbooks/project-context-extended-acceptance)发现，当前 Antigravity 应用中的同名 MCP 服务会跨 Project 复用已绑定连接，工作区不同服务名又未被发现。该宿主的自动绑定需先解决连接归属问题并完成实际 A/B 验证；本 RFC 不通过新增目录绑定绕过这一发布门禁。

## 2. 决策建议

采用**运维者注册目录、统一解析器逐调用匹配**的方案。Git 仓库保持现有 common-directory 身份；非 Git 目录使用映射文件中持久化的 workspace UUID。命名空间与本机路径继续分离。

| 候选 | 优点 | 代价 / 裁决 |
| --- | --- | --- |
| 未绑定目录自动按 basename 建空间 | 无安装步骤 | 同名目录碰撞，拒绝 |
| 未绑定目录自动按绝对路径哈希建空间 | 确定性、无需注册 | 临时会话目录易产生大量孤立空间，移动后身份变化；暂不默认启用 |
| 在每个目录放身份文件 | 可随目录移动 | 改动用户资料目录，复制目录会复制身份；首版不采用 |
| 映射文件保存目录绑定与 UUID | 不修改资料目录，迁移明确，可复用历史空间 | 需要一次部署绑定；推荐 |

“自动”指每次调用不用模型重复传 ID，不代表在缺少配置时猜测目录应该归属哪个项目。

## 3. 一个解析 Module，四个宿主 Adapter

保留 `ProjectContextResolver` 的 Interface：`explicit()` 与 `automatic(candidates, roots)`。MCP、Pi 和 Worker 不各自增加一套目录匹配逻辑。实现内部新增目录身份分支，沿用现有来源验证、冲突处理、scope 门禁和不可信上下文渲染。

```text
宿主活动目录 / 专属连接绑定 / roots
  → 本机绝对路径与符号链接规范化
  → 探测 Git 身份
      成功：沿用 Git common directory 与现有映射
      确认为非 Git：匹配已注册目录绑定
      探测超时 / 权限错误：失败，不降级
  → 返回 namespace + 稳定 context_key
  → 现有 ScopeRouter / Mem0
```

Git 探测需要区分“确实不是仓库”和“无法完成探测”。目前探测失败统一返回空结果，实施时需在内部返回可判别的结果；不能将 Git 超时或损坏仓库当作非 Git 来绕过现有隔离。

内部解析结果增加稳定的 `context_key`（如 `git:<canonical-common-dir>`、`directory:<uuid>`）；保留可选的 `repository_anchor` / `worktree_root` 供 Git 运维诊断。Worker 直接使用解析结果的 key，不再以 `repository_anchor is None` 推断它必然是显式 namespace。该 key 不进入 Agent 工具 Schema，也不成为鉴权凭据。

## 4. 映射与运维 Interface

建议在 `projects.json` 的新版本中保留现有 Git bindings，增加独立的 directory bindings：

```json
{
  "version": 2,
  "mode": "canonical",
  "bindings": [
    {"repository_anchor": "/abs/git-repo/.git", "namespace": "existing_git_project"}
  ],
  "directory_bindings": [
    {
      "workspace_id": "59fb6045-21e4-4f89-98ee-c8f85111e9ab",
      "root": "/abs/research-notes",
      "namespace": "research_notes"
    }
  ]
}
```

上面的 UUID 仅为示例。实际注册时本机生成 UUID，namespace 由部署者明确指定。路径保存为规范化的绝对路径；重复 UUID、重复规范化 root、空 namespace、global 和非法格式均拒绝。

沿用运维命令并明确身份类型：

```bash
hippo project bind /abs/research-notes --kind directory --namespace research_notes
hippo project inspect /abs/research-notes/subfolder
```

`inspect` 展示类型、匹配的绑定、namespace、context_key 与配置 revision；普通 MCP 错误不暴露路径。`bind --kind directory` 首版只在 canonical 模式可用；Git 目录仍使用既有 Git 绑定，不以目录绑定遮盖它。

移动目录后，通过同一 `bind` 命令的运维选项 `--workspace-id <existing-uuid>` 更新 root，保留身份；旧 root 仍存在时拒绝将它误当成“移动”，不自动把两个独立目录合并。普通注册遇到已使用的 directory namespace 时要求明确选择已有身份或使用新 namespace，不按名称静默复用。

该选项只用于本机 CLI，不新增 Agent 参数。目录移动后旧会话仍应结束，新会话以新绑定开始；不把新目录直接注入已经固定身份的连接。

## 5. 匹配、roots 与冲突

1. 已验证活动目录先尝试 Git。即使 Git 仓库位于一个已绑定非 Git 根之下，也仍使用 Git 身份，避免父目录绑定吞掉子仓库。
2. 确认为非 Git 时，在规范化路径上按目录成员关系匹配；精确目录及其子目录属于该 workspace，不使用字符串前缀判断（`/notes-a` 不属于 `/notes`）。多个嵌套绑定选最具体的根，注册时在 inspect 中显示这一覆盖关系。
3. 同一请求同时提供活动目录与 stdio 绑定时，全部来源必须归为同一稳定 context_key。多个 roots 只做现有候选成员校验；无候选时，必须归并为唯一身份。
4. roots 中出现未绑定非 Git 路径、无效路径或不一致候选时失败，不跳过它再取其他 root。没有 Git 身份、也没有目录绑定时返回可操作的 `PROJECT_CONTEXT_UNBOUND`。
5. 符号链接通过真实路径匹配；绑定根消失、映射文件损坏或权限不足时失败。显式 namespace 和 global 保留现有优先覆盖语义。

四宿主的来源契约保持一致：Codex 逐请求 metadata、Claude 原始 stdio 环境、Pi 逐调用 `ctx.cwd`、Antigravity 经验证的专属连接或 roots。没有目录上下文的 Antigravity 全局连接仍不能自动推断非 Git 项目。

## 6. 配置升级与后台捕获

新增配置版本需先让所有前台入口及 v2 Worker 都能读取，再原子升级映射；旧程序当前会拒绝未知版本，因此不能在旧消费者仍服务时直接写 version 2。legacy 模式与 version 1 配置保持兼容，目录绑定不提前改变旧路由。

Hook 继续只捕获宿主目录线索并入队，不增加同步 Git、配置迁移或网络请求。Worker 在合并作业之前固定 directory UUID、namespace、mode 与 revision，跨绑定、跨身份或混合历史时沿用持久会话围栏。

9 条历史 dead 作业保持原状。新绑定只对后续新会话生效；历史恢复需要另行核对原始目录、完整会话来源和可验证分段，不能用当前新绑定重新解释旧快照。

回滚先暂停项目写入和消费，保留新格式映射与 v2 队列。若必须使用旧解析器，隔离 directory 作业并保留显式 namespace 访问；不能把 UUID 改成 basename，也不能把非 Git 作业搬给旧消费者猜测处理。

## 7. 验收标准

| 场景 | 必须满足的结果 |
| --- | --- |
| 两个同名非 Git 根 | 不同 UUID、不同空间，交替 / 并发调用零串用 |
| 绑定目录及子目录、符号链接 | 同一身份；字符串前缀邻居不匹配 |
| 嵌套目录绑定、父目录内 Git 仓库 | 最具体目录生效；Git 身份优先 |
| Git 探测超时 / 损坏 / 权限失败 | 不降级为 directory，后端零调用 |
| roots 为同一目录多个子目录 | 归并为一个身份 |
| 多 workspace roots，无活动候选 | 报歧义；有合法候选时仅校验成员 |
| 未绑定非 Git、来源冲突、缺 cwd | 项目操作失败；有效 global 仍可用 |
| 目录移动、旧 root 消失、旧会话重连 | UUID 保持；新会话使用新 root；旧混合历史仍隔离 |
| version 1 升级 / 回滚 / 在途作业 | 队列与 namespace 不被旧程序重新解释 |
| 四宿主实际调用 | 搜索、写入、时间线一致；校验实际 ID 与后端身份 |

同时复跑已有 Git worktree、同名仓库及历史绑定回归。性能验收沿用解析 p95 50ms 与 Hook 退出 50ms 目标，分别报告冷启动和稳态成本。本次 Git 路由测量已暴露 Hook 进程全程超标；该缺口需单独处理，不能以非 Git 方案修改目标来掩盖。

## 8. 实施拆分与待确认事项

实施顺序建议为：先明确 Git 探测结果与统一 context_key，再做 version 2 配置和运维绑定，随后接入 Worker 身份冻结，最后执行四宿主真实目录验收与升级演练。每一步使用相同解析 Interface 验证，不引入额外记忆后端或通用插件框架。

待确认：是否需要为未绑定目录增加显式 opt-in 的自动注册模式，以及临时目录的生命周期与空间回收策略。首版建议保留注册门槛；本 RFC 获确认前不实现或部署这些新行为。
