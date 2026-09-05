# Hippo (海马体) 🦛

> 面向 AI 编码智能体（Anti-Gravity, Codex, Z-code 等）的统一长短期记忆中枢，基于 Mem0 与 MCP (Model Context Protocol) 打造。

---

## 🌟 核心特性

- **跨 IDE/Agent 统一记忆**：通过标准 MCP (Model Context Protocol) 协议，无缝接入 **Anti-Gravity**、**Codex**、**Cursor**、**Zed AI (Z-code)** 等开发工具。
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

### 1. Anti-Gravity
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

### 2. Zed AI (Z-code)
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

---

## 🛠 暴露的 MCP 工具集

任何接入的 Agent 都可以调用以下工具：
- `search_memory(query, scope='all', limit=5)`: 混合检索历史事实
- `save_memory(content, scope='project', image_path=None)`: 保存新事实（支持多模态图片）
- `get_user_profile()`: 一键拉取用户全局偏好画像
- `list_memories(scope='all', limit=20)`: 列出持久化记忆
- `delete_memory(memory_id)`: 删除失效记忆

---

## 📚 延伸阅读
- [Mem0 深度技术调研报告](./mem0_research.md)
