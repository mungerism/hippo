# 双项目路由扩展验收（2026-10-16）

本次扩展验收的结论是**部分通过**：Codex、Claude Code、Pi 的双项目真实调用通过；Antigravity 跨 Project 的连接隔离失败；Hook 进程退出超过 50ms。不能据此宣称四宿主全部通过，也不应继续向当前 Antigravity 共享连接部署固定项目绑定。

本记录补充并收窄[首次发布验收](/runbooks/project-context-acceptance)的适用范围。运行时沿用已部署的 `3bb67f3`，本次仓库变更只有文档与独立基准脚本。非 Git 支持见草稿 [RFC-0004](/rfcs/0004-non-git-workspace-context)，尚未实现或部署。

## 环境与证据边界

| 项目 | 实际环境 |
| --- | --- |
| Codex CLI | 0.155.1，独立临时会话，启用请求 metadata |
| Claude Code | 2.1.216，独立 stdio 配置，原始项目环境 |
| Pi | CLI 与加载器 package.json 均为 0.99.2；安装目录仍名为 0.83.0，不以目录名判断版本 |
| Antigravity | 原生应用 2.23.1，两个本地 Project |
| 仓库 | 两个不同路径、相同 basename 的 Git 仓库 A/B；另有 A 的 linked worktree |
| 数据 | 独立验收用户与集合 `hippo_acceptance_hosts_20261015`，不使用生产记忆作合成写入目标 |

实际宿主调用通过仅做日志观察的包装器进入已安装的 Hippo。包装器记录解析身份、是否传选择器、操作 ID 和时间，不替换解析结果或后端。逐条用实际 Qdrant payload 的 `agent_id` / `user_id` 核对返回 ID，不能只相信模型文字或进程退出码。

初始只有 A/B/global 三条事实时，默认门禁因 BM25 约 0.035 低于 0.15 拒绝召回，尽管语义相似度约 0.92。随后在隔离集合加入 20 条无关合成背景事实，使该极小语料下的检索可用；没有关闭或调低门禁。此过程证明的是这套合成语料上的路由行为，不是检索质量评测。

## 真实宿主结果

| 宿主 | 实际操作 | 结果 |
| --- | --- | --- |
| Codex A/B | 每项目 search → add → search → 时间线；省略项目选择器，额外提供另一仓库作为 root | 8 次操作通过，返回身份正确 |
| Claude A/B | 每项目上述 4 次操作；独立 stdio，省略选择器 | 8 次操作通过，返回身份正确 |
| Pi A/B | 每项目上述 4 次操作；原生扩展读取回调 cwd，省略选择器 | 8 次操作通过，返回身份正确 |
| Antigravity A | 原生工作区绑定，搜索、写入与时间线 | 单项目通过，不代表连接独占 |
| Antigravity B | 同名服务，只读搜索 | **失败：返回 A 的 ID / namespace，未执行 B 写入** |
| Antigravity 唯一服务名 | 改为 `hippo-acceptance-a/b`，显式注册插件并新开聊天 | 未发现新工具，未证明该绕行可用 |

六个 CLI 进程并发启动；每个宿主 A/B 的实际工具窗口分别重叠约 Codex 16.30s、Claude 10.64s、Pi 17.35s。六个工具窗口没有共同交集，因此不把它表述为“六会话同时调用”。搜索的 project/all/global 隔离另由下述协议测试验证。

