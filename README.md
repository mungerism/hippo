# Hippo (海马体) 🦛

> 面向 AI 编码智能体（antigravity, Codex, ZCode, Zed AI, Cursor, pi 等）的统一长短期记忆中枢，基于 Mem0 与 MCP (Model Context Protocol) 打造。

> [!IMPORTANT]
> **项目重要原则（Core Philosophy）**：**Hippo 只是对 Mem0 的轻量工程封装（Thin Wrapper over Mem0）**。
> 1. **100% 规范对标**：所有暴露的 MCP 工具名（`search_memories`、`add_memory`）与参数类型与 Mem0 官方规范完全一致，专为自主 Agent 极致精简。
> 2. **专注工程边界**：Hippo 仅专注于多端 IDE 的 MCP 协议挂载、Git 目录自动路由（无需手动管理 `agent_id`）以及本地单二进制 Qdrant 常驻服务。

---

## 🌟 核心特性

- **跨 IDE/Agent 统一记忆**：通过标准 MCP (Model Context Protocol) 协议，无缝接入 **antigravity**、**Codex**、**ZCode**、**Zed AI**、**Cursor** 等开发工具。
- **两级作用域隔离**：
  - **全局习惯 (`global`)**：记录开发者的个人偏好（如 macOS 环境、Surge 代理设置、代码风格）。
  - **项目专有 (`project`)**：基于当前 Git 仓库自动隔离（架构决策、踩坑记录、技术选型）。
- **零外部服务依赖**：基于嵌入式 Qdrant 本地文件存储（`~/.hippo/storage/qdrant`），无需 Docker 或单独数据库守护进程。
- **多模型灵活适配**：支持 Google Gemini Developer API、Google Vertex AI（ADC）或 OpenAI；Vertex AI 可使用 `gemini-embedding-2`。
- **终端快捷 CLI**：提供 `hippo` 命令行工具，随时手工查阅、新增、删除与诊断。

---

## 🚀 快速开始

### 1. 配置模型 Provider

系统配置文件位于 `~/.hippo/.env`。

```bash
# 编辑配置文件
vim ~/.hippo/.env
```

使用 Google Gemini Developer API：
```env
HIPPO_PROVIDER=gemini
GOOGLE_API_KEY=AIzaSy...
GEMINI_LLM_MODEL=gemini-3.5-flash-lite
GEMINI_EMBEDDING_MODEL=models/gemini-embedding-2
```

使用 Google Vertex AI：

```bash
# 本地开发：配置 Application Default Credentials (ADC)
gcloud auth application-default login
```

```env
HIPPO_PROVIDER=vertexai
GOOGLE_CLOUD_PROJECT=your-gcp-project
GOOGLE_CLOUD_LOCATION=global
VERTEX_LLM_MODEL=gemini-3.5-flash-lite
VERTEX_EMBEDDING_MODEL=gemini-embedding-2
VERTEX_EMBEDDING_DIMS=768
```

Vertex AI 必须通过 `HIPPO_PROVIDER=vertexai` 显式启用，不参与 `auto` 检测，避免开发机上已有 ADC 时意外切换 Provider。生产环境建议使用运行环境绑定的 Service Account / ADC，而不是保存长期凭证文件。

`gemini-embedding-2` 在 Vertex AI 上通过 `google-genai` 的 `embedContent` API 调用。Google 对该模型不支持 `task_type` 字段，因此 Hippo 会按照检索场景将任务说明写入输入文本：搜索 query 使用 `task: search result | query: ...`，记忆文档使用 `title: none | text: ...`。

> [!NOTE]
> 不同 embedding Provider / 模型产生的向量空间不能直接混用。为兼容已有数据，Gemini/OpenAI 仍沿用历史 `hippo_memories` collection；Vertex AI 默认使用独立的 `hippo_memories_vertexai_<model>_<dims>` collection。可通过 `HIPPO_COLLECTION_NAME` 显式覆盖，但只有在确认向量空间兼容时才应复用旧 collection。切换已有记忆到新的 embedding 模型仍需要后续 reindex / migration。

