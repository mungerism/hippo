# Qdrant 与 Worker 本地常驻部署运维

Hippo 采用 100% 本地优先原则，底层向量存储依赖运行在 `127.0.0.1:6333` 的单二进制 Qdrant 实例。

---

## 1. 运行模式

Hippo 支持两种 Qdrant 运行模式：

1. **LaunchAgent 系统常驻模式（推荐）**：
   通过 macOS 的 `launchd` 机制将 Qdrant 注册为系统级后台常驻服务（`dev.hippo.qdrant`），开机自启、崩溃自动重启。
2. **按需按次拉起模式（On-Demand Fallback）**：
   若未安装常驻服务，每次调用 `hippo` 命令或通过 MCP 唤起时，Hippo 会自动检测端口 `6333`；若未监听且本地二进制存在，会自动通过后台子进程拉起。

---

## 2. Qdrant 管理命令

使用 `hippo service` 子命令管理 Qdrant LaunchAgent：

```bash
# 查看当前双服务状态（Qdrant + Worker）
hippo service status

# 安装并启动 Qdrant LaunchAgent 常驻服务（默认行为）
hippo service install

# 卸载并停止 Qdrant 常驻服务
hippo service uninstall
```

---

## 3. Worker 常驻守护部署

Spool 异步蒸馏队列的消费由 `dev.hippo.worker` LaunchAgent 服务负责。

### 3.1 安装与管理

```bash
# 安装 Worker 常驻守护进程
hippo service install worker

# 同时安装 Qdrant 与 Worker
hippo service install all

# 重启 Worker 服务
hippo service restart worker

# 卸载 Worker 常驻服务
hippo service uninstall worker

# 查看双服务运行状态（含 PID、退出码等）
hippo service status
```

### 3.2 Worker 运行机制

- **单实例排他锁**：Worker 通过内核 `fcntl.flock` 实现单进程互斥，同时只有一个 Worker 实例可以运行；
- **LaunchAgent 保活**：plist 中配置 `KeepAlive: true` + `RunAtLoad: true`，崩溃后由 `launchd` 自动拉起；
- **优雅退出**：Worker daemon 监听 `SIGTERM` / `SIGINT` 信号，收到信号后完成当前安全边界再退出；
- **日志路径**：`~/.hippo/logs/worker.log`；
- **环境变量**：Worker LaunchAgent 自动注入 `HIPPO_DISABLE_RECOVERY_WAKEUP=1`，禁用多余的定时唤醒进程。

### 3.3 Fallback 按需消费模式

若未安装 Worker LaunchAgent，每次 Hook 捕获（`hippo hook capture`）后会自动 spawn 一次性后台 `--drain` 进程消费队列。此模式无需额外配置，但在宿主 Hook 不频繁触发时可能导致积压。

---

## 4. 核心路径与目录结构

所有 Hippo 运行时数据均统一保存在 `~/.hippo`（或环境变量 `HIPPO_HOME` 指定的目录）：

```text
~/.hippo/
├── bin/
│   └── qdrant               # Qdrant 单二进制执行文件
├── config/
│   └── qdrant.yaml          # Qdrant 服务端配置文件
├── storage/                 # 物理持久化目录
│   ├── history.db           # SQLite 记忆版本历史
│   └── qdrant/              # Qdrant 向量库存储
├── spool/                   # 异步会话蒸馏队列
│   ├── jobs/                # 作业目录（payload.json + state.json）
│   ├── receipts/            # 语义去重收据（永不清理）
│   └── tombstones/          # 已清理作业墓碑标记（保持 job-id 幂等）
├── logs/
│   └── worker.log           # Worker 常驻守护日志
├── qdrant.log               # Qdrant 服务端运行与崩溃日志
└── .env                     # 本地环境配置 (API 密钥与模型参数)
```
