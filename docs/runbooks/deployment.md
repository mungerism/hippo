# Qdrant 本地常驻与部署运维

Hippo 采用 100% 本地优先原则，底层向量存储依赖运行在 `127.0.0.1:6333` 的单二进制 Qdrant 实例。

---

## 1. 运行模式

Hippo 支持两种 Qdrant 运行模式：

1. **LaunchAgent 系统常驻模式（推荐）**：
   通过 macOS 的 `launchd` 机制将 Qdrant 注册为系统级后台常驻服务（`dev.hippo.qdrant`），开机自启、崩溃自动重启。
2. **按需按次拉起模式（On-Demand Fallback）**：
   若未安装常驻服务，每次调用 `hippo` 命令或通过 MCP 唤起时，Hippo 会自动检测端口 `6333`；若未监听且本地二进制存在，会自动通过后台子进程拉起。

---

## 2. 管理命令

使用 `hippo service` 子命令管理 LaunchAgent：

```bash
# 查看当前服务状态
hippo service status

# 安装并启动 LaunchAgent 常驻服务
hippo service install

# 卸载并停止常驻服务
hippo service uninstall
```

---

## 3. 核心路径与目录结构

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
│   └── spool.db             # Spool 存储
├── qdrant.log               # 服务端运行与崩溃日志
└── .env                     # 本地环境配置 (API 密钥与模型参数)
```