使用 OpenAI：
```env
HIPPO_PROVIDER=openai
OPENAI_API_KEY=sk-...
OPENAI_LLM_MODEL=gpt-4o-mini
OPENAI_EMBEDDING_MODEL=text-embedding-3-small
```

---

## 💻 命令行 CLI 指南

可以在终端使用 `hippo`（或在工程根目录下使用 `uv run hippo`）：

```bash
# 查看当前状态与存储路径
hippo status

# 1. 添加一条全局个人偏好
hippo add "我平时在 Mac 上开发，网络使用 Surge 代理" --global

# 2. 添加一条当前工程专有记忆（自动绑定当前 Git 仓库）
hippo add "本项目后端采用 FastAPI，包管理器强制使用 uv"

# 3. 语义检索记忆 (范围: all | global | project)
hippo search "包管理器"

# 4. 列出所有记忆
hippo list

# 5. 查看全局偏好画像
hippo profile

# 6. 删除指定记忆
hippo delete <memory_id>

# 7. 启动 MCP Server (stdio 模式)
hippo serve

# 8. 自动生命周期 Hook 与异步 Spool 队列管理
hippo hook status                     # 查看 Spool 队列各状态作业与积压流水
hippo hook worker --drain             # 立即单次排他消费当前就绪的待办作业
hippo hook worker --daemon            # 以常驻守护进程持续消费 Spool 作业
hippo hook retry <job_id>             # 将死信 (dead) 作业重置重新入队
```

---

## 🧠 会话记忆蒸馏流水线 (Session Distillation)

Hippo 采用**双事件容灾 (Dual-Event Resilience) 与异步 Spool 状态机**机制：
- **认知面与系统面物理隔离**：MCP Server 面向智能体保持绝对纯粹，仅保留 `search_memories` 与 `add_memory`（限制单句事实 2000 字符），杜绝 Agent 尝试序列化完整长会话历史；
- **双事件容灾挂载**：在宿主切面以 `Stop`（一轮交互完成）为主检查点防丢，以 `SessionEnd`（会话结束）为对账兜底；
  - **Codex & pi**：启用完整双事件模式；
  - **ZCode & Antigravity**：按宿主原生能力以 Stop-only 模式运行；
- **极速 Spool 入队**：Hook 在 `< 50ms` 内原子占位入队并 `exit 0` 返回宿主，绝不卡顿终端交互；
- **语义游标防重 (Semantic Cursor)**：基于目标与最终陈述哈希，跨事件瞬时拦截重复投递，消灭关闭日志追加导致的游标漂移；
- **免维护自愈**：后台单 Worker 进程利用 `worker.lock` 互斥消费，具备 Lease 超时回收、3次退避重试与死信保护。


---

## 🔌 多客户端集成配置 (MCP)

系统已为各大客户端完成了配置，只要客户端运行，就会自动启动并挂载 `hippo-memory` MCP 服务：

### 1. antigravity
配置文件：`~/.gemini/config/mcp_config.json`
```json
{
  "mcpServers": {
    "hippo-memory": {
      "command": "/opt/homebrew/bin/uv",
      "args": [
        "--directory",
        "/path/to/hippo",
        "run",
        "hippo-mcp"
      ]
    }
  }
}
```

### 2. ZCode (智谱 AI)
配置文件：`~/.zcode/cli/config.json`
```json
{
  "mcp": {
    "servers": {
      "hippo-memory": {
        "type": "stdio",
        "command": "/opt/homebrew/bin/uv",
        "args": [
          "--directory",
          "/path/to/hippo",
          "run",
          "hippo-mcp"
        ]
      }
    }
  }
}
```

