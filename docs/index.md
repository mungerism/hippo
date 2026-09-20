---
layout: home

hero:
  name: "🦛 Hippo"
  text: "跨 IDE 统一记忆中枢"
  tagline: "对齐官方规范，补齐开源短板：为 AI 编码智能体打造的轻量持久记忆引擎"
  actions:
    - theme: brand
      text: 快速起步
      link: /runbooks/deployment
    - theme: alt
      text: 架构总览
      link: /architecture/
    - theme: alt
      text: 架构决策 (ADR)
      link: /adr/
    - theme: alt
      text: 运维手册
      link: /runbooks/

features:
  - icon: ⚡
    title: Hot Path 显式直写
    details: 面向 Agent 显式交互提供 <200ms 的亚秒级纳秒直写，MCP 工具安全收口，杜绝写入阻塞。
  - icon: 🔄
    title: Warm Path 异步蒸馏
    details: 基于双事件容灾与轻量 Spool 队列，跨 Codex / Pi / ZCode / Antigravity 自动化捕获上下文并蒸馏事实。
  - icon: 🧠
    title: Cold Path 记忆治理
    details: 离线分析候选记忆簇，自动识别等价合并、时间冲突收敛与过期计划废弃，保证长期记忆的一致性。
  - icon: 🛡️
    title: 向量空间隔离与安全围栏
    details: 严格按 Provider/Model/Dimension 隔离 Qdrant 集合，落实 Memory = Context not Policy，杜绝 Prompt 逃逸。
---

## 🏛️ 核心架构与数据流 {#core-architecture}

```mermaid
flowchart TD
    subgraph HOSTS ["多宿主适配层 (Host Integration)"]
        H1["Codex CLI"]
        H2["Pi Agent"]
        H3["ZCode"]
        H4["Antigravity / Cursor"]
    end

    subgraph INTERFACE ["协议与接口层 (Interface Layer)"]
        I1["MCP Server (add / search)"]
        I2["Terminal CLI (hippo-cli)"]
        I3["Lifecycle Hooks"]
    end

    subgraph GATEWAY ["网关与治理层 (Gateway & Governance)"]
        G1["Hot Path 直写网关 (亚秒级)"]
        G2["Warm Path 异步蒸馏 (Spool)"]
        G3["Cold Path 离线合并治理"]
        G4["检索安全门禁 (Context Envelopes)"]
        G5["向量 Profile 隔离解析"]
    end

    subgraph CORE ["核心引擎层 (Core Engine)"]
        C1["HippoEngine (多作用域路由 / 并发锁)"]
        C2["EmbeddingMigrator (向量断点迁移)"]
        C3["Mem0 记忆核心封装"]
    end

    subgraph STORAGE ["物理存储层 (Storage Layer)"]
        S1["Qdrant 向量数据库 (127.0.0.1:6333)"]
        S2["SQLite 历史版本存储 (history.db)"]
        S3["Spool 队列持久化 (spool.db)"]
    end

    HOSTS --> INTERFACE
    INTERFACE --> GATEWAY
    GATEWAY --> CORE
    CORE --> STORAGE
```

<div style="margin-top: 3rem; text-align: center;">
  <p style="color: var(--vp-c-text-2); font-size: 0.95rem;">
    Hippo 遵循 100% 本地优先原则，基于本地单二进制 Qdrant (127.0.0.1:6333) 与 uv / pnpm 极简依赖运行。
  </p>
</div>
