```mermaid
flowchart TD
    subgraph HOSTS ["多宿主适配层 (Host Integration Layer)"]
        H1["Codex CLI"]
        H2["Pi Agent"]
        H3["ZCode"]
        H4["Antigravity / Cursor"]
    end

    subgraph INTERFACE ["协议与接口层 (Interface Layer)"]
        I1["MCP Server (add_memory, search_memories)"]
        I2["Terminal CLI (hippo add/search/reindex/consolidate)"]
        I3["Lifecycle Hooks (hippo hook capture)"]
    end

    subgraph GATEWAY ["网关与策略治理层 (Gateway & Governance Layer)"]
        G1["Hot Path 直写网关 (infer=False / ≤200ms)"]
        G2["Warm Path 异步蒸馏 (Spool Worker / infer=True)"]
        G3["Cold Path 离线合并 (Memory Consolidator)"]
        G4["检索安全门禁 (Untrusted Envelope / Relevance Gate)"]
        G5["向量 Profile 隔离解析 (EmbeddingProfile)"]
    end

    subgraph CORE ["核心引擎层 (Core Engine Layer)"]
        C1["HippoEngine (多作用域路由 / 并发写锁)"]
        C2["EmbeddingMigrator (跨模型/维度断点续传迁移)"]
        C3["Mem0 Memory 核心封装"]
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
