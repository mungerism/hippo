# 0005. Spool 队列死信批量治理、Tombstone 幂等墓碑与 Worker LaunchAgent 常驻保活 (Spool Governance and Worker Service)

- **状态 (Status)**: 已通过 (Accepted)
- **决策者 (Deciders)**: Hippo 核心团队
- **日期 (Date)**: 2026-09-22

---

## 1. 背景与问题陈述 (Context and Problem Statement)

在 [ADR-0002](/adr/0002-session-memory-distillation) 中，Hippo 确立了基于异步 Spool 状态机与宿主生命周期切面的会话记忆蒸馏管线。原有设计采用“Hook 触发捕获 + 启动临时 detached `--drain` 子进程”的按需消费模式。

随着多宿主（Codex、ZCode、Antigravity、Pi）并发会话量的增加与生产运行深入，该模式暴露出以下系统级瓶颈与运维短板：
1. **死信积压与批量恢复缺失**：当外部模型提供商发生配额超限（如 Gemini API 429 RESOURCE_EXHAUSTED）或偶发网络波动时，作业在耗尽 3 次指数退避后会进入 `dead` 状态。原系统仅支持单作业 `hippo hook retry <job_id>`，面对数百项历史死信缺乏批量审计与一键重试能力；
2. **作业累积与幂等破坏风险**：Spool 目录随着使用持续累积完成态（completed/skipped/coalesced/dead）作业。若直接通过外部脚本删除作业目录，会导致该 `job_id` 失去防重占位；一旦宿主重放相同会话事件，将导致重复入队与重复蒸馏；
3. **前台 Hook 触发依赖与临时进程竞争**：按需拉起模式完全依赖新 Hook 事件触发消费。若开发者长时间未产生新交互，积压的 pending 作业无法被及时消费；而在高频交互时，频繁 spawn 临时 worker 容易造成内核锁争用与短暂的进程抖动；
4. **热路径探测延迟**：在 Hook 捕获的热路径中，若采用 `launchctl print` 同步探测服务状态，在系统负载高或 launchd 迟钝时可能产生高达数秒的阻塞，严重违背 `< 50ms` 极速释放的铁律。

---

## 2. 备选方案考量 (Considered Options)

- **方案 A：引入外部消息队列中间件（如 Redis / RabbitMQ / Celery）**
  - *优点*：原生支持死信队列（DLQ）、持久化消费与分布式 Worker。
  - *缺点*：严重违背 Hippo 100% 本地优先与单二进制轻量原则，大幅增加宿主依赖与部署复杂性，不可接受。
- **方案 B：基于系统 Cron 定时轮询拉起 `--drain`**
  - *优点*：无需增加常驻服务。
  - *缺点*：调度时效性差（最小粒度 1 分钟），无法做到实时消费，且缺乏 `launchd` 的崩溃保活自愈与细粒度生命周期管理。
- **方案 C：双服务 LaunchAgent 常驻架构 + POSIX 文件原子状态机治理（入选方案）**
  - *优点*：
    1. 零新增外部依赖，纯利用 macOS `launchd` 实现 `dev.hippo.worker` 常驻保活（KeepAlive/RunAtLoad）；
    2. 基于内核 `fcntl.flock` 单实例排他锁，优雅支持常驻 Daemon 与临时 Drain 协同；
    3. 引入 Tombstone 墓碑机制与 POSIX 原子 staging 目录重命名，实现清理与并发重试的绝对安全性；
    4. Hook 热路径采用内核锁快速非阻塞探测（<0.1ms），消除任何同步命令开销。

---

## 3. 决策结果 (Decision Outcome)

所选方案：**方案 C**。

### 3.1 Worker LaunchAgent 双常驻架构

将 Hippo 本地服务扩展为对等双常驻架构：
- `dev.hippo.qdrant`：底层向量数据库守护进程（端口 `127.0.0.1:6333`）；
- `dev.hippo.worker`：Spool 异步蒸馏常驻消费守护进程（命令 `hippo hook worker --daemon`）。

**运行与保活规范**：
- **服务配置**：plist 配置 `RunAtLoad: true` 与 `KeepAlive: true`，崩溃由操作系统自动拉起；注入环境变量 `HIPPO_DISABLE_RECOVERY_WAKEUP=1` 禁用多余的定时唤醒进程；
- **日志规整**：标准输出与错误重定向至 `~/.hippo/logs/worker.log`；
- **优雅退出**：Worker daemon 监听 `SIGTERM` 与 `SIGINT` 信号，收到退出信号后平滑完成当前作业安全边界再释放锁退出；
- **向后兼容性**：`hippo service install` 无参数默认仍仅操作 `qdrant`，通过 `hippo service install worker` 或 `hippo service install all` 显式管理。

