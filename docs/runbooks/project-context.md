# 自动项目路由与版本切换

本功能已实现，2026-10-15 本机部署已开启 canonical / v2，并完成四宿主真实召回验收，详见[发布记录](/runbooks/project-context-acceptance)。软件默认仍为 legacy 命名空间、v1 队列，Codex metadata 默认关闭；新部署须完成下述切换屏障与适用宿主验收后启用。2026-10-16 [扩展验收](/runbooks/project-context-extended-acceptance)发现 Antigravity 跨 Project 连接隔离失败及 Hook 全程超时，本机 Antigravity 已撤下固定绑定。完整验收目标见 [RFC-0003](/rfcs/0003-automatic-project-context)。

## 宿主接线

所有目录必须是服务所在机器可访问的绝对 Git 工作目录。容器、SSH、远程宿主路径不自动换算。

| 宿主 | 配置 |
| --- | --- |
| Codex | stdio command `hippo`，args `serve --codex-context` 才协商实验 metadata；关闭时使用标准 roots 或显式项目选择 |
| Claude Code | stdio command `hippo`，args `serve`；在载入 dotenv 之前捕获宿主 `CLAUDE_PROJECT_DIR`。确认连接专属于项目且原始环境正确 |
| Antigravity | 共享 `hippo serve`，安装 [PreToolUse 自动补参插件](/runbooks/antigravity-plugin)；未安装时显式传 `project_id`，不部署固定 root |
| Pi | 安装本仓库原生扩展，工具按每次回调 `ctx.cwd` 进入 `hippo agent search/add`；部署环境 `HIPPO_QUEUE_VERSION=2` 可使事件捕获发布 v2，默认 1 |

CLI 的 `hippo agent search QUERY --context-dir /abs/repo` 和 `hippo agent add TEXT --context-dir /abs/repo` 是宿主桥，不开放检索预算。缺少目录且未显式选择项目时，项目调用失败；`--scope global` 无需目录。MCP 仍只暴露原有工具。

## 检查与绑定

```bash
hippo project inspect /abs/repo
hippo project bind /abs/repo --namespace existing_project
```

inspect 输出本地路径，仅供运维者查看。bind 原子更新 `$HIPPO_HOME/projects.json`（默认 `~/.hippo/projects.json`），保留当前路由模式。应逐一核验需要保留历史空间的仓库，不能根据 basename 自动迁移同名仓库。

配置示例（仅在完成下述维护屏障后设为 canonical）：

```json
{
  "version": 1,
  "mode": "canonical",
  "bindings": [
    {"repository_anchor": "/abs/repo/.git", "namespace": "existing_project"}
  ]
}
```

anchor 必须来自 inspect。canonical 下未绑定仓库使用 `repo_` 加 anchor 的完整 SHA-256；worktree 共享 common directory，独立 clone 默认分开。移动仓库后重新显式绑定。没有配置或 `mode=legacy` 时仍用仓库名，bindings 不提前改变旧路由。

## v2 与 canonical 的维护屏障

1. 暂停四宿主捕获、自动 drain 和项目写入；备份 projects.json 与整个 spool，保留作业状态和固定身份。
2. 停止旧 daemon 及其自动重启，确认进程退出、worker 锁释放。用兼容消费者排空或明确隔离 v1 作业：`hippo hook worker --drain --queue-version 1`。核验 `hippo hook status --queue-version 1`，不能将锁忙视为升级成功。
3. 安装兼容消费者，确认其支持版本参数。v2 Worker 使用 `hippo hook worker --daemon --queue-version 2`，v2 捕获统一加 `--queue-version 2`；Pi 在重启/重载扩展前设置部署环境 `HIPPO_QUEUE_VERSION=2`。不要将它设为 Agent 工具参数。所有旧入口和自动启动脚本均须核验；现有服务安装器的默认仍是 v1，不能据其进程存在判断 v2 健康。
4. 开启 canonical 前，核验全部历史绑定及未绑定仓库预览；在暂停状态原子修改映射 mode。前台和 v2 Worker 使用相同 HIPPO_HOME，停止旧会话并新开独立会话后恢复。
5. 使用 `hippo hook status --queue-version 2` 检查独立队列 `spool/v2/`。验证四宿主在同一仓库的搜索、时间线与写入选中同一空间，再恢复捕获和写入。生产检查使用可删除的验收事实，不能将模拟测试当作真实宿主验收。

v2 发布前会检查 legacy Worker 是否持锁，但它不能替代进程版本核验与上述维护步骤。队列的 retry/prune/status 都须指定正确版本。崩溃恢复唤醒保留队列版本和原队列根目录。

