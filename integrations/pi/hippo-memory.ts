import type { ExtensionAPI } from "@earendil-works/pi-coding-agent";
import { Type } from "typebox";
import { execFile } from "node:child_process";
import { promisify } from "node:util";

const execFileAsync = promisify(execFile);
const HIPPO_BIN = "/path/to/hippo/.venv/bin/hippo";

export default function (pi: ExtensionAPI) {
  // 1. 获取用户全局画像工具
  pi.registerTool({
    name: "get_user_profile",
    label: "Hippo User Profile",
    description:
      "获取用户全局的核心偏好画像与环境背景（如 macOS 环境、Surge 代理设置、极简规范工程等）。适合在复杂任务开局时快速对齐意图。",
    promptSnippet: "获取用户全局核心偏好画像与环境背景",
    parameters: Type.Object({}),
    async execute(_toolCallId, _params) {
      try {
        const { stdout } = await execFileAsync(HIPPO_BIN, ["profile"]);
        return {
          content: [{ type: "text", text: stdout.trim() || "暂无全局画像记录。" }],
          details: {},
        };
      } catch (err: any) {
        return {
          content: [{ type: "text", text: `获取画像失败: ${err.message}` }],
          details: { error: String(err) },
        };
      }
    },
  });

  // 2. 语义与混合记忆检索工具
  pi.registerTool({
    name: "search_memory",
    label: "Hippo Search Memory",
    description:
      "在 Hippo 记忆中枢中检索与当前任务相关的全局偏好、项目历史架构决策与避坑指南。",
    promptSnippet: "检索用户的全局偏好或当前项目的历史架构与经验事实",
    parameters: Type.Object({
      query: Type.String({
        description: "检索关键词或自然语言问题，如 'CQRS 踩坑' 或 'Surge 代理'",
      }),
      scope: Type.Optional(
        Type.String({
          description: "检索范围: 'all'(默认) | 'global'(全局偏好) | 'project'(当前项目)",
        })
      ),
      project: Type.Optional(
        Type.String({ description: "可选指定项目名，默认自动根据当前目录探测" })
      ),
    }),
    async execute(_toolCallId, params) {
      try {
        const args = ["search", params.query];
        if (params.scope) args.push("--scope", params.scope);
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

  // 3. 沉淀新记忆工具
  pi.registerTool({
    name: "save_memory",
    label: "Hippo Save Memory",
    description:
      "向 Hippo 统一长时记忆中枢沉淀新的个人习惯、技术选型或当前项目的避坑指南。",
    promptSnippet: "沉淀新的个人习惯、技术选型或项目避坑指南到 Hippo",
    parameters: Type.Object({
      content: Type.String({ description: "要记录的事实经验或规范约束" }),
      scope: Type.Optional(
        Type.String({
          description: "存储范围: 'project'(默认，当前项目) | 'global'(个人全局习惯)",
        })
      ),
      project: Type.Optional(
        Type.String({ description: "可选指定项目名，默认自动关联当前 Git 仓库" })
      ),
    }),
    async execute(_toolCallId, params) {
      try {
        const args = ["add", params.content];
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

  // 4. 注册 /hippo 便捷斜杠命令
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
