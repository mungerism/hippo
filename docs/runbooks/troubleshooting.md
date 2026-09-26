# Doctor 巡检与环境排障

Hippo 提供了内置的 `hippo doctor` 命令行诊断工具，用于自动化检查本地部署健康状态、端口连通性、API 凭据及宿主集成。

---

## 1. 一键健康巡检

在终端执行：

```bash
hippo doctor
```

巡检器将依次检查以下五大维度：
1. **环境与配置**：`~/.hippo/.env` 存在性、Google/OpenAI API 密钥格式、ADC 凭据合法性；
2. **Qdrant 服务**：端口 `127.0.0.1:6333` 监听状态、常驻内存占用 (RSS)、磁盘数据目录物理实际占用 (Disk) 与预分配稀疏上限、LaunchAgent 托管状态、Collection 连通性；
3. **Worker 服务**：`dev.hippo.worker` LaunchAgent 运行状态，区分三态健康语义：
   - ✅ 未安装：正常（按需消费模式）
   - ✅ 已安装且 running：正常（常驻保活）
   - ❌ 已安装但未运行 / crash loop：故障
4. **宿主集成**：Codex、Pi、ZCode 与 Antigravity 的 Hook 与 MCP 配置状态；
5. **Spool 队列**：异步蒸馏队列积压量、死信作业数量与最近作业健康度。

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

### 故障 4：Spool 队列死信作业积压
- **现象**：`hippo doctor` 报告存在 dead 状态的死信作业。
- **排查与修复**：
  1. 查看当前队列状态：
     ```bash
     hippo hook status
     ```
  2. 预览待重试的死信作业：
     ```bash
     hippo hook retry --all-dead --dry-run
     ```
  3. 批量重试所有死信作业：
     ```bash
     hippo hook retry --all-dead
     ```
  4. 若需清理历史废弃作业（如已确认无法恢复的损坏数据）：
     ```bash
     # 预览 30 天前的死信作业
     hippo hook prune --state dead --days 30 --dry-run
     # 确认执行删除
     hippo hook prune --state dead --days 30 --force
     ```
  5. 清理完成后验证：
     ```bash
     hippo doctor
     ```

### 故障 5：Worker 常驻服务异常
- **现象**：`hippo doctor` 报告 `dev.hippo.worker` 已安装但未运行。
- **排查**：
  1. 查看 Worker 日志：
     ```bash
     tail -50 ~/.hippo/logs/worker.log
     ```
  2. 查看 LaunchAgent 状态详情：
     ```bash
     launchctl print gui/$(id -u)/dev.hippo.worker
     ```
  3. 重启 Worker 服务：
     ```bash
     hippo service restart worker
     ```
  4. 若问题持续，尝试卸载后重装：
     ```bash
     hippo service uninstall worker
     hippo service install worker
     hippo service status
     ```

---

## 3. Spool 队列运维速查

| 命令 | 用途 |
|------|------|
| `hippo hook status` | 查看队列统计与最近作业详情 |
| `hippo hook retry <job_id>` | 重试单个失败/跳过的作业 |
| `hippo hook retry --all-dead` | 批量重试所有死信作业 |
| `hippo hook retry --all-dead --dry-run` | 预览待重试的死信作业 |
| `hippo hook prune --state dead --days 7 --dry-run` | 预览 7 天前的死信作业 |
| `hippo hook prune --state all --days 30 --force` | 清理所有 30 天前的终态作业 |
| `hippo hook worker --drain` | 手动触发前台消费 |
| `hippo service install worker` | 安装 Worker 常驻守护 |
| `hippo service status` | 查看双服务运行状态 |
