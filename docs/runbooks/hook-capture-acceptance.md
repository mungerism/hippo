# 捕获隔离与 Hook 入队延迟验收

2026-10-16 本机完成 [Issue #106](https://github.com/mungerism/hippo/issues/106) 捕获隔离修复与 [Issue #102](https://github.com/mungerism/hippo/issues/102) 入队延迟优化。[PR #108](https://github.com/mungerism/hippo/pull/108) 修复停用检查、持久围栏及轻量入口；[PR #109](https://github.com/mungerism/hippo/pull/109) 复用现有 Worker 的本机入队通道，已合并并部署至 `6531f0a`。

## 原生捕获隔离

在 Antigravity App 2.23.1 的两个独立 Git 项目中进行 7 次原生模型工具调用。模型原始请求均省略 `project_id` / `agent_id`，使用无记忆后端的专用路由探针；生产 MCP 不参与测试写入。

| 检查 | 实际结果 |
| --- | --- |
| 初始 B 会话停用捕获 | 自动持久围栏，0 个入队作业 |
| 同一已加载路由命令切换停用开关 | 项目查询立即返回 `PROJECT_CONTEXT_MISSING`，global 查询正常 |
| 同会话恢复路由开关 | 自动解析回 B，以上调用使用同一 MCP PID |
| 已围栏会话重新启用捕获 | 新捕获作业进入 dead，完整历史没有重放入库 |
| 新 B 会话、新 A 会话 | 自动解析到不同的正确项目，两者均自动围栏、0 个入队作业 |
| 清理与只读审计 | 测试 MCP 配置恢复原始字节，专用进程和观察 Hook 已移除；两个测试项目空间均为 0 条记忆 |

原有隔离事件与 30 条合成记录清理仍保留在[插件发布记录](/runbooks/antigravity-plugin)。本次验收未新增需要删除的记忆，也未解除任何历史围栏。App 结论不外推未测试的 CLI / IDE 版本。

## 已部署入口的完整进程计时

使用已安装 Python、CLI、Adapter 与 Spool，分别测常驻通道和本地回退。每种宿主、每条路径 30 次，合计 240 次，均验证实际持久落盘；常驻模式另外确认监听器成功确认 30 次请求。每次计时包含独立 Hook 进程启动、输入、发布、确认与退出，未只测预热解析器。阈值仍为最大 50ms。

| 宿主 | 常驻 p95 | 常驻最大 | 本地回退 p95 | 本地回退最大 |
| --- | ---: | ---: | ---: | ---: |
| Codex | 26.73ms | 30.30ms | 47.22ms | 47.48ms |
| Antigravity | 29.50ms | 30.82ms | 46.13ms | 46.13ms |
| Pi | 26.50ms | 26.51ms | 46.22ms | 47.38ms |
| ZCode | 25.83ms | 26.02ms | 47.46ms | 47.92ms |

8 组 `--check` 全部返回 0，单根与双根稳态解析 p95 也均通过 50ms。基准仅在独立临时 Git / spool 内启动相同的 CaptureServer 并持有 Worker 锁，不初始化 Engine、不启动额外 Worker、不使用记忆后端。另对实际生产 Worker 的 `0600` socket 做无效请求握手，确认监听已启用且非法请求被拒绝。

复跑时在仓库外执行，避免源码目录覆盖已安装包。省略 `--resident` 可测本地回退，四宿主分别重复：

```bash
cd /tmp
/path/to/deployed/python /path/to/hippo/benchmarks/project_context_latency.py \
  --hippo /path/to/deployed/hippo --host antigravity \
  --resident --samples 30 --check
```

### 先前失败与边界

#108 部署后四宿主最大耗时分别为 67.09 / 201.91 / 57.02 / 53.00ms，未达到契约；这些失败样本已保留。#109 源码版四宿主也分别做过 30 次常驻测量，最大为 28.71 / 26.63 / 42.15 / 25.93ms，最终判断仍以以上部署验收为准。

50ms 是本轮实测通过的性能门槛，不是 Python 进程在任意系统调度、休眠恢复、磁盘停顿或负载下的硬实时保证。socket 不可用、操作超时或相对路径输入回退原来的本地发布；确认丢失采用至少一次投递及既有防重，不跳过 fsync，也不宣称跨存储 exactly-once。协议与生命周期见 [ADR-0002](/adr/0002-session-memory-distillation) 和 [ADR-0005](/adr/0005-spool-governance-and-worker-service)。

## 部署、验证与证据

安装前备份包、映射、会话围栏和 Worker plist；通过 `uv` 安装 wheel，核对已安装关键模块与合并源码一致。确认无 processing 作业后仅重启 Worker，服务恢复 running；Qdrant、其他 MCP 连接、队列格式、canonical 映射和 Worker 参数保持原状。

[PR CI](https://github.com/mungerism/hippo/actions/runs/38879698658) 全部通过：764 项测试（763 通过、1 项按环境跳过），另有真实 Qdrant 集成、评测门禁、文档、打包和 CodeQL。新增 11 项通道测试覆盖真实子进程无重模块导入、持久化确认、停用捕获、失败回退、确认丢失、并发、相对路径和持锁生命周期。

私有证据保存在 `~/.hippo/acceptance/capture-isolation-and-latency-20261016`，包含原始计时样本、原生工具调用、清理审计、部署核验和只读离线复核脚本，不含 MCP 凭据：

```bash
python3 ~/.hippo/acceptance/capture-isolation-and-latency-20261016/verify.py
```

安装回滚备份为 `~/.hippo/backups/worker-capture-transport-20261016T092255Z`。回滚仅还原包并重启 Worker；不回退映射、不删除队列、不解除会话围栏。
