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
4. **宿主集成**：Codex、Pi、ZCode 与 Antigravity 等可选集成的 Hook 与 MCP 静态配置状态；
5. **Spool 队列**：分别列出 v1/v2 的 pending、processing、completed、skipped、coalesced、dead、不可读记录与收据文件数，并探测各版本消费锁。

### 读懂巡检结果（#118）

Doctor 不再用 v1 队列代替全部队列。`Spool v1` 与 `Spool v2` 分别统计，消费锁表示本次采样是否有消费者持有该版本的锁；它不等同于 LaunchAgent 运行状态，也不证明后台模型请求成功。不存在的队列标为未启用，巡检不创建目录、作业、锁或收据，不重试作业。不可读状态单独报错，不把损坏记录算成健康的零积压。成功收据文件数不等于有效记忆数。

`hippo hook status` 仍默认 v1，但表格标题明确显示版本；部署使用 v2 时运行 `hippo hook status --queue-version 2`。死信提示包含正确的版本，只建议先运行 `hippo hook audit-dead --queue-version 2` 等只读审计，不建议直接批量重试受围栏保护的历史。

MCP 配置按结构读取：Codex 的 TOML `mcp_servers`、常见 JSON `mcpServers`、ZCode 的 `mcp.servers`、Zed 的 JSONC `context_servers`（含 command 对象）。支持直接 `hippo-mcp`、`hippo serve`、常见 uv/uvx 启动方式和 Python 模块入口；检查 enabled/disabled，忽略注释、说明文字或其他程序参数中的同名字符串。无法解析的配置不输出原始配置内容。

客户端和 Hook 都是可选集成，缺失或禁用不会被汇总为核心服务故障；配置错误会在相应集成行提示。自定义 shell 包装、未知启动参数或远程 MCP 连接需要单独验证，不会为了巡检执行配置中的命令。

巡检的几层证据须分别理解：

| 检查 | 能说明什么 | 不能说明什么 |
| --- | --- | --- |
| 端口 / collection | 本机 Qdrant 可访问、集合存在 | 远程模型可用、召回质量通过 |
| LaunchAgent | 托管进程运行或退出情况 | 该进程正在消费哪个队列 |
| 分版本消费锁 / 状态文件 | 当前消费者与队列元数据快照 | 持续吞吐、后续模型请求成功 |
| MCP 配置 / Hook / Pi 文件 | 静态入口启用、文件或指纹存在 | 当前客户端已重连、真实事件已捕获 |

汇总区将核心问题与可选集成提示分开。数据是非原子采样；活跃 Worker 可能在检查期间改变状态。真实 MCP 握手、调用和会话捕获仍需独立验收。

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

### 故障 6：模型提取响应解析失败

Warm Path 遇到 `DISTILLATION_RESPONSE_INVALID_JSON`、`DISTILLATION_RESPONSE_INVALID_SHAPE` 或 `DISTILLATION_RESPONSE_INVALID_ITEM` 时，不再把失败当作正常空提取完成。错误不包含原始模型响应；Worker 最多尝试 3 次，前两次分别退避 10 秒、20 秒，耗尽后保留 `dead` 作业供排查。合法的空事实列表、去重或持久化质量过滤后的空结果仍可正常完成。

检查时选择实际运行的队列版本，例如本机 v2：

```bash
hippo hook status --queue-version 2
hippo hook retry <job_id> --queue-version 2 --dry-run
```

确认模型输出恢复正常、项目身份可靠且会话未被围栏阻断后，才选择性重试相应死信。

历史 `completed` 空收据不能单独证明解析失败：它也可能是合法的空提取或质量过滤。先备份相关作业和收据，结合可关联的日志与转录审计，记录证据和身份；无法关联的项目保留“原因未知”。不得删除全部空收据、清除成功游标或直接重放全部空结果。当前 CLI 默认不允许重试 `completed`，确需恢复时应另行准备经过审查的逐作业迁移计划，保留审计与防重依据。

---

## 3. Spool 队列运维速查

### 上下文死信：先审计，再判断是否允许恢复

```bash
hippo hook audit-dead --queue-version 2
```

该命令只读取作业、会话围栏和本地项目配置，验证当前目录与冻结身份，不读取转录、不访问模型、不创建队列、不修改状态。输出包括脱敏的会话摘要、当前上下文状态、首次围栏原因及 `retry_preview`；不输出仓库路径、用户 ID 或对话内容。`manual_review` 仅表示通过了当前可检查的条件，仍须验证历史转录范围和故障是否已恢复，不能当作自动恢复授权。

- `session_blocked`：会话已被围栏保护，禁止清除围栏或重放完整历史。修复新会话的活动仓库来源；旧记录只有在另行证明安全分段与身份后才能设计迁移。
- `context_unresolved`：当前目录缺失、非 Git、来源歧义等，修复宿主上下文；不得回退服务 cwd、任选一个 workspace 或猜测项目。
- `identity_mismatch`、`identity_not_frozen`、`session_fence_unknown`：无法证明历史作业归属，保留现场，不用当前映射重新归属历史。
- `capture_disabled`、`capture_config_invalid`：停用或配置异常，不能通过重试绕过。
- `already_processed`、`tombstoned`：已有成功游标或清理记录，不重复投递。
- `unreadable_job_or_fence`：先排查损坏记录，不尝试自动恢复。

新发生的上下文拒绝会在作业状态中保留 `context_error_code`；首次会话围栏记录 `blocked_reason`、`blocked_job_id`、`blocked_at`，后续拒绝不会覆盖第一现场。旧记录没有这些证据时，报告 `LEGACY_CAUSE_UNKNOWN` / `LEGACY_FENCE_REASON_UNKNOWN`，不依据后续有效 cwd 反推首次阻断原因。

2026-11-02 本机排查（#117）：v2 共 373 个死信，其中 367 个上下文相关，涉及 Codex 85 个、Antigravity 8 个会话。按当前目录检查，139 个作业为非 Git 目录，5 个涉及已不存在的目录，223 个拥有有效 Git 仓库但会话已被围栏阻断。当前目录有效不能证明历史上下文始终安全；旧围栏未保留首次原因，无法完整还原。此快照不等同于宿主故障率，也不构成批量重试依据。

| 命令 | 用途 |
|------|------|
| `hippo hook audit-dead --queue-version 2` | 只读预览死信恢复条件与会话围栏，不执行重试 |
| `hippo hook status` | 查看队列统计与最近作业详情 |
| `hippo hook retry <job_id>` | 重试单个失败/跳过的作业 |
| `hippo hook retry --all-dead` | 批量重试所有死信作业 |
| `hippo hook retry --all-dead --dry-run` | 预览待重试的死信作业 |
| `hippo hook prune --state dead --days 7 --dry-run` | 预览 7 天前的死信作业 |
| `hippo hook prune --state all --days 30 --force` | 清理所有 30 天前的终态作业 |
| `hippo hook worker --drain` | 手动触发前台消费 |
| `hippo service install worker` | 安装 Worker 常驻守护 |
| `hippo service status` | 查看双服务运行状态 |