## 会话隔离与回滚

身份在任何合并前持久化；重试保留原 namespace 和用户。首段上下文缺失、歧义，或会话中切换实际项目/路由模式时，会话进入持久阻断状态，后续全量历史不再自动蒸馏。开始新会话后恢复，不删除 `spool/contexts` 来绕过隔离。无关映射变动只阻止跨 revision 合并。

回滚先暂停捕获、项目写入和消费。保存 v2 队列及映射，交给兼容消费者处理或整体隔离；绝不搬入 legacy jobs。兼容消费者曾处理过的 v1 队列也必须排空或隔离后才能交给旧程序。canonical 已启用时，只能用已保存 namespace 显式访问，或继续使用兼容解析器；不能直接恢复 basename 写入。

## 开发验证记录

PR #95 隔离测试运行 718 项，717 项通过、1 项因未启用真实 Qdrant 集成而跳过；PR #98 增加 stateless 回归后为 719 项，718 项通过、1 项跳过。相关 CI 的真实 Qdrant 集成、代码检查、包构建与文档构建通过。MCP 隔离测试使用真实 SDK 内存传输验证初始化协商与请求 metadata，不连接生产记忆后端。

Pi 扩展契约脚本可用已安装的 Pi 包目录复跑，不安装新的依赖：

```bash
node tests/pi-extension-contract.mjs /absolute/path/to/@earendil-works/pi-coding-agent
```

已在 Pi 0.83.0 的加载器下验证搜索/写入的 ctx.cwd、缺上下文拒绝、global、两个捕获事件以及 v1/v2 版本参数。首次单项目召回见[发布记录](/runbooks/project-context-acceptance)，双项目并发及 Hook 时延的新结果见[扩展验收](/runbooks/project-context-extended-acceptance)。

## 日常核查与上下文失败

使用 `hippo project inspect /abs/repo` 检查 mode 与 namespace，使用 `hippo hook status --queue-version 2` 区分积压和已隔离失败。`dead` 增长需逐条核对，不能仅因 pending 为零判定捕获完整。

自动项目解析目前只支持本机 Git 目录。非 Git 会话被标为上下文未解析时，检查宿主目录及会话隔离记录；明确项目归属后用新会话重新捕获。确需支持非 Git 项目时，可由调用方明确选择命名空间，但这不会自动修复历史 Hook 作业。个人通用偏好可显式使用 global。

保留未解析作业及会话隔离记录供审计；补目录、切绑定后，不直接批量 retry 或移除 contexts 隔离记录，避免旧完整会话历史写入错误项目。

## Antigravity stateless 协议

本机真实验收确认：Antigravity 的 `2026-07-28` 请求仍声明 roots 能力，但 SDK 会抛出 `NoBackChannelError`，无法发起 `roots/list`。本次安全修订不再将启动绑定单独作为该场景的可靠来源：SDK `can_send_request=false`，或 `roots/list` 明确抛出 `NoBackChannelError` 时，只有启动绑定而没有已启用、合法的请求级目录，返回 `PROJECT_CONTEXT_UNVERIFIED`，在搜索、时间线或写入接触后端前停止。即使客户端完全不声明 roots，也检查公开的通道能力。

无绑定、无请求上下文仍返回 `PROJECT_CONTEXT_MISSING`；显式 `project_id` / `agent_id` 和有效 global 保留原语义。请求 metadata 与启动绑定冲突仍拒绝，不能用 metadata 静默盖掉旧绑定。普通 roots 异常、超时、空列表或冲突也不降级。有回传通道的既有专属 stdio 绑定契约保留；通道能力本身不证明项目归属，部署者仍须核验连接独占。

本机版本没有载入顶层 `.agents/mcp_config.json`。首次验收通过工作区插件加载固定 root，但 2026-10-16 的双 Project 验收发现同名连接复用，改用不同服务名又未发现工具，详见 [Issue #101](https://github.com/mungerism/hippo/issues/101)。当前使用无固定 root 的 `hippo serve`，项目调用显式传 `project_id`。其他版本只有在实际 A/B 交替、并发与重连均证实连接独占后，才可启用工作区 `--project-root`；不要因插件配置存在就认定专属连接成立。


## Antigravity Plugin 自动补参

正式插件通过每次 Hook 的 `workspacePaths` 补齐内层 MCP 参数，安装与回滚见 [Antigravity 自动项目路由插件](/runbooks/antigravity-plugin)。这提供了独立于 MCP metadata 的请求级目录来源，保留上述 stateless 安全检查；显式选择仍优先，不再要求 Agent 为每次正常单仓库调用主动填写项目。历史固定绑定失败结论保持有效。
