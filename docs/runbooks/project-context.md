# 自动项目路由与版本切换

本功能为实现候选，尚未在本次开发中部署。默认 legacy 命名空间、v1 队列；Codex metadata 默认关闭。真实四宿主验收通过后才启用对应功能，验收项见 [RFC-0003](/rfcs/0003-automatic-project-context)。

## 宿主接线

所有目录必须是服务所在机器可访问的绝对 Git 工作目录。容器、SSH、远程宿主路径不自动换算。

| 宿主 | 配置 |
| --- | --- |
| Codex | stdio command `hippo`，args `serve --codex-context` 才协商实验 metadata；关闭时使用标准 roots 或显式项目选择 |
| Claude Code | stdio command `hippo`，args `serve`；在载入 dotenv 之前捕获宿主 `CLAUDE_PROJECT_DIR`。确认连接专属于项目且原始环境正确 |
| Antigravity | 标准 roots；没有活动目录契约时，每个工作区配置专属 `hippo serve --project-root /abs/repo`，不得复用不同项目的连接 |
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

本次隔离测试共 718 项通过、1 项因未启用真实 Qdrant 集成而跳过；Ruff CI 规则、compileall、Python wheel/sdist 与文档构建通过。MCP 使用真实 SDK 内存传输验证初始化协商与请求 metadata，不连接生产记忆后端。

Pi 扩展契约脚本可用已安装的 Pi 包目录复跑，不安装新的依赖：

```bash
node tests/pi-extension-contract.mjs /absolute/path/to/@earendil-works/pi-coding-agent
```

已在 Pi 0.83.0 的加载器下验证搜索/写入的 ctx.cwd、缺上下文拒绝、global、两个捕获事件以及 v1/v2 版本参数。真实 Codex/Claude Code/Antigravity/Pi 聊天、窗口隔离和 Hook 时延仍须在发布环境验收。
