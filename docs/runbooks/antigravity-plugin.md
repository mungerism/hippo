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

安装生成的 Hook 使用当前 Hippo Python 解释器执行 `python -m hippo_memory.antigravity`，传入绝对映射路径和服务名，避免依赖宿主 PATH。更换 Hippo 安装位置或 HIPPO_HOME 后重新安装插件。安装完成后新建会话；按宿主实际行为刷新配置或重连 MCP，并确认 Hook 已加载。CLI 列表未显示工作区插件不能单独证明它没有运行。

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

停用只合并该插件的 `enabled=false`，重新加载后恢复显式项目选择，不删除记忆、队列或其他插件。若同时在全局及工作区安装，需要停用两处。完整安装回滚可按备份 `manifest.json` 逐文件恢复原始字节（原来不存在的文件对应 `snapshot=null`）；恢复前先核对之后是否有其他配置变动，避免覆盖新设置。

## 证据与适用范围

本机 Antigravity App 2.23.1 已完成 [12 次原生路由探针及 1 次多根拒绝](/runbooks/project-context-extended-acceptance#antigravity-hook-validation)，证明 MCP 的 `call_mcp_tool` 事件和浅层 `overwrite.Arguments` 确实生效；这些探针没有记忆后端。正式插件的回归覆盖 A/B 并发、子目录与 worktree、显式/global、错误映射、Hook 重启/总截止时间、参数保全、安装幂等、配置保留及写入失败回滚。正式部署与真实记忆验收需独立记录；不能将 App 证据外推为所有 CLI/IDE 版本已验收。

协议参照 [Antigravity Hooks](https://antigravity.google/docs/hooks) 与 [Plugins](https://antigravity.google/docs/plugins)，目录身份遵循 [ADR-0009](/adr/0009-request-scoped-mcp-project-routing)。