Antigravity B 的只读调用进入了 A 的同一个探针 PID，解析结果为 `acceptance_a`。可证明的是当前应用/配置下连接发生跨 Project 复用；尚不能将根因精确归结为某个缓存键或某层插件注册逻辑。问题跟踪：[Issue #101](https://github.com/mungerism/hippo/issues/101)。

[官方 MCP 说明](https://antigravity.google/docs/mcp)与[插件说明](https://www.antigravity.google/docs/plugins)描述配置与工具发现，但不足以替代连接隔离实测。只设置不同工作区配置，或只更换服务名，都不能作为本机已验收成功的依据。

## 协议与加载器验证

以下操作连接真实 Hippo 与隔离后端，但不是额外的原生聊天验收：

- MCP SDK stdio：同一连接 A → B → A、8 个并发 A/B 调用、项目时间线隔离、逐次 roots 变化拒绝与恢复。
- MCP SDK stdio：4 个并发 Claude / 运维专属绑定连接，关闭后重连不沿用旧身份。
- 已安装 Pi 加载器与扩展：同一实例 A → B → A、6 个并发回调、linked worktree 归 A、缺失 cwd 拒绝、无 cwd 的 global 成功；桥接真实已部署 CLI。

这些检查通过，覆盖 Hippo 的逐请求行为，不代表 Antigravity 会为两个 Project 正确建立独立连接。相关项目解析与队列单元回归 21 项通过。

## 延迟实测

每项 30 次，单位 ms；解析器计时不含导入或 roots 网络往返，Hook 为新进程启动至退出的全程时间，含持久化。独立临时队列持有 Worker 锁，未启动 Worker 或调用记忆后端。

| 指标 | 中位数 | p95 | 最大值 | 判定 |
| --- | ---: | ---: | ---: | --- |
| 单候选解析（预热后） | 11.15 | 12.38 | 12.97 | p95 ≤ 50，通过 |
| 候选 + 两 roots（预热后） | 33.26 | 36.90 | 37.16 | p95 ≤ 50，通过 |
| Hook 新进程全程 | 77.67 | 80.88 | 82.13 | 全程 ≤ 50，**未通过** |

此前独立一轮 30 次测量，单候选解析 p95 为 15.22ms，Hook p95 为 105.41ms、最大 138.67ms；两轮均显示 Hook 未达标。这里没有验证完整宿主传输链路的解析 p95，不能把预热解析器结果推广为端到端时延。

复跑本仓库脚本，使用部署解释器和部署入口：

```bash
/path/to/deployed/python -m benchmarks.project_context_latency \
  --hippo /path/to/deployed/hippo --samples 30 --check
```

`--check` 在任一实测目标失败时退出 1；本次预期如此。脚本同时核验每次捕获成功并实际持久化独立作业，不把快速失败当成成功样本。问题跟踪：[Issue #102](https://github.com/mungerism/hippo/issues/102)。优化须保留原子发布、fsync 和版本屏障，不能放宽现有目标以消除告警。

## 缓解、证据保留与后续门禁

用户已明确授权移除本机 Antigravity 的固定 `--project-root` 并仅重连 Hippo MCP。工作区插件配置改为 `hippo serve`；全局同名连接同样不绑定项目。没有可靠 roots / 活动上下文的项目操作须显式传 `project_id`。此操作不重启 Qdrant、Worker 或其他宿主。

原生应用重连后的两次只读调用已验证：省略选择器返回 `PROJECT_CONTEXT_MISSING`；显式传 Hippo 仓库路径后成功返回 `Project: hippo`，工具参数与实际返回 ID 均已核对。全局 Hippo 条目仅增加 `PYTHONUNBUFFERED=1` 以触发该服务配置重载，其他 MCP 条目保持原样。

原配置保存在本机 `~/.hippo/backups/antigravity-explicit-20261015T231713Z`。原始测试证据保存在 `~/.hippo/acceptance/host-routing-20261016`，不提交真实记忆、凭据或原始宿主报文到仓库。36 条合成记录已逐条核对用户、前缀和完整备份后随隔离测试集合清理，临时凭据副本已移除；生产集合未做删除。审计分别见 `cleanup-audit.json` 与 `antigravity-mitigation-verification.json`。

重新启用 Antigravity 自动绑定前，必须在实际应用中完成 A/B 交替、并发、重连，逐次核对真实返回 ID 和写入 namespace；唯一服务名只有在真实工具发现及路由均通过后才可作为方案。非 Git 绑定不能绕过这一宿主发布条件。现存 9 条非 Git dead 作业继续隔离，不批量 retry 或移除围栏。