### 3.2 Hook 热路径 < 0.1ms 内核锁探测

为保证 Hook capture 在 50ms 内退出的绝对性能：
- 在 `SpoolStorage` 中实现 `is_worker_active()`：通过非阻塞 `fcntl.flock(fd, LOCK_EX | LOCK_NB)` 瞬时探测 `worker.lock`；
- 若锁已被常驻 Worker 或前台 Drain 持有，说明后台正在消费，capture 立即退出（0 额外耗时）；
- 若锁未被持有（按需模式或常驻服务未安装），安全降级 spawn detached drain 进程；
- 严禁在 capture 热路径中调用任何 `subprocess` 执行 `launchctl`。

### 3.3 死信批量治理与状态机边界

- **批量重试**：新增 `hippo hook retry --all-dead`，支持 `--dry-run` 预览与批量重置；
- **状态机合法性约束**：引入 `RETRYABLE_STATES = {"dead", "skipped"}`。单作业重试与批量重试仅允许重置死信或跳过的作业，严禁重置 `processing`、`completed` 或 `coalesced`，避免重复消费与数据污染；
- **状态重置自愈**：重置时同步清除 `attempt=0`、`not_before=0.0`、`error=None`、`worker_pid=None`、`claimed_at=None`、`skip_reason=None`。

### 3.4 安全修剪 (Prune) 与 Tombstone 幂等墓碑

为防止磁盘空间无限增长并保持入队幂等性，构建两级安全修剪机制：
1. **严格终态过滤**：`hippo hook prune` 仅支持 `TERMINAL_STATES`（dead、skipped、completed、coalesced），严禁修剪 `pending` 与 `processing`；
2. **非负时间校验**：`--days` 与 `--hours` 强制必须为非负数，杜绝负数时间差导致全量静默误删；
3. **破坏性操作保护**：非 `--dry-run` 模式必须显式提供 `--force / -f`；
4. **Tombstone 墓碑持久化**：删除作业目录前，在 `~/.hippo/spool/tombstones/<job_id>.json` 写入墓碑凭据（记录 `final_state`、`pruned_at` 与 `semantic_cursor`）。`enqueue()` 入队前先检查 tombstone，命中直接返回 `is_new=False`，保障作业生命周期内的永久幂等；
5. **POSIX 原子 Staging 隔离**：删除前先将作业目录原子重命名为 `.prune_<job_id>_<pid>_<ts>` 暂存区。在暂存区内执行最终状态复核与墓碑写入；若检测到并发 `retry` 导致状态变为 pending，或墓碑写入异常，立即原子 `rename` 恢复原目录并放弃删除；
6. **收据永不清理**：`~/.hippo/spool/receipts/` 保存会话级语义去重游标（Semantic Cursor），与作业生命周期彻底解耦，**永不被 prune 清理**。

---

## 4. 收益与影响 (Consequences)

### 收益 (Positive Consequences)
- **零延迟全天候消费**：常驻 Worker 实现真正的异步流式消费，会话结束后记忆秒级提纯，无需等待下一次交互；
- **生产级运维闭环**：提供批量重试、状态修剪、服务启停、三态健康巡检（fallback / running / broken）的完整工具链；
- **高并发零竞争**：基于内核锁与原子目录重命名，即使面对多 Agent 并发写入、重试与清理交叉执行，依然具备数学级幂等与数据零丢失保证；
- **极致轻量**：全量代码仅基于 Python 标准库与系统原生能力，未增加任何第三方重依赖。

### 负面影响与折衷 (Trade-offs)
- **元数据微量累积**：已清理作业会在 `tombstones/` 留下每个约几百字节的 JSON 文件以维系永久幂等，但相比完整作业目录（含转录历史），空间占用下降 99% 以上；
- **macOS 平台特性绑定**：`launchctl` 深度绑定 macOS 环境，Linux/Windows 环境需在后续通过 systemd 或跨平台守护方案补充对齐。

---

## 5. 实施指导与不变式 (Implementation Guidelines & Invariants)

1. **Hot Path Budget**：`hippo hook capture` 整体执行时间必须 `< 50ms`，禁止包含外部网络请求与同步子进程调用；
2. **Terminal-Only Pruning**：`prune_jobs` 绝不能删除任何非终态作业；
3. **Tombstone Before Unlink**：任何物理删除作业目录的操作，必须在 Tombstone 成功落盘之后；若落盘失败必须放弃删除；
4. **Receipts Preserved**：`receipts/` 目录严禁被 prune 遍历或清理；
5. **Lock Exclusivity**：同一时刻在整个系统内只能有一个 Worker 进程持有 `worker.lock` 并消费队列。