### 3. Zed AI
配置文件：`~/.config/zed/settings.json`
```json
{
  "context_servers": {
    "hippo-memory": {
      "command": "/opt/homebrew/bin/uv",
      "args": [
        "--directory",
        "/path/to/hippo",
        "run",
        "hippo-mcp"
      ]
    }
  }
}
```

### 3. Cursor
配置文件：`~/.cursor/mcp.json`
```json
{
  "mcpServers": {
    "hippo-memory": {
      "command": "/opt/homebrew/bin/uv",
      "args": [
        "--directory",
        "/path/to/hippo",
        "run",
        "hippo-mcp"
      ]
    }
  }
}
```

### 4. Codex
配置文件：`~/.codex/config.toml`
```toml
[mcp_servers.hippo-memory]
command = "/opt/homebrew/bin/uv"
args = ["--directory", "/path/to/hippo", "run", "hippo-mcp"]
enabled = true
```

### 5. pi-coding-agent (pi)
扩展文件：`~/.pi/agent/extensions/hippo-memory.ts`
> 自动加载为原生工具：`search_memories`、`add_memory` 及 `/hippo` 快捷命令。

---

## 🧠 提升记忆触发率与自动化蒸馏：`hippo init`

MCP 记忆工具由模型自主决定何时调用（各客户端均无会话开始自动检索）。Hippo 已通过 server instructions 与工具描述声明调用时机；若需在指令层进一步固化"开始任务前先检索记忆"，并自动配置各 IDE 的生命周期 Hook，运行：

```bash
hippo init
```

它会幂等地执行两部分配置（重复执行无副作用）：
1. **记忆检索约定**：写入 `~/.codex/AGENTS.md`（Codex 全局指令）与当前 Git 根目录的 `AGENTS.md`（ZCode、antigravity、pi 等按工作区读取），可用 `--skip-global` / `--skip-project` 控制；
2. **生命周期 Hook 与插件**：自动在 Codex (`~/.codex/hooks.json`)、ZCode (`~/.zcode/cli/config.json`)、Antigravity (`~/.gemini/config/hooks.json`) 挂载 Stop / SessionEnd 异步蒸馏切面，并安装/更新 pi 扩展；可用 `--no-hooks` 跳过。

---

## 🛠 暴露的标准 MCP 工具集 (专为自主 Agent 极致精简的安全子集)

根据 Mem0 官方对自主智能体的最佳实践，Hippo MCP 专为 Agent 暴露两个最纯粹的核心记忆工具，记忆演化与冲突消解全自动处理：
- **`search_memories(query, ...)`**：基于向量语义与多信号混合检索相关记忆（支持 `filters`、`limit`、`scope`）。
- **`add_memory(text, ...)`**：沉淀新的个人偏好、技术规范或项目踩坑事实（单句事实 `text`，限制 2000 字符，支持本地截图图片路径）。长多轮会话记忆由后台异步 Hook 蒸馏流水线接管，避免 Agent 序列化全量上下文导致 Token 爆炸。底层由 Mem0 自动判定新增、覆盖更新或消除冲突。

> 💡 **提示**：`list`、`get`、`update`、`delete`、`clear` 等确定性生命周期管理操作由面向人类开发者的 **Hippo CLI** 全权提供，避免 Agent 产生 UUID 幻觉或误操作。

---

## 🩺 本地服务运维（service / doctor）

```bash
# 将 Qdrant 升级为 LaunchAgent 常驻服务（RunAtLoad + KeepAlive 崩溃自拉起）
hippo service install

# 查看服务状态 / 卸载（卸载后回到按需拉起模式）
hippo service status
hippo service uninstall

# 一键巡检：Qdrant 连通、collection、Provider 凭证/ADC、服务状态、5 家客户端接入、依赖版本
hippo doctor
```

服务未安装时，Hippo 沿用按需拉起模式：任何命令运行前自动探测 6333 端口，未监听则拉起 `~/.hippo/bin/qdrant`。

---

## 📚 延伸阅读
- [Mem0 深度技术调研报告](./mem0_research.md)
