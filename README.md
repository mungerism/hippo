# Hippo 🦛

<p align="left">
  <b>English</b> | <a href="README_zh.md">简体中文</a>
</p>

[![CI](https://github.com/mungerism/hippo/actions/workflows/ci.yml/badge.svg)](https://github.com/mungerism/hippo/actions/workflows/ci.yml)
[![License: Apache-2.0](https://img.shields.io/badge/License-Apache_2.0-blue.svg)](https://opensource.org/licenses/Apache-2.0)
[![Python 3.12+](https://img.shields.io/badge/python-3.12+-blue.svg)](https://www.python.org/downloads/)
[![Docs](https://img.shields.io/badge/docs-GitHub_Pages-brightgreen.svg)](https://mungerism.github.io/hippo/)

> A unified long-term and short-term memory hub for AI coding agents (antigravity, Codex, ZCode, Zed AI, Cursor, pi, etc.), powered by Mem0 and MCP (Model Context Protocol).

> [!IMPORTANT]
> **Core Tenet**: **Hippo is a lightweight engineering wrapper over Mem0.**
> 1. **100% Spec Alignment**: The MCP tools exposed to agents (`search_memories`, `add_memory`) strictly match official Mem0 naming and parameter semantics.
> 2. **Focused Engineering Boundaries**: Hippo focuses strictly on multi-IDE MCP mounting, automatic Git repository routing (eliminating manual `agent_id` maintenance), and local single-binary Qdrant daemon management.

---

## 🌟 Key Features

- **Cross-IDE / Agent Unified Memory**: Connect seamlessly to **antigravity**, **Codex**, **ZCode**, **Zed AI**, **Cursor**, and **pi-coding-agent** via standard Model Context Protocol (MCP).
- **Two-Tier Scope Isolation**:
  - **Global Habits (`global`)**: Developer-wide personal preferences (e.g., macOS dev environment, proxy configs, coding styles).
  - **Project Context (`project`)**: Automatically isolated by the current Git repository root (architecture decisions, tech stacks, debugging notes).
- **Zero External Service Dependencies**: Uses embedded local Qdrant storage (`~/.hippo/storage/qdrant`), requiring no Docker or dedicated database servers.
- **Flexible Model Providers**: Built-in support for Google Gemini Developer API, Google Vertex AI (ADC authentication, supports `gemini-embedding-2`), and OpenAI.
- **Fast Developer CLI**: Powerful `hippo` CLI for manual memory queries, profile inspection, status checks, and troubleshooting.
- **Asynchronous Session Distillation**: Dual-event resilience (`Stop` + `SessionEnd`) with a lightweight spool queue (`< 50ms` hot path) and background worker consolidation.
- **Docs as Code**: Full documentation site powered by VitePress with architecture blueprints, ADRs, RFCs, and runbooks: [mungerism.github.io/hippo](https://mungerism.github.io/hippo/).

---

## 📚 Documentation (Docs as Code)

All design decisions, architecture models, and operations runbooks live in [`docs/`](docs/):

- **Live Documentation**: 🌐 [https://mungerism.github.io/hippo/](https://mungerism.github.io/hippo/)
- **System Architecture**: [`docs/architecture/`](docs/architecture/) (Layered data flow and host contract matrix)
- **Architecture Decision Records (ADR)**: [`docs/adr/`](docs/adr/) (Core technical decisions ADR-0001 to 0006)
- **RFC Proposals**: [`docs/rfcs/`](docs/rfcs/) (Feature designs and adoption gates)
- **Runbooks**: [`docs/runbooks/`](docs/runbooks/) (Deployment, operations, doctor, and migration guides)
- **Knowledge Base**: [`docs/knowledge/`](docs/knowledge/) (Deep dives into Mem0, Codex, session distillation, and benchmarks)
- **Agent Guidelines**: [`docs/agents/`](docs/agents/) (AI Agent collaboration contracts and memory conventions)

### Local Development & Preview
```bash
# Install docs dependencies
pnpm --dir docs install

# Start local hot-reload dev server
pnpm --dir docs run dev

# Run build verification
pnpm --dir docs run build
```

---

## 🚀 Quick Start

### 1. Configuration & Providers

The configuration file resides at `~/.hippo/.env`.

```bash
cp .env.example ~/.hippo/.env
vim ~/.hippo/.env
```

**Using Google Gemini Developer API:**
```env
HIPPO_PROVIDER=gemini
GOOGLE_API_KEY=AIzaSy...
GEMINI_LLM_MODEL=gemini-3.5-flash-lite
GEMINI_EMBEDDING_MODEL=models/gemini-embedding-2
```

**Using Google Vertex AI:**
```bash
# Authenticate via Application Default Credentials (ADC)
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

**Using OpenAI:**
```env
HIPPO_PROVIDER=openai
OPENAI_API_KEY=sk-...
OPENAI_LLM_MODEL=gpt-4o-mini
OPENAI_EMBEDDING_MODEL=text-embedding-3-small
```

---

## 💻 CLI Usage

You can use the `hippo` CLI in your terminal (or `uv run hippo` from source):

```bash
# Check current system status and storage paths
hippo status

# 1. Add a cross-project global preference
hippo add "I develop on macOS and use Surge proxy" --global

# 2. Add a project-specific memory (automatically tagged with current Git repo)
hippo add "This project uses FastAPI with uv as package manager"

# 3. Search memories semantically (scope: all | global | project)
hippo search "package manager"

# 4. Inspect recent memory timeline (ADD / UPDATE / DELETE)
hippo recent --today
hippo recent --hours 48 --scope project

# 5. List all memories
hippo list

# 6. View synthesized user profile
hippo profile

# 7. Delete a memory by ID
hippo delete <memory_id>

# 8. Start MCP Server (stdio mode)
hippo serve

# 9. Spool queue management & worker operations
hippo hook status                     # View spool queue jobs and backlog
hippo hook worker --drain             # Consume all ready jobs immediately
hippo hook worker --daemon            # Run background worker daemon
hippo hook retry <job_id>             # Requeue a dead-letter job
```

---

## 🔌 IDE & Client Integration (MCP)

System configurations for major clients. You can run directly via `uvx` or specify your local source clone:

> 💡 **Tip**: Replace `/path/to/hippo` with the absolute path to your cloned repository, or run `uv tool install hippo-memory` to use `hippo-mcp` directly.

### 1. antigravity
Config path: `~/.gemini/config/mcp_config.json`
```json
{
  "mcpServers": {
    "hippo-memory": {
      "command": "uv",
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

### 2. ZCode
Config path: `~/.zcode/cli/config.json`
```json
{
  "mcp": {
    "servers": {
      "hippo-memory": {
        "type": "stdio",
        "command": "uv",
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
Config path: `~/.config/zed/settings.json`
```json
{
  "context_servers": {
    "hippo-memory": {
      "command": "uv",
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

### 4. Cursor
Config path: `~/.cursor/mcp.json`
```json
{
  "mcpServers": {
    "hippo-memory": {
      "command": "uv",
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

### 5. Codex
Config path: `~/.codex/config.toml`
```toml
[mcp_servers.hippo-memory]
command = "uv"
args = ["--directory", "/path/to/hippo", "run", "hippo-mcp"]
enabled = true
```

### 6. pi-coding-agent (pi)
Extension path: `~/.pi/agent/extensions/hippo-memory.ts`
> Loaded as native tools: `search_memories`, `add_memory`, and `/hippo` slash command.

---

## 🧠 Automated Setup & Hooks: `hippo init`

Run `hippo init` to idempotently configure retrieval guidelines and lifecycle hooks:

```bash
hippo init
```

This performs two idempotent setups:
1. **Memory Retrieval Convention**: Injects standardized markdown markers into `~/.codex/AGENTS.md` (global) and repository root `AGENTS.md` (workspace-level for ZCode, antigravity, pi);
2. **Lifecycle Hooks & Plugins**: Injects asynchronous Stop / SessionEnd hooks into Codex (`~/.codex/hooks.json`), ZCode (`~/.zcode/cli/config.json`), and Antigravity (`~/.gemini/config/hooks.json`), and installs/updates the pi extension.

---

## 🛠 Standard MCP Tools

Hippo MCP exposes two minimal, secure tools tailored for autonomous agents:
- **`search_memories(query, ...)`**: Hybrid vector and keyword retrieval with safety gate. Automatically switches to bounded timeline retrieval when explicit temporal queries ("today", "yesterday", "last 48 hours") are detected.
- **`add_memory(text, ...)`**: Saves single-sentence facts, preferences, or rules (max 2000 chars, supports local image paths). Multi-turn session context is handled by asynchronous background distillation to avoid token explosions.

---

## 🩺 Service & Doctor Diagnostics

```bash
# Install Qdrant as a LaunchAgent background service (auto-restart on crash)
hippo service install

# Check service status or uninstall
hippo service status
hippo service uninstall

# System doctor: check Qdrant connectivity, collection health, provider credentials, and 5 IDE MCP integrations
hippo doctor
```

---

## 📚 Further Reading
- [Mem0 In-depth Research Report](docs/knowledge/mem0-deep-dive.md)

---

## 📜 License

This project is licensed under the [Apache License 2.0](LICENSE).
