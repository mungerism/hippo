# Doctor 巡检与环境排障

Hippo 提供了内置的 `hippo doctor` 命令行诊断工具，用于自动化检查本地部署健康状态、端口连通性、API 凭据及宿主集成。

---

## 1. 一键健康巡检

在终端执行：

```bash
hippo doctor
```

巡检器将依次检查以下四大维度：
1. **环境与配置**：`~/.hippo/.env` 存在性、Google/OpenAI API 密钥格式、ADC 凭据合法性；
2. **Qdrant 服务**：端口 `127.0.0.1:6333` 监听状态、LaunchAgent 托管状态、Collection 连通性；
3. **宿主集成**：Codex、Pi、ZCode 与 Antigravity 的 Hook 与 MCP 配置状态；
4. **Spool 队列**：异步蒸馏队列积压量与最近作业健康度。

---

## 2. 常见故障诊断与修复

### 故障 1：Qdrant 端口未监听 (`127.0.0.1:6333` 连通失败)
- **现象**：`hippo doctor` 报告 Qdrant 端口未监听。
- **排查**：
  1. 检查 `~/.hippo/bin/qdrant` 是否存在且具有执行权限；
  2. 查看 `~/.hippo/qdrant.log` 日志定位崩溃原因；
  3. 执行 `hippo service install` 重新注册并拉起服务。

### 故障 2：Vertex AI 鉴权失败或凭据失效
- **现象**：提示 `DefaultCredentialsError` 或 `FileNotFoundError`。
- **排查**：
  1. 检查环境变量 `GOOGLE_APPLICATION_CREDENTIALS` 指向的文件是否存在；
  2. Hippo 内置了凭据自愈清洗机制：若指向的文件已被删除，会自动剔除该环境变量以 fallback 至系统 Application Default Credentials (ADC)；
  3. 确认已执行 `gcloud auth application-default login` 并配置了正确的 `GOOGLE_CLOUD_PROJECT`。

### 故障 3：并发写锁超时 (`HippoLockTimeoutError`)
- **现象**：多 Agent 并发写入或批量治理时提示锁超时。
- **排查**：
  1. Hippo 针对多进程设计了细粒度锁等待机制，默认超时为 10 秒；
  2. 可通过设置环境变量 `HIPPO_LOCK_TIMEOUT=30.0` 增加超时上限；
  3. 检查是否有长事务或挂起的进程锁定了存储目录。
