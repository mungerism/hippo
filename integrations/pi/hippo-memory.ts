import type { ExtensionAPI } from "@earendil-works/pi-coding-agent";
import { Type } from "typebox";
import { execFile, spawn } from "node:child_process";
import { existsSync } from "node:fs";
import { homedir } from "node:os";
import { join } from "node:path";
import { promisify } from "node:util";

const execFileAsync = promisify(execFile);

function resolveHippoBin(): string {
  if (process.env.HIPPO_BIN) return process.env.HIPPO_BIN;
  const candidates = [
    join(process.cwd(), ".venv", "bin", "hippo"),
    join(homedir(), ".hippo", "bin", "hippo"),
    "/opt/homebrew/bin/hippo",
    "/usr/local/bin/hippo",
  ];
  for (const c of candidates) {
    if (existsSync(c)) return c;
  }
  return "hippo";
}

/**
 * Extract conversation turns from session (aligned with Mem0 official pi-agent-plugin extraction spec)
 */
function extractConversation(messages: any[]): Array<{ role: "user" | "assistant"; content: string }> {
  const result: Array<{ role: "user" | "assistant"; content: string }> = [];
  if (!Array.isArray(messages)) return result;

  for (const msg of messages) {
    if (!msg || (msg.role !== "user" && msg.role !== "assistant")) continue;
    let text = "";
    if (typeof msg.content === "string") {
      text = msg.content;
    } else if (Array.isArray(msg.content)) {
      text = msg.content
        .filter((b: any) => b && (b.type === "text" || typeof b.text === "string"))
        .map((b: any) => b.text || "")
        .join("\n");
    }
    if (text && text.trim()) {
      result.push({ role: msg.role, content: text.trim() });
    }
  }
  return result;
}

