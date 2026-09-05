import type { ExtensionAPI } from "@earendil-works/pi-coding-agent";
import { Type } from "typebox";
import { execFile } from "node:child_process";
import { promisify } from "node:util";

const execFileAsync = promisify(execFile);
const HIPPO_BIN = "/Users/munger/Code/Repos/Personal/hippo/.venv/bin/hippo";

export default function (pi: ExtensionAPI) {
  // 1. 语义与混合记忆检索工具 (Mem0 标准: search_memories)
  pi.registerTool({
    name: "search_memories",
    label: "Mem0 Search Memories",
    description:
      "在持久化记忆中枢中检索与当前任务相关的偏好、项目历史架构决策与避坑指南 (完全兼容 Mem0 官方 search_memories 规范)。",
    promptSnippet: "检索全局偏好或项目历史架构与经验事实",
    parameters: Type.Object({
      query: Type.String({
        description: "检索关键词或自然语言问题，例如 '技术栈选型' 或 'Surge 代理'",
      }),
      scope: Type.Optional(
        Type.String({
          description: "检索范围: 'all'(默认，同时检索全局和当前项目) | 'global'(仅全局习惯) | 'project'(仅当前项目)",
        })
      ),
      limit: Type.Optional(
        Type.Number({
          description: "返回的最大条数，默认 5",
        })
      ),
      project: Type.Optional(
        Type.String({ description: "可选指定项目名，默认自动根据当前 Git 仓库探测" })
      ),
    }),
    async execute(_toolCallId, params) {
      try {
        const args = ["search", params.query];
        if (params.scope) args.push("--scope", params.scope);
        if (params.limit) args.push("--limit", String(params.limit));
        if (params.project) args.push("--project", params.project);

        const { stdout } = await execFileAsync(HIPPO_BIN, args);
        return {
          content: [{ type: "text", text: stdout.trim() || "未找到相关记忆。" }],
          details: {},
        };
      } catch (err: any) {
        return {
          content: [{ type: "text", text: `检索记忆失败: ${err.message}` }],
          details: { error: String(err) },
        };
      }
    },
  });

  // 2. 沉淀新记忆工具 (Mem0 标准: add_memory)
  pi.registerTool({
    name: "add_memory",
    label: "Mem0 Add Memory",
    description:
      "向持久化记忆中枢沉淀新的个人习惯、技术选型或项目避坑经验 (完全兼容 Mem0 官方 add_memory 规范)。",
    promptSnippet: "沉淀新的个人习惯、技术选型或项目避坑经验到长时记忆",
    parameters: Type.Object({
      text: Type.String({ description: "要记录的事实经验、技术规范或个人偏好" }),
      scope: Type.Optional(
        Type.String({
          description: "存储范围: 'project'(默认，当前项目) | 'global'(个人跨项目全局习惯)",
        })
      ),
      project: Type.Optional(
        Type.String({ description: "可选指定项目名，默认自动关联当前 Git 仓库" })
      ),
    }),
    async execute(_toolCallId, params) {
      try {
        const args = ["add", params.text];
        if (params.scope === "global") {
          args.push("--global");
        } else if (params.project) {
          args.push("--project", params.project);
        }

        const { stdout } = await execFileAsync(HIPPO_BIN, args);
        return {
          content: [{ type: "text", text: stdout.trim() }],
          details: {},
        };
      } catch (err: any) {
        return {
          content: [{ type: "text", text: `保存记忆失败: ${err.message}` }],
          details: { error: String(err) },
        };
      }
    },
  });

  // 3. 列出记忆列表/画像工具 (Mem0 标准: get_memories)
  pi.registerTool({
    name: "get_memories",
    label: "Mem0 Get Memories",
    description:
      "列出指定作用域内的记忆列表或全局偏好画像 (完全兼容 Mem0 官方 get_memories 规范)。",
    promptSnippet: "获取指定作用域的记忆列表或全局开发偏好",
    parameters: Type.Object({
      scope: Type.Optional(
        Type.String({
          description: "获取范围: 'all'(默认) | 'global'(全局偏好画像) | 'project'(当前项目)",
        })
      ),
      limit: Type.Optional(
        Type.Number({
          description: "最大返回条数，默认 20",
        })
      ),
      project: Type.Optional(
        Type.String({ description: "可选指定项目名" })
      ),
    }),
    async execute(_toolCallId, params) {
      try {
        if (params.scope === "global") {
          const { stdout } = await execFileAsync(HIPPO_BIN, ["profile"]);
          return {
            content: [{ type: "text", text: stdout.trim() || "暂无全局画像记录。" }],
            details: {},
          };
        }
        const args = ["list"];
        if (params.scope) args.push("--scope", params.scope);
        if (params.limit) args.push("--limit", String(params.limit));
        if (params.project) args.push("--project", params.project);

        const { stdout } = await execFileAsync(HIPPO_BIN, args);
        return {
          content: [{ type: "text", text: stdout.trim() || "指定范围暂无记忆记录。" }],
          details: {},
        };
      } catch (err: any) {
        return {
          content: [{ type: "text", text: `获取记忆列表失败: ${err.message}` }],
          details: { error: String(err) },
        };
      }
    },
  });

  // 4. 获取单条记忆详情工具 (Mem0 标准: get_memory)
  pi.registerTool({
    name: "get_memory",
    label: "Mem0 Get Memory",
    description: "通过记忆 ID 查看单条记忆的详细信息 (Mem0 官方 get_memory 规范)。",
    promptSnippet: "查看指定 ID 的单条记忆详细信息",
    parameters: Type.Object({
      memory_id: Type.String({ description: "要获取的记忆唯一 ID" }),
    }),
    async execute(_toolCallId, params) {
      try {
        const { stdout } = await execFileAsync(HIPPO_BIN, ["get", params.memory_id]);
        return {
          content: [{ type: "text", text: stdout.trim() }],
          details: {},
        };
      } catch (err: any) {
        return {
          content: [{ type: "text", text: `获取记忆失败: ${err.message}` }],
          details: { error: String(err) },
        };
      }
    },
  });

  // 5. 删除单条记忆工具 (Mem0 标准: delete_memory)
  pi.registerTool({
    name: "delete_memory",
    label: "Mem0 Delete Memory",
    description: "根据记忆 ID 删除一条不再需要或过期的记忆事实 (Mem0 官方 delete_memory 规范)。",
    promptSnippet: "根据 ID 删除一条过期或冗余的记忆",
    parameters: Type.Object({
      memory_id: Type.String({ description: "要删除的记忆唯一 ID" }),
    }),
    async execute(_toolCallId, params) {
      try {
        const { stdout } = await execFileAsync(HIPPO_BIN, ["delete", params.memory_id]);
        return {
          content: [{ type: "text", text: stdout.trim() }],
          details: {},
        };
      } catch (err: any) {
        return {
          content: [{ type: "text", text: `删除记忆失败: ${err.message}` }],
          details: { error: String(err) },
        };
      }
    },
  });

  // 6. 注册 /hippo 便捷斜杠命令
  pi.registerCommand("hippo", {
    description: "快速查看 Hippo 记忆库状态或执行检索",
    handler: async (args, ctx) => {
      try {
        const cmdArgs = args ? args.split(" ") : ["status"];
        const { stdout } = await execFileAsync(HIPPO_BIN, cmdArgs);
        ctx.ui.notify(stdout.trim(), "info");
      } catch (err: any) {
        ctx.ui.notify(`Hippo 执行失败: ${err.message}`, "error");
      }
    },
  });
}
