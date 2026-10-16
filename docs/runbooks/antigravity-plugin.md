# Antigravity 自动项目路由插件

插件把每次 `PreToolUse` 事件的 `workspacePaths` 交给 Hippo 共用项目解析器，通过 `overwrite.Arguments` 为 Hippo MCP 自动补齐 `project_id`。Agent 可省略项目参数；共享 MCP 进程不保存最近项目，也不绑定某个工作区。

## 安装

先安装包含此功能的 Hippo 版本。MCP 服务使用 `hippo serve`，移除 `--project-root` 和项目专属 `CLAUDE_PROJECT_DIR`；Hook 与 MCP 使用同一 `$HIPPO_HOME/projects.json`。已有 canonical / v2 的部署保留映射及队列，不需要重新迁移或重启 Qdrant。

```bash
# 默认：全局 ~/.gemini/config/plugins/hippo-project-routing
hippo antigravity install

# 只为一个工作区安装
hippo antigravity install --workspace /absolute/repo

# 自定义 MCP 服务名；可重复指定，仅精确匹配这些服务
hippo antigravity install --server-name hippo-memory
```

插件仅包含 `plugin.json` 和根目录 `hooks.json`，不创建 MCP 服务，不更改全局 MCP 配置或 Stop 捕获。默认 MCP 服务名为 `hippo-memory`。安装合并 `plugins.json` 和 `config.json` 的对应插件开关，保留其他插件与注册项；已有同名非 Hippo 插件、符号链接或错误 JSON 时中止。原始文件备份在 `$HIPPO_HOME/backups/antigravity-plugin-*`，输出包含备份路径；重复安装内容相同时不写入。

安装生成的 Hook 使用当前 Hippo Python 解释器执行 `python -m hippo_memory.antigravity`，传入绝对映射路径和服务名，避免依赖宿主 PATH。更换 Hippo 安装位置或 HIPPO_HOME 后重新安装插件。安装后按宿主实际行为刷新配置或重连 MCP，并通过原生调用确认 Hook 已加载；仅新建会话不证明缓存或开关已经更新。CLI 列表未显示工作区插件不能单独证明它没有运行。

## 调用契约

```text
PreToolUse(call_mcp_tool)
  精确匹配 ServerName + search_memories/add_memory
  显式 project_id/agent_id 或 global → 保留原参数，由 MCP 校验
  workspacePaths → 共用 Git / canonical 解析器
  唯一仓库身份 → overwrite.Arguments = 原参数 + project_id
  缺失、歧义、无效、超时 → deny（不含目录和异常正文）
```

搜索、自然语言时间线和写入使用相同路径。拷贝完整内层 `Arguments` 后补参，保留 query/text、scope、user_id 等字段；不向 MCP 工具增加参数。显式选择器优先语义保持不变，包括跨项目访问和旧 `agent_id` 覆盖。两个不同仓库即使同名也拒绝自动选择；同仓库子目录与 canonical Git worktree 可合并。只支持本机可访问的 Git 目录，不取插件 cwd、首个工作区、会话 ID 或产物目录作兜底。