export default function (pi: ExtensionAPI) {
  const hippoBin = resolveHippoBin();

  // 1. Semantic and hybrid memory search tool (Mem0 standard: search_memories)
  pi.registerTool({
    name: "search_memories",
    label: "Mem0 Search Memories",
    description:
      "在持久化长期记忆中做语义检索。开始任务或回答任何可能依赖历史上下文的问题前，务必先调用本工具：项目技术选型、历史踩坑、用户偏好、此前对话结论等都存在这里，不要只依赖当前聊天窗口。scope: 'all'(默认，项目记忆+个人全局偏好) | 'project'(仅当前 Git 仓库) | 'global'(仅跨项目个人习惯)。",
    promptSnippet: "开始任务前先检索全局偏好或项目历史架构与经验事实",
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

        const { stdout } = await execFileAsync(hippoBin, args);
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

  // 2. Persist new memory tool (Mem0 standard: add_memory)
  pi.registerTool({
    name: "add_memory",
    label: "Mem0 Add Memory",
    description:
      "向持久化长期记忆沉淀新内容 (兼容 Mem0 官方 add_memory 规范)。当用户表达偏好、做出值得保留的决策、纠正你的行为、或明确要求记住某事时调用；跨项目的个人习惯用 scope='global'，其余默认沉淀到当前项目。",
    promptSnippet: "沉淀用户偏好、重要决策或项目踩坑经验到长时记忆",
    parameters: Type.Object({
      text: Type.String({
        maxLength: 2000,
        description:
          "要记录的事实经验、技术规范或个人偏好 (最多 2000 字符，单句事实；长多轮会话记忆由系统异步 Hook 自动蒸馏)",
      }),
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
        const text = params.text?.trim() || "";
        if (!text) {
          return {
            content: [{ type: "text", text: "保存记忆失败: text 不能为空" }],
            details: { error: "text is empty" },
          };
        }
        if (text.length > 2000) {
          return {
            content: [
              {
                type: "text",
                text: `保存记忆失败: text 长度超过 2000 字符限制 (当前 ${text.length} 字符)。请提炼为简明单句事实，长对话记忆由系统异步 Hook 自动蒸馏。`,
              },
            ],
            details: { error: "text exceeds 2000 characters limit" },
          };
        }

        const args = ["add", text];
        if (params.scope === "global") {
          args.push("--global");
        } else if (params.project) {
          args.push("--project", params.project);
        }

        const { stdout } = await execFileAsync(hippoBin, args);
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

  // 3. Register /hippo convenience slash command
  pi.registerCommand("hippo", {
    description: "快速查看 Hippo 记忆库状态或执行检索",
    handler: async (args, ctx) => {
      try {
        const cmdArgs = args ? args.split(" ") : ["status"];
        const { stdout } = await execFileAsync(hippoBin, cmdArgs);
        ctx.ui.notify(stdout.trim(), "info");
      } catch (err: any) {
        ctx.ui.notify(`Hippo 执行失败: ${err.message}`, "error");
      }
    },
  });

/**
 * Extract touched file paths from conversation context or tool calls
 */
function extractTouchedFiles(messages: any[]): string[] {
  const files = new Set<string>();
  if (!Array.isArray(messages)) return [];
  const fileExtRegex = /\b[\w./-]+\.(?:py|ts|js|jsx|tsx|go|rs|java|c|cpp|h|md|json|toml|yaml|yml|sh|sql)\b/g;

  for (const msg of messages) {
    if (!msg) continue;
    // Check tool call arguments
    if (Array.isArray(msg.tool_calls)) {
      for (const tc of msg.tool_calls) {
        const args = tc?.function?.arguments || tc?.arguments;
        const str = typeof args === "string" ? args : JSON.stringify(args || {});
        const matches = str.match(fileExtRegex);
        if (matches) matches.forEach((f) => files.add(f));
      }
    }
    // Check tool result or content blocks
    if (Array.isArray(msg.content)) {
      for (const block of msg.content) {
        if (block?.type === "tool_result" || block?.type === "tool_use") {
          const str = JSON.stringify(block);
          const matches = str.match(fileExtRegex);
          if (matches) matches.forEach((f) => files.add(f));
        }
      }
    }
  }
  return Array.from(files).sort();
}

  // 4. Dual-event lifecycle distillation (agent_settled primary checkpoint + session_shutdown fallback)
  const dispatchHook = (eventName: string, ctx: any) => {
    try {
      const sessionManager = ctx?.sessionManager;
      let sessionId = "unknown_pi_session";
      if (typeof sessionManager?.getSessionId === "function") {
        try {
          sessionId = String(sessionManager.getSessionId() || "");
        } catch {}
      }
      let transcriptPath = "";
      if (typeof sessionManager?.getSessionFile === "function") {
        try {
          transcriptPath = String(sessionManager.getSessionFile() || "");
        } catch {}
      }

      // Extract in-memory direct messages without disk scanning
      let rawMsgs: any[] = [];
      if (Array.isArray(ctx?.messages)) {
        rawMsgs = ctx.messages;
      } else if (typeof sessionManager?.getMessages === "function") {
        try {
          rawMsgs = sessionManager.getMessages() || [];
        } catch {}
      }
      const extractedTurns = extractConversation(rawMsgs);
      const lastAssistant = extractedTurns.filter((t) => t.role === "assistant").pop()?.content || "";
      const touchedFiles = extractTouchedFiles(rawMsgs);

      const payload = JSON.stringify({
        session_id: sessionId,
        event: eventName,
        project_dir: ctx?.cwd || process.cwd(),
        transcript_path: transcriptPath,
        turns: extractedTurns.slice(-100),
        total_turns: extractedTurns.length,
        last_assistant_message: lastAssistant,
        touched_files: touchedFiles,
      });

      const child = spawn(hippoBin, ["hook", "capture", "--host", "pi"], {
        detached: true,
        stdio: ["pipe", "ignore", "ignore"],
      });

      // Crash prevention: attach async error listeners to child process and stdin to prevent host crashes (e.g. ENOENT)
      child.on("error", () => {
        // Silent fail-safe, never disrupt user
      });

      if (child.stdin) {
        child.stdin.on("error", () => {
          // Catch async EPIPE pipe errors
        });
        child.stdin.write(payload);
        child.stdin.end();
      }
      child.unref();
    } catch {
      // Silent fail-safe for synchronous exceptions
    }
  };

  pi.on("agent_settled", (_event, ctx) => {
    dispatchHook("agent_settled", ctx);
  });

  pi.on("session_shutdown", (_event, ctx) => {
    dispatchHook("session_shutdown", ctx);
  });
}
