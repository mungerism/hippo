# 自动项目上下文发布与巡检记录

本记录描述 2026-10-15 本机部署与实际验收，其他机器应按[切换手册](/runbooks/project-context)独立验证。软件默认开关与本机已启用配置分开记录。

## 部署与回滚依据

- 合并 [PR #95](https://github.com/mungerism/hippo/pull/95) 和 [PR #98](https://github.com/mungerism/hippo/pull/98)，部署提交 `3bb67f31fe4ba87306fd7cbefb14d4da91122667`，已核对安装包与源代码一致。
- 本机启用 canonical、12 个历史 namespace 显式绑定及 v2 Worker / Hook；软件默认仍为 legacy / v1，Codex metadata 仍需 `serve --codex-context`。
- 切换前配置与队列备份位于本机 `~/.hippo/backups/pr95-20261015T071105Z`；验收摘要位于 `~/.hippo/acceptance/pr95/acceptance-report.json`。这些是本机证据位置，不随仓库发布，勿将原始报文、路径清单或真实记忆提交 Git。
- 切换期间仅重启 Hippo Worker 并重建 MCP 连接，Qdrant 未重启。回滚须保留 v2 队列和 namespace，暂停捕获与消费后按切换手册操作，不能直接恢复 basename 写入。

## 真实宿主验收

| 宿主 | 本机已验证的来源 | 召回结果与限制 |
| --- | --- | --- |
| Codex | 协商后的请求级 sandbox cwd，本地 `file:` URI | 省略项目选择器召回通过；需显式启用实验能力 |
| Claude Code | 原始 `CLAUDE_PROJECT_DIR` 与 roots | 专属 stdio 连接省略项目选择器召回通过 |
| Pi | 原生扩展工具回调的 `ctx.cwd` | 省略 project 召回通过；测试使用已配置的可用模型，未更改默认模型 |
| Antigravity | 工作区专属插件的 `--project-root` | 省略项目选择器召回通过；本机 stateless 连接无 roots 回传通道 |

验收逐条核对实际返回的记忆 ID。`all` 返回当前项目加当前用户 global、排除其他项目；`project` 排除 global 与其他项目；`global` 无需项目上下文；时间线保持项目隔离。共用入口及失败零后端调用另有隔离契约测试覆盖。

合成事实使用独立验收用户和隔离集合。早期进入生产集合的 4 条合成事实已在逐条核验、备份并获用户授权后删除，最终验收记录保留的合成测试点为零；正常记忆未删除。

本次证据支持本机已测试配置下的真实召回及作用域隔离，不等同于逐宿主全部工具、所有版本、多窗口/多目录组合或性能指标均已实测。Hook 的 50ms 目标、解析 p95、长时间稳定性及未覆盖宿主组合仍需独立测量。

Antigravity 仅对明确的 SDK `NoBackChannelError` 使用独立合法绑定继续解析。工作区插件提供专属 MCP 配置，全局连接不固定绑定某个仓库。普通 roots 错误、超时或冲突不降级；其他工作区需各自配置和验收。

## 发布后只读巡检（2026-10-15）

首次验收时 v2 为 completed 18、skipped 1、dead 5。本次后续快照为 completed 31、skipped 4、dead 9，pending / processing 均为 0；这是快照，不代表持续监控结果。

- 12 个绑定的 anchor 均存在；逐仓库 `project inspect` 的 mode 为 canonical，namespace 均与绑定一致。
- 在可访问本机端口的运维环境中，Qdrant 与 Worker 均常驻运行，Qdrant `/healthz` 返回 HTTP 200。
- 已完成作业中，Codex 的 9 条固定为 Hippo namespace，Antigravity 的 22 条固定为另一个绑定项目；没有共用安装仓库 namespace 的现象。此检查只验证持久化身份，不单独证明蒸馏内容完整或所有写入均无污染。
- 9 条 dead 作业全部来自非 Git 目录，且没有固定 routing identity；用同一解析器只读复核均返回 `PROJECT_CONTEXT_INVALID`。这些作业保留隔离，没有批量重试或解除会话围栏。

因此当前消费无积压，已知缺口是非 Git 会话未自动蒸馏。若要覆盖非 Git 项目，应另行定义其稳定身份与捕获契约；不能以猜测项目或写入 global 代替。

## 偏好变更回归与治理边界

此次实际召回同时出现“PR / Issue 优先英文”和新的“默认中文”历史记忆。按 [ADR-0003](/adr/0003-cold-path-memory-consolidation)，Hot / Warm Path 先捕获，Cold Path 后治理；默认写入与搜索不自动运行 consolidation。因此，保存新偏好不代表旧事实已被标为 superseded，不能仅凭两条同时召回就认定生命周期过滤失效。

隔离回归使用合成的 PR 语言偏好，覆盖以下行为：

1. 治理前两个活跃历史事实可同时召回；dry-run 不改变记忆。
2. 分类器将同一偏好判为 CONFLICT 后，更新的 `last_confirmed_at` 胜过旧记录更高的确认次数；旧项软废弃，召回仅保留中文偏好。
3. 旧项仍可通过审计 get 查看血统；英文 Conventional Commits 的独立事实保留；重复治理不再变更记忆。

复跑：

```bash
uv run python -m unittest tests.test_consolidator.TestPreferenceChangeEndToEnd -v
```

测试用脚本化分类响应驱动真实 discovery、仲裁、apply、journal 和召回过滤，不连接生产后端，也不评估在线 LLM 对中文冲突的识别质量。生产旧记忆仍待单独预览与审查；本次未执行生产 consolidation，也未启用自动治理。

本次新增 3 项回归通过；连同 consolidation、生命周期过滤、项目解析和项目队列的相关测试共 71 项通过。文档构建及 CI 所用 Ruff 错误规则检查通过。
