# [ADR-0009] MCP 按调用方上下文解析项目

- **状态 (Status)**: 已通过 (Accepted)
- **日期 (Date)**: 2026-10-14
- **关联**: [Issue #93](https://github.com/mungerism/hippo/issues/93)

MCP 服务的启动目录可能是 Hippo 安装仓库，而客户端聊天属于其他仓库。旧实现把省略的项目选择器交给 `ScopeRouter`，最终使用服务进程工作目录，导致搜索与写入选错项目；`scope='project'` 只能限制已选身份，无法纠正身份来源。

MCP 网关统一使用显式 `project_id`，或本次请求所属客户端通过 `roots/list` 提供的唯一 Git 仓库身份。保留搜索的旧 `agent_id` 优先覆盖语义，但其路径选择器与 `project_id` 一样规范化为 Git 仓库名。根 URI 必须指向本机存在的绝对目录，使用 Git 仓库根路径判断唯一性，显示名称不参与路由；同仓库的多个子目录可以合并为一个身份，同名但不同路径的两个仓库仍属于歧义。每次调用重新读取 roots，不在全局引擎或会话中缓存项目，也不使用服务目录、环境变量或 `default_project` 兜底。

缺失 roots 能力、空列表、多仓库、不可访问/非 Git 目录、请求异常或超过 2 秒未返回时，在接触后端之前返回可操作且不含路径或客户端异常正文的错误，提示显式选择项目。搜索保留不可信上下文信封，写入保留结构化错误。`scope='global'`（或搜索 `agent_id='global'`）无需项目上下文；`all` 必须解析出一个项目，不能静默退化为全局搜索。语义搜索、时间线查询和显式写入共享此契约。

MCP 的自动 roots 和显式路径检测均以过滤掉全部 `GIT_*` 变量的子进程环境执行 Git fallback，避免服务继承的 `GIT_DIR`、`GIT_WORK_TREE`、`GIT_COMMON_DIR` 或 Git 配置变量将非 Git 客户端目录重定向到其他仓库。过滤仅作用于该 Git 子进程，不修改共享服务的 `os.environ`，也不改变 CLI/SDK/Hook 默认继承 Git 环境的行为。

拒绝沿用服务工作目录、默认取第一个 root 或缓存最近使用项目：这些方式兼容旧客户端，但会在跨仓库部署或共享服务时静默选错身份。代价是没有可靠 roots 的客户端须传入 `project_id`，因此给 `add_memory` 增加这一安全选择器；身份和预算相关的底层参数仍不向写入工具开放。CLI、SDK 和 Hook 的缺省本地目录路由保持原有契约。共享 `ScopeRouter` 将显式项目名作为命名空间，只有含路径分隔符、绝对路径或 `.` / `..` 的选择器参与目录检测，避免已解析名称因服务目录下存在同名子目录而被再次路由。项目命名空间继续沿用现有仓库名约定，不迁移历史记忆，也不在本次修复引入新的命名空间格式。

实现依赖 MCP SDK 的请求级 Context 和 roots 能力；不提供该能力的协议/传输通过显式项目选择器使用服务。参考 [MCP roots 规范](https://modelcontextprotocol.io/specification/2025-06-18/client/roots)。客户端 roots 表示其声明的工作区，不是用户鉴权凭据，也无法推断多个工作区中当前聊天选中了哪个。

## 2026-10-15：自动宿主上下文的受控扩展

[RFC-0003](/rfcs/0003-automatic-project-context) 的实现扩展了上述来源限制：允许明确启用的 Codex 请求 metadata、本地专属 stdio 启动时捕获的 `CLAUDE_PROJECT_DIR`，以及运维者传入的 `serve --project-root`。Claude 环境在 dotenv 载入前固定；普通仓库环境变量、服务 cwd 与最近项目不作为 MCP 来源。存在多个 roots 时，可信候选必须属于其中一个仓库；没有候选则 roots 必须唯一，各来源冲突时失败。

Pi 工具通过回调 `ctx.cwd` 调用共用安全桥；Hook 保留完整候选，缺失或歧义时不能取进程 cwd 或首个工作区。新增 opt-in canonical 模式，以本地 Git common directory 为身份、显式映射保留历史空间、未绑定仓库使用完整哈希。默认保持 legacy；启用前须执行队列切换与历史绑定屏障，详见[运维手册](/runbooks/project-context)。这些规则取代本文原先对来源和命名空间的绝对限制，不改变请求隔离、scope、预算和 Mem0 存储契约。

本机已于 2026-10-15 完成上述切换及四宿主真实召回验收，见[发布记录](/runbooks/project-context-acceptance)。Antigravity 的 stateless 连接会明确抛出 SDK `NoBackChannelError`；仅将这一状态视为 roots 不可用，继续要求有效独立上下文。普通 roots 异常、超时、空列表或冲突仍然失败。该兼容修订不改变软件默认开关。

## 2026-10-16：Antigravity 连接归属验收失败

[扩展验收](/runbooks/project-context-extended-acceptance)发现 Antigravity 2.23.1 在两个 Project 间复用同名 MCP 连接，B 查询返回 A 的测试身份；未执行 B 写入。不同服务名未被发现，尚无可用的已验证绕行。本机已获授权移除固定 root，回到显式 `project_id`。专属部署绑定能力保留，但只有实际连接独占经 A/B 并发、切换和重连验证后才能启用。此结论收窄前次单项目验收范围，不放宽解析器的失败门禁。

## 2026-10-16：无回传通道不能仅凭启动绑定路由

[Issue #101](https://github.com/mungerism/hippo/issues/101)的原生探针进一步观察到：Antigravity 请求可不声明 roots，SDK `can_send_request=false`；metadata 只有会话、产物目录和协议字段，没有活动仓库。先前仅捕获 `NoBackChannelError` 的兼容处理不足以覆盖此分支。

MCP 网关因此在两处拒绝仅有启动绑定的自动项目调用：公开通道能力明确为 false 时，以及实际 roots 请求抛出 `NoBackChannelError` 时。返回 `PROJECT_CONTEXT_UNVERIFIED`，不进入任何记忆后端；显式选择与 global 保留。已启用且合法的请求级 Codex metadata 仍可参与解析，但所有来源必须一致。此规则取代前述 stateless 场景下“静态绑定单独足够”的解释，不按客户端名称或版本做特殊放行，也不从会话 ID / artifacts 目录猜测项目。

这是一项防止错误读写的安全修复，不修复宿主自身的连接池或恢复 Antigravity 免选择器路由。没有可靠活动目录的当前宿主仍使用显式项目选择；工作区唯一服务名也需独立实测，不能据配置存在宣称已支持。
