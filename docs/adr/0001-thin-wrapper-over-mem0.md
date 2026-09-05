# 0001. Hippo 只是对 Mem0 的轻量工程封装 (Thin Wrapper over Mem0)

**状态**: accepted  
**日期**: 2026-09-05  

## 背景与上下文 (Context)
在多 AI 编码智能体（antigravity、Codex、ZCode、Zed AI、Cursor、pi）日常开发中，需要一个统一的跨 IDE 记忆中枢。我们面临两个选择：自行从零设计一套长时记忆中枢，或者深度改造封装第三方框架。然而，Mem0 官方已经具备优秀且经过验证的事实提取、知识图谱与语义索引能力。

## 决策 (Decision)
我们决定 **将 Hippo 明确定义为 Mem0 的轻量级工程封装（Thin Wrapper）**：
1. **接口对标**：所有 MCP Server 工具名、参数签名与数据结构必须 100% 对齐 Mem0 官方规范（`add_memory`、`search_memories`、`get_memories` 等）。
2. **拒绝二次发明**：严禁在 Hippo 内部自行发明非标的自定义记忆 API 或深重抽象层。
3. **专注工程落地**：Hippo 的职责严格限定于：
   - 多端 IDE 的 MCP stdio 协议桥接。
   - 基于本地 Git 目录的智能作用域路由（自动隔离项目级与全局记忆）。
   - 本地常驻单二进制 Qdrant Server 解决文件锁并发痛点。
   - 人类友好的极简命令行 CLI (`hippo`)。

## 收益与影响 (Consequences)
- **零心智负担**：Agent 与人类开发者可直接复用 Mem0 官方文档、官方 Prompts 与生态工具。
- **极低升级成本**：当 Mem0 升级时，Hippo 可以无缝跟进最新特性，无架构包袱。
