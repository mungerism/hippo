# 运维与排障手册 (Runbooks) 索引

运维手册（Runbooks）用于指导开发者与运维人员在日常部署、巡检维护、故障排查与数据迁移等场景下的具体操作步骤与排错路径。

---

## 📋 手册清单

| 手册分类 | 文档标题 | 适用场景 | 关键命令 / 工具 |
| :--- | :--- | :--- | :--- |
| **部署与常驻** | [Qdrant 本地常驻与部署运维](/runbooks/deployment) | 初次安装、LaunchAgent 服务托管、自愈管理 | `hippo service install/status` |
| **健康巡检** | [Doctor 巡检与环境排障](/runbooks/troubleshooting) | 端口未监听、凭据缺失、宿主集成异常排查 | `hippo doctor` |
| **数据迁移** | [记忆迁移与向量重建操作手册](/runbooks/migration) | 更换模型、升级向量维度、导入 Codex 记忆 | `hippo reindex`, `hippo migrate-codex` |