该 Hook 只输出 `allow` 或 `deny`，不授予权限，不使用 `auto_approve` 或 `permissionOverrides`。输入上限 1 MiB；POSIX 子进程总截止时间为 2 秒，宿主超时为 5 秒。它是同步的路由检查，与 Stop 捕获的 50ms 入队目标分别衡量；后者的 [Issue #102](https://github.com/mungerism/hippo/issues/102)仍未解决。

插件损坏、未加载或被宿主终止时，未补参请求到达无固定绑定的 MCP 后仍缺少可靠项目上下文并失败。显式选择和 global 继续可用。这一保障依赖 MCP 配置保持无固定项目绑定；不要恢复已证实不安全的工作区固定 root。

## 验收与回滚

在两个独立、最好同名的 Git 仓库中新开真实宿主会话，Agent 省略选择器，分别验证搜索、时间线和写入，再交替调用、并发调用并重连。多仓库 Project 的自动项目请求应被拒绝，global 应继续工作。使用独立验收用户与集合，避免合成事实进入正常记忆；记录实际 MCP 收到的参数和最终后端空间，不能把纯路由探针当作记忆读写验收。

```bash
hippo antigravity disable
# 或停用工作区插件
hippo antigravity disable --workspace /absolute/repo
```

停用只合并该插件的 `enabled=false`，重新加载后恢复显式项目选择，不删除记忆、队列或其他插件。推荐只使用一种安装范围。同名工作区插件可能覆盖全局插件的服务名及映射路径；混合安装时仅修改工作区开关未必改变已加载命令，须在插件管理器核对实际状态和调用结果，切换到全局安装前移走工作区副本。完整安装回滚可按备份 `manifest.json` 逐文件恢复原始字节（原来不存在的文件对应 `snapshot=null`）；恢复前先核对之后是否有其他配置变动，避免覆盖新设置。

## 证据与适用范围

本机 Antigravity App 2.23.1 已完成 [12 次原生路由探针及 1 次多根拒绝](/runbooks/project-context-extended-acceptance#antigravity-hook-validation)，证明 MCP 的 `call_mcp_tool` 事件和浅层 `overwrite.Arguments` 确实生效；这些探针没有记忆后端。正式插件的回归覆盖 A/B 并发、子目录与 worktree、显式/global、错误映射、Hook 重启/总截止时间、参数保全、安装幂等、配置保留及写入失败回滚。正式部署与真实记忆验收需独立记录；不能将 App 证据外推为所有 CLI/IDE 版本已验收。

协议参照 [Antigravity Hooks](https://antigravity.google/docs/hooks) 与 [Plugins](https://antigravity.google/docs/plugins)，目录身份遵循 [ADR-0009](/adr/0009-request-scoped-mcp-project-routing)。


## 2026-10-16 正式部署与原生验收 {#release-20261016}

[PR #103](https://github.com/mungerism/hippo/pull/103)、[#104](https://github.com/mungerism/hippo/pull/104)、[#105](https://github.com/mungerism/hippo/pull/105)按顺序合并，运行代码对应 main `2492827c51fbbac91d0b16dad40db6a4ba09be57`。独立工具环境已安装 wheel，逐文件核对 `antigravity.py`、`mcp_scope.py` 和 `cli.py` 与发布源码一致；正式全局插件已启用，服务名 `hippo-memory`，仍使用无固定 root 的共享 MCP。没有切换 canonical / v2 格式，没有重启 Worker 或 Qdrant；已有其他宿主连接在下次重连时载入新版。

完整回归 739 项（738 通过、1 项真实 Qdrant 按本地契约跳过），新增插件回归 13 项。更新后的 PR [CI](https://github.com/mungerism/hippo/actions/runs/38869275509)与合并后 [main CI](https://github.com/mungerism/hippo/actions/runs/38869437647)通过，包含独立真实 Qdrant、文档和发行包构建。

在 App 2.23.1 两个真实原生 Project 中使用安装后的模块、真实记忆引擎及独立测试用户/集合，导出 8 个会话共 23 次模型工具请求；原始请求均省略 `project_id` / `agent_id`。其中测试 MCP 收到 19 次请求（18 次后端操作、1 次缺上下文拒绝），另 1 次多根请求在 Hook 阶段拒绝；3 次生产服务只读检查用于区分工作区覆盖和最终全局接线。

| 检查 | 实际结果 |
| --- | --- |
| A/B 搜索、写入和时间线 | 各自进入 `plugin_a` / `plugin_b`，无对方 ID；两次原生写入成功并被召回 |
| all / global | B 的 all 时间线包含 B 与 global；global 语义查询仅召回 global |
| 并发 | A/B 在同一 MCP PID 内经测试屏障同时执行真实后端查询，时间窗口重叠 |
| 重连 | 精确停止测试进程后，新 PID 仍正确路由 A/B |
| 多仓库 | 自动项目请求返回 `PROJECT_CONTEXT_AMBIGUOUS`，未到 MCP；global 正常 |
| 无匹配 Hook | 移走测试工作区插件后，测试别名返回 `PROJECT_CONTEXT_MISSING`，未进入后端 |
| 全局安装 | 移走同名工作区覆盖后，生产服务原生只读调用收到 Hook 改写提示，使用测试仓库的 canonical 哈希空间；本地协议检查确认真实 Hippo 仓库仍映射 `hippo` |

通用的合成查询因 `weak_lexical_collision_failed` 被门禁拒绝，改用具体 ALPHA / BRAVO / GLOBAL 标记后正常召回。未关闭或调低任何门禁。18 次后端操作包含两次“修改工作区停用开关仍执行已加载命令”的诊断，不将这两次计为停用成功。

### 捕获隔离发现与清理

工作区 `.agents/hooks.json` 的 `hippo-memory-distill.enabled=false` 未阻止全局 Stop 捕获；同名路由插件的工作区停用开关在混合安装下也未单独改变运行命令。不能以配置文件变化、新建会话或独立 MCP 集合替代运行时捕获隔离验证。

审计在两个一次性测试仓库的生产 canonical 哈希空间发现 30 条合成蒸馏记录，包含前序同一验收流程遗留记录；均逐条核对 `session_id`、source、host、user 和完整 payload。已备份含向量的原始记录，为 17 个测试会话持久阻断重放，再按确切 ID 清理，两个测试空间剩余 0 条。正常项目空间不在删除范围。独立测试集合的 5 条合成记录另行备份并随集合移除。

后续原生验收须在首次工具调用前核验生产捕获隔离或持久会话围栏；不依赖 Agent 的“不保存”提示或工作区停用字段。开关合并、缓存及验收捕获隔离由 [Issue #106](https://github.com/mungerism/hippo/issues/106) 独立跟进，不据此撤销已验证的请求级路由结论。

临时 MCP 条目及观察 Hook 已移除，测试进程已停止，测试工作区插件已移出扫描路径，B 已恢复单仓库。全局 MCP 文件精确恢复原始字节，正式全局路由插件保留。随后仅重连 Antigravity 的生产 Hippo MCP，使现有连接载入新版；在已围栏会话中再次原生只读查询，Hook 正常补参、已清理空间返回空结果，新的 Stop 作业被围栏阻断且两处测试空间仍为 0 条。生产 v2 队列检查时 pending / processing 均为 0；dead 保留并逐项审计，不批量 retry。

私有证据：`~/.hippo/acceptance/antigravity-plugin-release-20261016`；不含临时凭据或全局 MCP 配置副本。可离线复跑只读校验：

```bash
python3 ~/.hippo/acceptance/antigravity-plugin-release-20261016/verify.py
```

报告确认项目串用 ID 为 0、共享进程并发、重连、全局安装与清理。这里仅验收 App 2.23.1，CLI/IDE 不作外推；Stop 捕获时延 Issue #102 仍待处理。
