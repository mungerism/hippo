# Hippo (海马体) 🦛

> 面向 AI 编码智能体（antigravity, Codex, ZCode, Zed AI, Cursor, pi 等）的统一长短期记忆中枢，基于 Mem0 与 MCP (Model Context Protocol) 打造。

> [!IMPORTANT]
> **项目重要原则（Core Philosophy）**：**Hippo 只是对 Mem0 的轻量工程封装（Thin Wrapper over Mem0）**。
> 1. **100% 规范对标**：所有 MCP 工具名（`add_memory`、`search_memories`、`get_memories` 等）与参数类型与 Mem0 官方规范完全一致，拒绝二次发明非标 API。
> 2. **专注工程边界**：Hippo 仅专注于多端 IDE 的 MCP 协议挂载、Git 目录自动路由（无需手动管理 `agent_id`）以及本地单二进制 Qdrant 常驻服务。

---

## 🌟 核心特性

- **跨 IDE/Agent 统一记忆**：通过标准 MCP (Model Context Protocol) 协议，无缝接入 **antigravity**、**Codex**、**ZCode**、**Zed AI**、**Cursor** 等开发工具。
- **两级作用域隔离**：
  - **全局习惯 (`global`)**：记录开发者的个人偏好（如 macOS 环境、Surge 代理设置、代码风格）。
  - **项目专有 (`project`)**：基于当前 Git 仓库自动隔离（架构决策、踩坑记录、技术选型）。
- **零外部服务依赖**：基于嵌入式 Qdrant 本地文件存储（`~/.hippo/storage/qdrant`），无需 Docker 或单独数据库守护进程。
- **多模型灵活适配**：支持 Google Gemini（`gemini-2.5-flash` + `text-embedding-004`，推荐）或 OpenAI（`gpt-4o-mini` + `text-embedding-3-small`）。
- **终端快捷 CLI**：提供 `hippo` 命令行工具，随时手工查阅、新增、删除与诊断。

---

## 🚀 快速开始

### 1. 配置 API Key

系统配置文件位于 `~/.hippo/.env`。请填入你的 Google Gemini 或 OpenAI API Key：

```bash
# 编辑配置文件
vim ~/.hippo/.env
```

例如使用 Google Gemini：
```env
HIPPO_PROVIDER=gemini
GOOGLE_API_KEY=AIzaSy...
GEMINI_LLM_MODEL=gemini-2.5-flash
GEMINI_EMBEDDING_MODEL=models/text-embedding-004
```

或使用 OpenAI：
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
```

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
        "/Users/munger/Code/Repos/Personal/hippo",
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
          "/Users/munger/Code/Repos/Personal/hippo",
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
        "/Users/munger/Code/Repos/Personal/hippo",
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
        "/Users/munger/Code/Repos/Personal/hippo",
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
args = ["--directory", "/Users/munger/Code/Repos/Personal/hippo", "run", "hippo-mcp"]
enabled = true
```

### 5. pi-coding-agent (pi)
扩展文件：`~/.pi/agent/extensions/hippo-memory.ts`
> 自动加载为原生工具：`search_memories`、`add_memory`、`get_memories`、`get_memory`、`delete_memory` 及 `/hippo` 快捷命令。


---

## 🛠 暴露的标准 MCP 工具集 (100% 对标 Mem0 官方规范)

任何接入的 Agent 均可调用与 Mem0 官方完全一致的标准工具：
- **`add_memory(text, ...)`**：沉淀新的个人偏好、技术规范或项目踩坑事实（支持多模态截图与 `messages` 会话记录）。
- **`search_memories(query, ...)`**：基于向量语义与多信号混合检索相关记忆（支持 `filters`、`limit`、`scope`）。
- **`get_memories(...)`**：列出记忆列表或全局偏好画像（支持结构化过滤与分页）。
- **`get_memory(memory_id)`**：根据记忆唯一 ID 获取单条事实的详细上下文与元数据。
- **`update_memory(memory_id, text, ...)`**：精确覆盖/更新某条已存在记忆的文本内容或元数据。
- **`delete_memory(memory_id)`**：根据记忆 ID 删除一条不再需要或过期的记忆。
- **`delete_all_memories(...)`**：在确认的作用域（用户/项目/智能体）内批量清理记忆。
- **`list_entities()`**：枚举当前记忆库中持久化的所有用户与项目/智能体实体。

---

## 📚 延伸阅读
- [Mem0 深度技术调研报告](./mem0_research.md)
